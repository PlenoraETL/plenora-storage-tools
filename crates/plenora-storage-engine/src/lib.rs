//! Application entry point with explicit provider selection through Cargo features.
#![forbid(unsafe_code)]
use plenora_storage_core::{
    CredentialResolver, Engine, EngineConfig, StorageProvider, StorageResult,
};
use std::sync::Arc;

/// Build a reusable engine using only the providers compiled into this artifact.
/// No credentials are resolved and no connection is opened during construction.
pub fn build_engine(
    config: EngineConfig,
    credentials: Arc<dyn CredentialResolver>,
) -> StorageResult<Engine> {
    let mut engine = Engine::new(config);
    for provider in providers(credentials) {
        engine.register_provider(provider)?;
    }
    Ok(engine)
}

fn providers(credentials: Arc<dyn CredentialResolver>) -> Vec<Arc<dyn StorageProvider>> {
    let providers: Vec<Arc<dyn StorageProvider>> = vec![
        #[cfg(feature = "local")]
        Arc::new(plenora_storage_providers::LocalProvider::new(
            credentials.clone(),
        )),
        #[cfg(feature = "s3")]
        Arc::new(plenora_storage_s3::S3Provider::new(credentials.clone())),
        #[cfg(feature = "sftp")]
        Arc::new(plenora_storage_sftp::SftpProvider::new(credentials.clone())),
        #[cfg(feature = "ftp")]
        Arc::new(plenora_storage_ftp::FtpProvider::new(credentials.clone())),
        #[cfg(feature = "ftps")]
        Arc::new(plenora_storage_ftp::FtpProvider::new_ftps(
            credentials.clone(),
        )),
        #[cfg(feature = "azure")]
        Arc::new(plenora_storage_providers::AzureProvider::new(
            credentials.clone(),
        )),
        #[cfg(feature = "gcs")]
        Arc::new(plenora_storage_providers::GcsProvider::new(
            credentials.clone(),
        )),
        #[cfg(feature = "smb")]
        Arc::new(plenora_storage_providers::SmbProvider::new(
            credentials.clone(),
        )),
        #[cfg(feature = "webdav")]
        Arc::new(plenora_storage_providers::WebDavProvider::new(
            credentials.clone(),
        )),
    ];
    drop(credentials);
    providers
}

mod files;
pub use files::{PutFileOptions, get_to_file, put_from_file};
