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
        max_buffered_put_bytes: 16 * 1024 * 1024,
    });
    engine.register_provider(Arc::new(S3Provider::new(Arc::new(MinioCredentials))))?;
    Ok(engine)
}

#[tokio::test]
#[ignore = "requires the Docker storage fixtures"]
async fn https_verifies_the_server_certificate_and_hostname()
-> Result<(), Box<dyn std::error::Error>> {
    let endpoint = std::env::var("PLENORA_MINIO_TLS_ENDPOINT")?;
    let mut connection = connection();
    connection.config["endpoint"] = serde_json::json!(endpoint);
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        allow_private_network: true,
        ..EngineConfig::default()
    });
    engine.register_provider(Arc::new(S3Provider::new(Arc::new(MinioCredentials))))?;
    assert!(
        engine
            .test(&connection, &ExecutionControl::default())
            .await?
            .reachable
    );
    connection.config["endpoint"] = serde_json::json!("https://minio-tls-invalid:9000");
    let control = ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(10));
    let error = engine
        .test(&connection, &control)
        .await
        .expect_err("wrong certificate hostname");
    assert_ne!(error.category, plenora_storage_core::ErrorCategory::Timeout);
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires the Docker storage fixtures"]
async fn s3_contract_roundtrip_against_minio() -> Result<(), Box<dyn std::error::Error>> {
    assert_eq!(
        std::env::var("PLENORA_MINIO_TEST").as_deref(),
        Ok("1"),
        "integration fixture must be explicitly enabled"
    );
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
#[ignore = "requires the Docker storage fixtures"]
async fn insecure_minio_requires_explicit_policy() -> Result<(), StorageError> {
    assert_eq!(
        std::env::var("PLENORA_MINIO_TEST").as_deref(),
        Ok("1"),
        "integration fixture must be explicitly enabled"
    );
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

#[tokio::test]
#[ignore = "requires the Docker storage fixtures"]
async fn a_single_trailing_slash_object_is_rejected_before_path_normalization()
-> Result<(), Box<dyn std::error::Error>> {
    use object_store::{
        aws::{AwsAuthorizer, AwsCredential},
        client::{HttpRequest, HttpRequestBody, HttpService},
    };

    assert_eq!(
        std::env::var("PLENORA_MINIO_TEST").as_deref(),
        Ok("1"),
        "integration fixture must be explicitly enabled"
    );
    let mut connection = connection();
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let bucket = format!("review-raw-keys-{nonce}");
    connection.config["bucket"] = serde_json::json!(bucket);
    let endpoint = connection.config["endpoint"]
        .as_str()
        .expect("endpoint")
        .trim_end_matches('/');
    let bucket_url = format!("{endpoint}/{bucket}");
    let object_url = format!("{bucket_url}/folder/");
    let credential = AwsCredential {
        key_id: required_env("PLENORA_MINIO_ACCESS_KEY", "plenora-dev"),
        secret_key: required_env("PLENORA_MINIO_SECRET_KEY", "plenora-dev-secret"),
        token: None,
    };
    let client = reqwest::Client::builder().no_proxy().build()?;
    let send = |method: reqwest::Method, url: String| {
        let client = &client;
        let credential = &credential;
        async move {
            // Bypass object_store::Path deliberately: the server must receive
            // the trailing slash literally, including during cleanup.
            let mut request = HttpRequest::new(HttpRequestBody::empty());
            *request.method_mut() = method;
            *request.uri_mut() = url.parse()?;
            AwsAuthorizer::new(credential, "s3", "us-east-1").try_authorize(&mut request, None)?;
            let response = client.call(request).await?;
            let status = response.status();
            response.into_body().bytes().await?;
            Ok::<_, Box<dyn std::error::Error>>(status)
        }
    };
    assert!(
        send(reqwest::Method::PUT, bucket_url.clone())
            .await?
            .is_success()
    );
    assert!(
        send(reqwest::Method::PUT, object_url.clone())
            .await?
            .is_success()
    );
    let result = engine()?
        .list(
            &connection,
            &ListRequest {
                prefix: None,
                cursor: None,
                max_items: Some(10),
            },
            &ExecutionControl::default(),
        )
        .await;
    assert!(
        send(reqwest::Method::DELETE, object_url)
            .await?
            .is_success()
    );
    assert!(
        send(reqwest::Method::DELETE, bucket_url)
            .await?
            .is_success()
    );
    let error = result.expect_err("the raw folder/ key must never be reported as folder");
    assert_eq!(error.code, "OBJECT_KEY_UNREPRESENTABLE");
    assert_eq!(
        error.category,
        plenora_storage_core::ErrorCategory::Protocol
    );
    assert_eq!(error.retry, plenora_storage_core::RetryDisposition::Never);
    Ok(())
}
