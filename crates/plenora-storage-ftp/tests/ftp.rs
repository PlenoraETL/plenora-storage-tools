use std::{collections::BTreeMap, sync::Arc};

use plenora_storage_core::{
    CopyRequest, CredentialMaterial, CredentialResolver, DeleteRequest, Engine, EngineConfig,
    ExecutionControl, GetRequest, ListRequest, ProviderConnection, PutRequest, StatRequest,
    StorageResult,
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
                overwrite: false,
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
                start_after: None,
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
                overwrite: false,
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
