use std::{collections::BTreeMap, sync::Arc};

use plenora_storage_core::{
    CopyRequest, CredentialMaterial, CredentialResolver, DeleteRequest, Engine, EngineConfig,
    ExecutionControl, GetRequest, ListRequest, ProviderConnection, PublicationPolicy, PutRequest,
    StatRequest, StorageProvider, StorageResult,
};
use plenora_storage_sftp::{CONFIG_CONTRACT, SftpProvider};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct TestCredentials;

impl CredentialResolver for TestCredentials {
    fn resolve(&self, _reference: &str) -> StorageResult<CredentialMaterial> {
        Ok(CredentialMaterial::new(BTreeMap::from([
            ("username".to_owned(), "plenora".to_owned()),
            ("password".to_owned(), "plenora-sftp-secret".to_owned()),
        ])))
    }
}

fn connection() -> ProviderConnection {
    ProviderConnection {
        provider: "sftp".to_owned(),
        config_contract: CONFIG_CONTRACT.to_owned(),
        config: serde_json::json!({
            "host": std::env::var("PLENORA_SFTP_ENDPOINT")
                .unwrap_or_else(|_| "sftp".to_owned()),
            "port": 22,
            "root": "upload",
            "host_key_sha256": null,
            "atomic_rename": true
        }),
        credential_ref: "test:sftp".to_owned(),
    }
}

#[test]
fn sftp_capabilities_qualify_atomic_publication() {
    let capability = SftpProvider::new(Arc::new(TestCredentials)).capabilities();
    assert_eq!(
        capability.attributes.get("put_create_if_absent_atomic"),
        Some(&"true".to_owned())
    );
    assert_eq!(
        capability.attributes.get("copy_create_if_absent_atomic"),
        Some(&"true".to_owned())
    );
    assert_eq!(
        capability.attributes.get("atomic_publication"),
        Some(&"qualified_by_connection".to_owned())
    );
}

#[tokio::test]
async fn sftp_rejects_atomic_publication_when_connection_is_unqualified() -> StorageResult<()> {
    let engine = engine(true)?;
    let mut unqualified = connection();
    unqualified.config["atomic_rename"] = serde_json::json!(false);
    let mut source = tokio::io::empty();
    let error = engine
        .put(
            &unqualified,
            &PutRequest {
                key: "must-not-exist.bin".to_owned(),
                overwrite: true,
                publication_policy: PublicationPolicy::AtomicRequired,
                content_type: None,
                content_length: Some(0),
                metadata: BTreeMap::new(),
            },
            &mut source,
            &ExecutionControl::default(),
        )
        .await
        .expect_err("an unqualified SFTP connection must reject atomic publication");
    assert_eq!(error.code, "SFTP_ATOMIC_PUBLICATION_UNAVAILABLE");
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );
    Ok(())
}

fn engine(allow_unverified_ssh: bool) -> StorageResult<Engine> {
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        allow_insecure_http: false,
        allow_insecure_ftp: false,
        allow_private_network: true,
        allow_unverified_ssh,
        max_transfer_bytes: 16 * 1024 * 1024,
        max_list_items: 100,
    });
    engine.register_provider(Arc::new(SftpProvider::new(Arc::new(TestCredentials))))?;
    Ok(engine)
}

#[tokio::test]
async fn unverified_host_key_requires_explicit_policy() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var("PLENORA_SFTP_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let error = engine(false)?
        .test(&connection(), &ExecutionControl::default())
        .await
        .expect_err("an unpinned host must fail closed");
    assert_eq!(error.code, "SFTP_HOST_KEY_REQUIRED");
    Ok(())
}

#[tokio::test]
async fn sftp_contract_roundtrip() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var("PLENORA_SFTP_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let engine = engine(true)?;
    let connection = connection();
    let control = ExecutionControl::default();
    let prefix = format!("conformance/sftp-{}/", std::process::id());
    let source_key = format!("{prefix}source.bin");
    let copied_key = format!("{prefix}copy.bin");
    let payload = b"plenora-storage-tools sftp conformance".to_vec();

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
                publication_policy: PublicationPolicy::AtomicRequired,
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
                publication_policy: PublicationPolicy::AtomicRequired,
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
