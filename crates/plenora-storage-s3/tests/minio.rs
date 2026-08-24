use std::{collections::BTreeMap, sync::Arc};

use plenora_storage_core::{
    CopyRequest, CredentialMaterial, CredentialResolver, DeleteRequest, Engine, EngineConfig,
    ExecutionControl, GetRequest, ListRequest, ProviderConnection, PublicationPolicy, PutRequest,
    StatRequest, StorageError, StorageProvider, StorageResult,
};
use plenora_storage_s3::{CONFIG_CONTRACT, S3Provider};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct MinioCredentials;

impl CredentialResolver for MinioCredentials {
    fn resolve(&self, _reference: &str) -> StorageResult<CredentialMaterial> {
        Ok(CredentialMaterial::new(BTreeMap::from([
            (
                "access_key_id".to_owned(),
                required_env("PLENORA_MINIO_ACCESS_KEY", "plenora-dev"),
            ),
            (
                "secret_access_key".to_owned(),
                required_env("PLENORA_MINIO_SECRET_KEY", "plenora-dev-secret"),
            ),
        ])))
    }
}

#[test]
fn s3_capabilities_claim_only_native_qualified_guarantees() {
    let capability = S3Provider::new(Arc::new(MinioCredentials)).capabilities();
    assert_eq!(
        capability.attributes.get("put_create_if_absent_atomic"),
        Some(&"true".to_owned())
    );
    assert_eq!(
        capability.attributes.get("copy_create_if_absent_atomic"),
        Some(&"false".to_owned())
    );
    assert_eq!(
        capability.attributes.get("conditional_put"),
        Some(&"native".to_owned())
    );
    assert_eq!(
        capability.attributes.get("atomic_publication"),
        Some(&"true".to_owned())
    );
}

fn required_env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn connection() -> ProviderConnection {
    ProviderConnection {
        provider: "s3".to_owned(),
        config_contract: CONFIG_CONTRACT.to_owned(),
        config: serde_json::json!({
            "endpoint": std::env::var("PLENORA_MINIO_ENDPOINT")
                .unwrap_or_else(|_| "http://minio:9000".to_owned()),
            "bucket": std::env::var("PLENORA_MINIO_BUCKET")
                .unwrap_or_else(|_| "plenora-test".to_owned()),
            "region": "us-east-1",
            "virtual_hosted_style": false
        }),
        credential_ref: "test:minio".to_owned(),
    }
}

fn engine() -> StorageResult<Engine> {
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        allow_insecure_http: true,
        allow_insecure_ftp: false,
        allow_private_network: true,
        allow_unverified_ssh: false,
        max_transfer_bytes: 16 * 1024 * 1024,
        max_list_items: 100,
    });
    engine.register_provider(Arc::new(S3Provider::new(Arc::new(MinioCredentials))))?;
    Ok(engine)
}

#[tokio::test]
async fn s3_contract_roundtrip_against_minio() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var("PLENORA_MINIO_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let engine = engine()?;
    let connection = connection();
    let control = ExecutionControl::default();
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let prefix = format!("conformance/{}-{nonce}/", std::process::id());
    let source_key = format!("{prefix}source.bin");
    let copied_key = format!("{prefix}copy.bin");
    let payload = b"plenora-storage-tools minio conformance".to_vec();

    let tested = engine.test(&connection, &control).await?;
    assert!(tested.reachable);

    let (mut producer, mut source) = tokio::io::duplex(1024);
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
                publication_policy: PublicationPolicy::AtomicRequired,
                content_type: Some("application/octet-stream".to_owned()),
                content_length: Some(payload.len() as u64),
                metadata: BTreeMap::from([("plenora-test".to_owned(), "true".to_owned())]),
            },
            &mut source,
            &control,
        )
        .await?;
    producer_task.await??;
    assert_eq!(put.bytes_transferred, payload.len() as u64);

    let mut duplicate_source = tokio::io::empty();
    let duplicate = engine
        .put(
            &connection,
            &PutRequest {
                key: source_key.clone(),
                overwrite: false,
                publication_policy: PublicationPolicy::AtomicRequired,
                content_type: None,
                content_length: Some(0),
                metadata: BTreeMap::new(),
            },
            &mut duplicate_source,
            &control,
        )
        .await
        .expect_err("native create-if-absent must reject an existing object");
    assert_eq!(
        duplicate.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );

    let stat = engine
        .stat(
            &connection,
            &StatRequest {
                key: source_key.clone(),
            },
            &control,
        )
        .await?;
    assert_eq!(stat.size, payload.len() as u64);

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

    let (mut sink, mut consumer) = tokio::io::duplex(1024);
    let consumer_task = tokio::spawn(async move {
        let mut output = Vec::new();
        consumer.read_to_end(&mut output).await?;
        Ok::<_, std::io::Error>(output)
    });
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
    drop(sink);
    assert_eq!(consumer_task.await??, payload);
    assert_eq!(get.checksum, put.checksum);

    let rejected_copy = engine
        .copy(
            &connection,
            &CopyRequest {
                source_key: source_key.clone(),
                destination_key: copied_key.clone(),
                overwrite: false,
                publication_policy: PublicationPolicy::AtomicRequired,
            },
            &control,
        )
        .await
        .expect_err("unsupported conditional copy must fail before mutation");
    assert_eq!(rejected_copy.code, "S3_COPY_CREATE_IF_ABSENT_UNSUPPORTED");
    assert_eq!(
        rejected_copy.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );

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

    for key in [&copied_key, &source_key] {
        let deleted = engine
            .delete(
                &connection,
                &DeleteRequest {
                    key: key.clone(),
                    ignore_missing: false,
                },
                &control,
            )
            .await?;
        assert!(deleted.deleted);
    }
    engine.close();
    Ok(())
}

#[tokio::test]
async fn insecure_minio_requires_explicit_policy() -> Result<(), StorageError> {
    if std::env::var("PLENORA_MINIO_TEST").as_deref() != Ok("1") {
        return Ok(());
    }
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        ..EngineConfig::default()
    });
    engine.register_provider(Arc::new(S3Provider::new(Arc::new(MinioCredentials))))?;
    let error = engine
        .test(&connection(), &ExecutionControl::default())
        .await
        .expect_err("HTTP MinIO must be denied by the default engine policy");
    assert_eq!(error.code, "INSECURE_HTTP_FORBIDDEN");
    Ok(())
}
