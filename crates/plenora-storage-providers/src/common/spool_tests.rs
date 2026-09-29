use super::*;
use plenora_storage_core::{EngineConfig, ExecutionControl, RemoteEffect};

#[tokio::test]
async fn spool_rewinds_and_hashes_without_retaining_the_payload() {
    let mut spool = Spool::new().await.unwrap();
    for part in [b"first".as_slice(), b"second"] {
        spool.append(part, 11).await.unwrap();
    }
    spool.finish(Some(11)).await.unwrap();
    let mut data = Vec::new();
    spool.file.read_to_end(&mut data).await.unwrap();
    assert_eq!(data, b"firstsecond");
    assert_eq!(spool.size, 11);
    assert_eq!(spool.digest.finalize(), Sha256::digest(&data));
}

#[tokio::test]
async fn failed_preparation_has_no_destination_effect_or_private_error() {
    let mut spool = Spool::new().await.unwrap();
    spool.append(b"existing", 8).await.unwrap();
    let error = spool.append(b"private-payload", 8).await.unwrap_err();
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(spool.size, 8);
    assert!(!serde_json::to_string(&error).unwrap().contains("private"));
    let error = spool.finish(Some(9)).await.unwrap_err();
    assert_eq!(error.code, "CONTENT_LENGTH_MISMATCH");
    assert_eq!(error.remote_effect, RemoteEffect::None);
}

#[tokio::test]
async fn cancelled_or_oversized_input_is_not_consumed() {
    let policy = EngineConfig {
        max_transfer_bytes: 2,
        ..EngineConfig::default()
    };
    let control = ExecutionControl::default();
    let context = OperationContext {
        policy: &policy,
        control: &control,
    };
    let mut source = b"abc".as_slice();
    let Err(error) = Spool::read(&mut source, Some(3), &context, u64::MAX).await else {
        panic!("oversized input was accepted");
    };
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(source, b"abc");
    control.cancellation.cancel();
    assert!(
        Spool::read(&mut source, None, &context, u64::MAX)
            .await
            .is_err()
    );
    assert_eq!(source, b"abc");
}
