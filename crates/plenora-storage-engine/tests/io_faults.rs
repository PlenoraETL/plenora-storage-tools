//! Consumer I/O failures exercise the real adapters, including remote cleanup.
#![cfg(feature = "full")]

use plenora_storage_core::{
    CancellationToken, CredentialMaterial, CredentialResolver, DeleteRequest, EngineConfig,
    ErrorCategory, ErrorPhase, ExecutionControl, GetRequest, ListRequest, ProviderConnection,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};
use plenora_storage_engine::UploadStrategy;
use std::{
    collections::BTreeMap,
    io,
    path::Path,
    pin::Pin,
    process::Command,
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

const PRIVATE_ERROR: &str = "private-callback-content-never-expose";
const PAYLOAD: &[u8] = b"previous-object-content-must-survive";

struct Resolver(BTreeMap<String, String>);
impl CredentialResolver for Resolver {
    fn resolve(&self, _: &str) -> StorageResult<CredentialMaterial> {
        Ok(CredentialMaterial::new(self.0.clone()))
    }
}

struct BrokenReader(bool);
impl AsyncRead for BrokenReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if self.0 {
            self.0 = false;
            buffer.put_slice(b"partial-input");
            Poll::Ready(Ok(()))
        } else {
            Poll::Ready(Err(io::Error::other(PRIVATE_ERROR)))
        }
    }
}

struct BrokenWriter {
    fail_flush: bool,
    cancel_flush: Option<CancellationToken>,
    accepted: usize,
    flushed: bool,
}
impl AsyncWrite for BrokenWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        if !self.fail_flush && self.accepted > 0 {
            return Poll::Ready(Err(io::Error::other(PRIVATE_ERROR)));
        }
        let count = if self.fail_flush {
            data.len()
        } else {
            data.len().min(1)
        };
        self.accepted += count;
        Poll::Ready(Ok(count))
    }
    fn poll_flush(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.flushed = true;
        if let Some(token) = &self.cancel_flush {
            token.cancel();
            return Poll::Pending;
        }
        Poll::Ready(if self.fail_flush {
            Err(io::Error::other(PRIVATE_ERROR))
        } else {
            Ok(())
        })
    }
    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

fn control() -> ExecutionControl {
    ExecutionControl::default().with_deadline(Instant::now() + Duration::from_secs(30))
}

fn fixture(provider: &str, local: &Path) -> (ProviderConnection, Resolver) {
    if provider == "local" {
        return (
            ProviderConnection {
                provider: provider.to_owned(),
                config_contract: "plenora-storage-local-connection-v1".to_owned(),
                config: serde_json::json!({"root": local}),
                credential_ref: "local:process".to_owned(),
            },
            Resolver(BTreeMap::new()),
        );
    }
    // Reuse the fixture inventory rather than maintaining another set of test
    // identities here. This path runs only in the repository's Linux fixture job.
    let scripts = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts");
    let output = Command::new("python3").args(["-c",
        "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from fixture_connections import fixture; print(json.dumps(fixture(sys.argv[2],Path(sys.argv[3]))))"])
        .arg(scripts).arg(provider).arg(local).output().expect("fixture discovery");
    assert!(output.status.success(), "fixture discovery failed");
    let (connection, credentials) = serde_json::from_slice(&output.stdout).expect("fixture JSON");
    (connection, Resolver(credentials))
}

fn redacted(error: &StorageError, provider: &str, phase: ErrorPhase) {
    assert_eq!(error.category, ErrorCategory::Io, "{provider}");
    assert_eq!(error.phase, phase, "{provider}");
    assert!(
        !serde_json::to_string(error)
            .unwrap()
            .contains(PRIVATE_ERROR),
        "{provider}"
    );
}

