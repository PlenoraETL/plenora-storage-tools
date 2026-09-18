use plenora_storage_core::{
    CredentialMaterial, CredentialResolver, EngineConfig, ProviderConnection, StorageResult,
};
use std::sync::Arc;

struct NoSecrets;
impl CredentialResolver for NoSecrets {
    fn resolve(&self, _: &str) -> StorageResult<CredentialMaterial> {
        panic!("discovery and lifecycle must never resolve credentials")
    }
}

#[test]
fn discovery_and_lifecycle_are_local() {
    let engine = plenora_storage_engine::build_engine(EngineConfig::default(), Arc::new(NoSecrets))
        .expect("application composition");
    let catalog = engine.capabilities();
    let has_provider = cfg!(any(
        feature = "local",
        feature = "s3",
        feature = "sftp",
        feature = "ftp",
        feature = "ftps",
        feature = "azure",
        feature = "gcs",
        feature = "smb",
        feature = "webdav"
    ));
    assert_eq!(catalog.operations.len(), if has_provider { 7 } else { 0 });
    engine.close();
    engine.close();
    assert!(engine.is_closed());
    let connection = ProviderConnection {
        provider: "absent".to_owned(),
        config_contract: "plenora-storage-absent-connection-v1".to_owned(),
        config: serde_json::Value::Null,
        credential_ref: "vault:missing".to_owned(),
    };
    assert_eq!(
        engine
            .preflight(&connection, &[])
            .expect_err("closed engine")
            .code,
        "ENGINE_CLOSED"
    );
}
