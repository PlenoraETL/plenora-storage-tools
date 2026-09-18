use std::{collections::BTreeMap, sync::Arc};

use plenora_storage_core::{
    CredentialMaterial, CredentialResolver, Engine, EngineConfig, ExecutionControl,
    ProviderConnection, PublicationPolicy, PutRequest, RemoteEffect, StorageResult,
};
use plenora_storage_ftp::FtpProvider;
use plenora_storage_s3::S3Provider;
use plenora_storage_sftp::SftpProvider;

struct UnreachableCredentials;

impl CredentialResolver for UnreachableCredentials {
    fn resolve(&self, _reference: &str) -> StorageResult<CredentialMaterial> {
        panic!("local rejection must precede credential resolution")
    }
}

fn engine(policy: EngineConfig) -> Engine {
    let mut engine = Engine::new(policy);
    let credentials = Arc::new(UnreachableCredentials);
    engine
        .register_provider(Arc::new(S3Provider::new(credentials.clone())))
        .unwrap();
    engine
        .register_provider(Arc::new(SftpProvider::new(credentials.clone())))
        .unwrap();
    engine
        .register_provider(Arc::new(FtpProvider::new(credentials)))
        .unwrap();
    engine
}

fn connection(provider: &str, config: serde_json::Value) -> ProviderConnection {
    ProviderConnection {
        provider: provider.to_owned(),
        config_contract: format!("plenora-storage-{provider}-connection-v1"),
        config,
        credential_ref: "secret://test/storage".to_owned(),
    }
}

#[test]
fn preflight_checks_real_provider_configurations_and_transport_policy() {
    let engine = engine(EngineConfig {
        allow_experimental_contracts: true,
        ..EngineConfig::default()
    });
    for provider in ["s3", "sftp", "ftp"] {
        let error = engine
            .preflight(&connection(provider, serde_json::json!({})), &["object"])
            .expect_err("missing provider fields");
        assert_eq!(error.remote_effect, RemoteEffect::None);
    }
    for (provider, config, expected) in [
        (
            "s3",
            serde_json::json!({"endpoint":"http://127.0.0.1", "bucket":"test"}),
            "INSECURE_HTTP_FORBIDDEN",
        ),
        (
            "sftp",
            serde_json::json!({"host":"127.0.0.1"}),
            "SFTP_HOST_KEY_REQUIRED",
        ),
        (
            "ftp",
            serde_json::json!({"host":"127.0.0.1"}),
            "INSECURE_FTP_FORBIDDEN",
        ),
    ] {
        let error = engine
            .preflight(&connection(provider, config), &["object"])
            .expect_err("policy");
        assert_eq!(error.code, expected);
        assert_eq!(error.remote_effect, RemoteEffect::None);
    }
}

#[tokio::test]
async fn oversized_declared_upload_is_rejected_before_contacting_every_provider() {
    let engine = engine(EngineConfig {
        allow_experimental_contracts: true,
        allow_insecure_http: true,
        allow_insecure_ftp: true,
        allow_unverified_ssh: true,
        max_transfer_bytes: 1,
        ..EngineConfig::default()
    });
    for (provider, config) in [
        (
            "s3",
            serde_json::json!({"endpoint":"http://127.0.0.1", "bucket":"test"}),
        ),
        ("sftp", serde_json::json!({"host":"127.0.0.1"})),
        ("ftp", serde_json::json!({"host":"127.0.0.1"})),
    ] {
        let error = engine
            .put(
                &connection(provider, config),
                &PutRequest {
                    key: "existing".to_owned(),
                    overwrite: true,
                    publication_policy: PublicationPolicy::BestEffort,
                    content_type: None,
                    content_length: Some(2),
                    metadata: BTreeMap::new(),
                },
                &mut tokio::io::empty(),
                &ExecutionControl::default(),
            )
            .await
            .expect_err("size preflight");
        assert_eq!(error.code, "TRANSFER_LIMIT_EXCEEDED");
        assert_eq!(error.remote_effect, RemoteEffect::None);
    }
}