#[allow(
    clippy::too_many_lines,
    reason = "Apply the same source, sink and cleanup fault matrix to every provider"
)]
async fn exercise(provider: &str, strategy: UploadStrategy) {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let prefix = format!("io-fault-{}-{nonce}", std::process::id());
    let key = format!("{prefix}/object");
    let local = std::env::temp_dir().join(&prefix);
    std::fs::create_dir(&local).unwrap();
    let (connection, resolver) = fixture(provider, &local);
    let engine = plenora_storage_engine::build_engine_with_upload_strategy(
        EngineConfig {
            allow_private_network: true,
            allow_insecure_http: true,
            allow_insecure_ftp: true,
            ..EngineConfig::default()
        },
        Arc::new(resolver),
        strategy,
    )
    .unwrap();
    let atomic = !matches!(provider, "ftp" | "ftps" | "smb" | "webdav");
    let request = PutRequest {
        key: key.clone(),
        overwrite: true,
        publication_policy: if atomic {
            PublicationPolicy::AtomicRequired
        } else {
            PublicationPolicy::BestEffort
        },
        content_type: None,
        content_length: Some(PAYLOAD.len() as u64),
        metadata: BTreeMap::new(),
    };
    engine
        .put(&connection, &request, &mut &*PAYLOAD, &control())
        .await
        .unwrap();
    for fail_flush in [false, true] {
        let mut sink = BrokenWriter {
            fail_flush,
            cancel_flush: None,
            accepted: 0,
            flushed: false,
        };
        let result = engine
            .get(
                &connection,
                &GetRequest { key: key.clone() },
                &mut sink,
                &control(),
            )
            .await;
        let error =
            result.expect_err("consumer I/O failure must not be reported as a successful download");
        redacted(&error, provider, ErrorPhase::Write);
        assert!(sink.accepted > 0, "{provider}");
        assert_eq!(sink.flushed, fail_flush, "{provider}");
        assert_eq!(error.remote_effect, RemoteEffect::Unknown, "{provider}");
        assert_eq!(
            error.retry,
            RetryDisposition::RequiresRecovery,
            "{provider}"
        );
    }
    let cancelled = control();
    let mut sink = BrokenWriter {
        fail_flush: true,
        cancel_flush: Some(cancelled.cancellation.clone()),
        accepted: 0,
        flushed: false,
    };
    let error = engine
        .get(
            &connection,
            &GetRequest { key: key.clone() },
            &mut sink,
            &cancelled,
        )
        .await
        .expect_err("a blocked flush must remain cancellable");
    assert!(sink.flushed && sink.accepted > 0, "{provider}");
    assert_eq!(error.category, ErrorCategory::Cancelled, "{provider}");
    assert_eq!(error.phase, ErrorPhase::Write, "{provider}");
    assert_eq!(error.remote_effect, RemoteEffect::Unknown, "{provider}");
    assert_eq!(
        error.retry,
        RetryDisposition::RequiresRecovery,
        "{provider}"
    );
    let mut broken = BrokenReader(true);
    let error = engine
        .put(
            &connection,
            &PutRequest {
                content_length: None,
                ..request
            },
            &mut broken,
            &control(),
        )
        .await
        .expect_err("source failure must not report a successful upload");
    redacted(&error, provider, ErrorPhase::Read);
    let expected = match provider {
        "s3" | "sftp" => RemoteEffect::RolledBack,
        "ftp" | "ftps" => RemoteEffect::Unknown,
        _ => RemoteEffect::None,
    };
    assert_eq!(error.remote_effect, expected, "{provider}");
    if expected != RemoteEffect::Unknown {
        let mut data = Vec::new();
        engine
            .get(
                &connection,
                &GetRequest { key: key.clone() },
                &mut data,
                &control(),
            )
            .await
            .unwrap();
        assert_eq!(
            data, PAYLOAD,
            "{provider}: failed upload changed the existing object"
        );
    }
    let listed = engine
        .list(
            &connection,
            &ListRequest {
                prefix: Some(prefix.clone()),
                ..ListRequest::default()
            },
            &control(),
        )
        .await
        .unwrap();
    assert_eq!(
        listed.objects.len(),
        1,
        "{provider}: unexpected visible object"
    );
    assert_eq!(listed.objects[0].key, key, "{provider}");
    engine
        .delete(
            &connection,
            &DeleteRequest {
                key,
                ignore_missing: false,
            },
            &control(),
        )
        .await
        .unwrap();
    if provider == "local" {
        std::fs::remove_dir(local.join(prefix)).unwrap();
    }
    std::fs::remove_dir(&local).unwrap();
    println!(
        "PASS {provider}: partial write, flush error/cancellation, source error, effect axes, redaction and namespace"
    );
}

#[tokio::test]
async fn local_consumer_failures_preserve_the_published_object() {
    exercise("local", UploadStrategy::Buffered).await;
}

#[tokio::test]
async fn local_prepared_consumer_failures_preserve_the_published_object() {
    exercise("local", UploadStrategy::PrivateFile).await;
}

#[tokio::test]
#[ignore = "requires all nine Linux storage fixtures and Python fixture discovery"]
async fn every_provider_preserves_consumer_io_failure_contracts() {
    for provider in [
        "local", "s3", "sftp", "ftp", "ftps", "azure", "gcs", "smb", "webdav",
    ] {
        exercise(provider, UploadStrategy::Buffered).await;
    }
}

#[tokio::test]
#[ignore = "requires five Linux storage fixtures and Python fixture discovery"]
async fn prepared_providers_preserve_consumer_io_failure_contracts() {
    for provider in ["local", "azure", "gcs", "smb", "webdav"] {
        exercise(provider, UploadStrategy::PrivateFile).await;
    }
}
