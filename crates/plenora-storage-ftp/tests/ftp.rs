use std::{collections::BTreeMap, sync::Arc};

use plenora_storage_core::{
    CopyRequest, CredentialMaterial, CredentialResolver, DeleteRequest, Engine, EngineConfig,
    ExecutionControl, GetRequest, ListRequest, ProviderConnection, PublicationPolicy, PutRequest,
    StatRequest, StorageProvider, StorageResult,
};
use plenora_storage_ftp::{CONFIG_CONTRACT, FtpProvider};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct TestCredentials;

impl CredentialResolver for TestCredentials {
    fn resolve(&self, _reference: &str) -> StorageResult<CredentialMaterial> {
        Ok(CredentialMaterial::new(BTreeMap::from([
            ("username".to_owned(), "plenora".to_owned()),
            ("password".to_owned(), "plenora-ftp-secret".to_owned()),
        ])))
    }
}

#[test]
fn ftp_capabilities_do_not_claim_atomic_publication() {
    let capability = FtpProvider::new(Arc::new(TestCredentials)).capabilities();
    assert_eq!(
        capability.attributes.get("put_create_if_absent_atomic"),
        Some(&"false".to_owned())
    );
    assert_eq!(
        capability.attributes.get("copy_create_if_absent_atomic"),
        Some(&"false".to_owned())
    );
    assert_eq!(
        capability.attributes.get("overwrite_false"),
        Some(&"rejected".to_owned())
    );
    assert_eq!(
        capability.attributes.get("atomic_publication"),
        Some(&"false".to_owned())
    );
}

#[tokio::test]
async fn ftp_rejects_unavailable_publication_policies_before_connecting() -> StorageResult<()> {
    let engine = engine(true)?;
    let control = ExecutionControl::default();
    let mut source = tokio::io::empty();
    let error = engine
        .put(
            &connection(),
            &PutRequest {
                key: "must-not-exist.bin".to_owned(),
                overwrite: false,
                publication_policy: PublicationPolicy::BestEffort,
                content_type: None,
                content_length: Some(0),
                metadata: BTreeMap::new(),
            },
            &mut source,
            &control,
        )
        .await
        .expect_err("FTP create-if-absent must be rejected before connecting");
    assert_eq!(error.code, "FTP_CREATE_IF_ABSENT_UNSUPPORTED");
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );

    let error = engine
        .copy(
            &connection(),
            &CopyRequest {
                source_key: "a".to_owned(),
                destination_key: "b".to_owned(),
                overwrite: true,
                publication_policy: PublicationPolicy::AtomicRequired,
            },
            &control,
        )
        .await
        .expect_err("FTP atomic publication must be rejected before connecting");
    assert_eq!(error.code, "FTP_ATOMIC_PUBLICATION_UNSUPPORTED");
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );
    Ok(())
}

fn connection() -> ProviderConnection {
    ProviderConnection {
        provider: "ftp".to_owned(),
        config_contract: CONFIG_CONTRACT.to_owned(),
        config: serde_json::json!({
            "host": std::env::var("PLENORA_FTP_ENDPOINT")
                .unwrap_or_else(|_| "ftp".to_owned()),
            "port": 21,
            "root": ".",
            "mode": "passive"
        }),
        credential_ref: "test:ftp".to_owned(),
    }
}

fn engine(allow_insecure_ftp: bool) -> StorageResult<Engine> {
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        allow_insecure_http: false,
        allow_insecure_ftp,
        allow_private_network: true,
        allow_unverified_ssh: false,
        max_transfer_bytes: 16 * 1024 * 1024,
        max_list_items: 100,
    });
    engine.register_provider(Arc::new(FtpProvider::new(Arc::new(TestCredentials))))?;
    Ok(engine)
}

#[tokio::test]
async fn plain_ftp_requires_explicit_policy() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var("PLENORA_FTP_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let error = engine(false)?
        .test(&connection(), &ExecutionControl::default())
        .await
        .expect_err("plain FTP must fail closed");
    assert_eq!(error.code, "INSECURE_FTP_FORBIDDEN");
    Ok(())
}

#[tokio::test]
async fn ftp_contract_roundtrip() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var("PLENORA_FTP_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let engine = engine(true)?;
    let connection = connection();
    let control = ExecutionControl::default();
    let prefix = format!("conformance/ftp-{}/", std::process::id());
    let source_key = format!("{prefix}source.bin");
    let copied_key = format!("{prefix}copy.bin");
    let payload = b"plenora-storage-tools ftp conformance".to_vec();

    assert!(engine.test(&connection, &control).await?.reachable);

    let (mut producer, mut source) = tokio::io::duplex(1_024);
    let input = payload.clone();
    let producer_task = tokio::spawn(async move {
        producer.write_all(&input).await?;
        producer.shutdown().await
    });
    let put = engine
        .put(
            &connection,
            &PutRequest {
                key: source_key.clone(),
                overwrite: true,
                publication_policy: PublicationPolicy::BestEffort,
                content_type: None,
                content_length: Some(payload.len() as u64),
                metadata: BTreeMap::new(),
            },
            &mut source,
            &control,
        )
        .await?;
    producer_task.await??;
    assert_eq!(put.bytes_transferred, payload.len() as u64);

    let metadata = engine
        .stat(
            &connection,
            &StatRequest {
                key: source_key.clone(),
            },
            &control,
        )
        .await?;
    assert_eq!(metadata.size, payload.len() as u64);

    let listed = engine
        .list(
            &connection,
            &ListRequest {
                prefix: Some(prefix.clone()),
                cursor: None,
                max_items: Some(10),
            },
            &control,
        )
        .await?;
    assert!(listed.objects.iter().any(|object| object.key == source_key));

    let (mut sink, mut consumer) = tokio::io::duplex(1_024);
    let get = engine
        .get(
            &connection,
            &GetRequest {
                key: source_key.clone(),
            },
            &mut sink,
            &control,
        )
        .await?;
    sink.shutdown().await?;
    let mut downloaded = Vec::new();
    consumer.read_to_end(&mut downloaded).await?;
    assert_eq!(downloaded, payload);
    assert_eq!(get.checksum, put.checksum);

    let copied = engine
        .copy(
            &connection,
            &CopyRequest {
                source_key: source_key.clone(),
                destination_key: copied_key.clone(),
                overwrite: true,
                publication_policy: PublicationPolicy::BestEffort,
            },
            &control,
        )
        .await?;
    assert_eq!(copied.size, payload.len() as u64);

    for key in [&source_key, &copied_key] {
        assert!(
            engine
                .delete(
                    &connection,
                    &DeleteRequest {
                        key: key.clone(),
                        ignore_missing: false,
                    },
                    &control,
                )
                .await?
                .deleted
        );
    }
    Ok(())
}
