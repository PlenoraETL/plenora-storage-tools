use plenora_storage_core::*;
use plenora_storage_providers::{
    AzureProvider, GcsProvider, LocalProvider, SmbProvider, WebDavProvider,
};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

struct NoCredentials;
impl CredentialResolver for NoCredentials {
    fn resolve(&self, _: &str) -> StorageResult<CredentialMaterial> {
        panic!("local/preflight must never resolve credentials")
    }
}
struct Fixture {
    root: PathBuf,
    engine: Engine,
    connection: ProviderConnection,
}
impl Fixture {
    fn new(limit: u64) -> Self {
        static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "plenora-local-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let mut engine = Engine::new(EngineConfig {
            max_buffered_put_bytes: limit,
            ..EngineConfig::default()
        });
        engine
            .register_provider(Arc::new(LocalProvider::new(Arc::new(NoCredentials))))
            .unwrap();
        let connection = ProviderConnection {
            provider: "local".to_owned(),
            config_contract: "plenora-storage-local-connection-v1".to_owned(),
            config: serde_json::json!({"root":root}),
            credential_ref: "local:process".to_owned(),
        };
        Self {
            root,
            engine,
            connection,
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
fn put(key: &str, overwrite: bool) -> PutRequest {
    PutRequest {
        key: key.to_owned(),
        overwrite,
        publication_policy: PublicationPolicy::AtomicRequired,
        content_length: None,
        content_type: None,
        metadata: BTreeMap::new(),
    }
}
#[tokio::test]
async fn local_seven_operations_pagination_and_atomic_conflict() {
    let f = Fixture::new(1024);
    let control = ExecutionControl::default();
    assert!(
        f.engine
            .test(&f.connection, &control)
            .await
            .unwrap()
            .reachable
    );
    for key in ["folder/z", "folder/a", "folder/m"] {
        f.engine
            .put(
                &f.connection,
                &put(key, false),
                &mut b"payload".as_slice(),
                &control,
            )
            .await
            .unwrap();
    }
    let first = f
        .engine
        .list(
            &f.connection,
            &ListRequest {
                prefix: Some("folder".to_owned()),
                max_items: Some(2),
                cursor: None,
            },
            &control,
        )
        .await
        .unwrap();
    assert!(first.truncated);
    assert_eq!(first.objects[0].key, "folder/a");
    let last = f
        .engine
        .list(
            &f.connection,
            &ListRequest {
                prefix: Some("folder".to_owned()),
                max_items: Some(2),
                cursor: first.next_cursor,
            },
            &control,
        )
        .await
        .unwrap();
    assert!(!last.truncated);
    assert_eq!(last.objects[0].key, "folder/z");
    let error = f
        .engine
        .put(
            &f.connection,
            &put("folder/a", false),
            &mut b"bad".as_slice(),
            &control,
        )
        .await
        .unwrap_err();
    assert_eq!(error.category, ErrorCategory::Conflict);
    let mut data = Vec::new();
    let got = f
        .engine
        .get(
            &f.connection,
            &GetRequest {
                key: "folder/a".to_owned(),
            },
            &mut data,
            &control,
        )
        .await
        .unwrap();
    assert_eq!(data, b"payload");
    assert_eq!(got.bytes_transferred, 7);
    f.engine
        .copy(
            &f.connection,
            &CopyRequest {
                source_key: "folder/a".to_owned(),
                destination_key: "copy".to_owned(),
                overwrite: false,
                publication_policy: PublicationPolicy::AtomicRequired,
            },
            &control,
        )
        .await
        .unwrap();
    assert_eq!(
        f.engine
            .stat(
                &f.connection,
                &StatRequest {
                    key: "copy".to_owned()
                },
                &control
            )
            .await
            .unwrap()
            .size,
        7
    );
    assert!(
        f.engine
            .delete(
                &f.connection,
                &DeleteRequest {
                    key: "copy".to_owned(),
                    ignore_missing: false
                },
                &control
            )
            .await
            .unwrap()
            .deleted
    );
    assert!(
        !f.engine
            .delete(
                &f.connection,
                &DeleteRequest {
                    key: "copy".to_owned(),
                    ignore_missing: true
                },
                &control
            )
            .await
            .unwrap()
            .deleted
    );
}
#[tokio::test]
async fn oversize_mismatch_and_cancel_preserve_existing_destination() {
    let f = Fixture::new(4);
    let control = ExecutionControl::default();
    std::fs::write(f.root.join("object"), b"old").unwrap();
    let error = f
        .engine
        .put(
            &f.connection,
            &put("object", true),
            &mut b"large".as_slice(),
            &control,
        )
        .await
        .unwrap_err();
    assert_eq!(error.category, ErrorCategory::ResourceLimit);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    let mut request = put("object", true);
    request.content_length = Some(1);
    assert_eq!(
        f.engine
            .put(&f.connection, &request, &mut b"bad".as_slice(), &control)
            .await
            .unwrap_err()
            .code,
        "CONTENT_LENGTH_MISMATCH"
    );
    control.cancellation.cancel();
    assert_eq!(
        f.engine
            .put(
                &f.connection,
                &put("object", true),
                &mut b"new".as_slice(),
                &control
            )
            .await
            .unwrap_err()
            .category,
        ErrorCategory::Cancelled
    );
    assert_eq!(std::fs::read(f.root.join("object")).unwrap(), b"old");
    assert_eq!(std::fs::read_dir(&f.root).unwrap().count(), 1);
}
#[tokio::test]
async fn concurrent_create_has_exactly_one_winner() {
    let f = Fixture::new(1024);
    let control = ExecutionControl::default();
    let request = put("winner", false);
    let (a, b) = tokio::join!(
        async {
            f.engine
                .put(&f.connection, &request, &mut b"first".as_slice(), &control)
                .await
        },
        async {
            f.engine
                .put(&f.connection, &request, &mut b"second".as_slice(), &control)
                .await
        }
    );
    assert_ne!(a.is_ok(), b.is_ok());
    assert_eq!(
        a.err().or_else(|| b.err()).unwrap().category,
        ErrorCategory::Conflict
    );
    let data = std::fs::read(f.root.join("winner")).unwrap();
    assert!(data == b"first" || data == b"second");
    assert_eq!(std::fs::read_dir(&f.root).unwrap().count(), 1);
}
#[tokio::test]
async fn filesystem_aliases_and_traversal_cannot_write() {
    let f = Fixture::new(100);
    let control = ExecutionControl::default();
    for key in [
        "../escape",
        "C:/escape",
        "file:stream",
        "NUL",
        "a/CON.txt",
        "trailing.",
        ".plenora-stage-reserved",
    ] {
        assert!(
            f.engine
                .put(
                    &f.connection,
                    &put(key, true),
                    &mut b"new".as_slice(),
                    &control
                )
                .await
                .is_err(),
            "{key}"
        );
    }
    assert_eq!(std::fs::read_dir(&f.root).unwrap().count(), 0);
}
#[cfg(unix)]
#[tokio::test]
async fn symlink_escape_is_rejected_for_reads_and_writes() {
    let f = Fixture::new(100);
    let outside = Fixture::new(100);
    let control = ExecutionControl::default();
    std::fs::write(outside.root.join("secret"), b"unchanged").unwrap();
    std::os::unix::fs::symlink(&outside.root, f.root.join("escape")).unwrap();
    assert!(
        f.engine
            .put(
                &f.connection,
                &put("escape/secret", true),
                &mut b"bad".as_slice(),
                &control
            )
            .await
            .is_err()
    );
    assert!(
        f.engine
            .get(
                &f.connection,
                &GetRequest {
                    key: "escape/secret".to_owned()
                },
                &mut Vec::new(),
                &control
            )
            .await
            .is_err()
    );
    assert_eq!(
        std::fs::read(outside.root.join("secret")).unwrap(),
        b"unchanged"
    );
}
#[test]
fn network_admission_rejects_inline_secrets_http_and_wrong_identity() {
    let providers: Vec<Box<dyn StorageProvider>> = vec![
        Box::new(AzureProvider::new(Arc::new(NoCredentials))),
        Box::new(GcsProvider::new(Arc::new(NoCredentials))),
        Box::new(SmbProvider::new(Arc::new(NoCredentials))),
        Box::new(WebDavProvider::new(Arc::new(NoCredentials))),
    ];
    for provider in providers {
        let mut connection = ProviderConnection {
            provider: provider.id().to_owned(),
            config_contract: provider.config_contract().to_owned(),
            credential_ref: "env:TEST".to_owned(),
            config: serde_json::json!({"endpoint":"http://127.0.0.1/","password":"secret"}),
        };
        assert!(
            provider
                .validate_connection(&connection, &EngineConfig::default())
                .is_err()
        );
        connection.config = match provider.id() {
            "azure" => {
                serde_json::json!({"endpoint":"http://127.0.0.1","account":"account","container":"bucket"})
            }
            "gcs" => serde_json::json!({"endpoint":"http://127.0.0.1","bucket":"bucket"}),
            "smb" => serde_json::json!({"host":"host","share":"../escape"}),
            _ => serde_json::json!({"endpoint":"http://127.0.0.1/"}),
        };
        assert!(
            provider
                .validate_connection(&connection, &EngineConfig::default())
                .is_err()
        );
    }
}
