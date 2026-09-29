//! Application entry point with explicit provider selection through Cargo features.
#![forbid(unsafe_code)]
use plenora_storage_core::{
    CredentialResolver, Engine, EngineConfig, StorageProvider, StorageResult,
};
use std::sync::Arc;

/// Upload/copy preparation for local, Azure, GCS, SMB and `WebDAV` adapters.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum UploadStrategy {
    /// Preserve existing in-memory payload bounds and default behavior.
    #[default]
    Buffered,
    /// Validate the full input in a private temporary file, then send bounded chunks.
    /// Requires local temporary disk space up to the admitted transfer size.
    PrivateFile,
}

/// Build a reusable engine using only the providers compiled into this artifact.
/// No credentials are resolved and no connection is opened during construction.
///
/// # Errors
/// Returns a registration error if compiled providers have conflicting identities.
pub fn build_engine(
    config: EngineConfig,
    credentials: Arc<dyn CredentialResolver>,
) -> StorageResult<Engine> {
    build_engine_with_upload_strategy(config, credentials, UploadStrategy::Buffered)
}

/// Build an engine with explicit preparation for the five additional providers.
///
/// S3, SFTP, FTP and FTPS retain their existing transfer implementations and limits.
/// Construction does not resolve credentials or open connections.
///
/// # Errors
/// Returns provider registration or unsupported-strategy errors.
pub fn build_engine_with_upload_strategy(
    config: EngineConfig,
    credentials: Arc<dyn CredentialResolver>,
    strategy: UploadStrategy,
) -> StorageResult<Engine> {
    let mut engine = Engine::new(config);
    for provider in providers(credentials, strategy)? {
        engine.register_provider(provider)?;
    }
    Ok(engine)
}

#[cfg_attr(
    not(any(
        feature = "local",
        feature = "azure",
        feature = "gcs",
        feature = "smb",
        feature = "webdav"
    )),
    allow(
        clippy::unnecessary_wraps,
        reason = "Other feature selections use fallible private-file provider construction"
    )
)]
fn providers(
    credentials: Arc<dyn CredentialResolver>,
    strategy: UploadStrategy,
) -> StorageResult<Vec<Arc<dyn StorageProvider>>> {
    let providers: Vec<Arc<dyn StorageProvider>> = vec![
        #[cfg(feature = "local")]
        Arc::new(extended::<plenora_storage_providers::Local>(
            credentials.clone(),
            strategy,
        )?),
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
        Arc::new(extended::<plenora_storage_providers::Azure>(
            credentials.clone(),
            strategy,
        )?),
        #[cfg(feature = "gcs")]
        Arc::new(extended::<plenora_storage_providers::Gcs>(
            credentials.clone(),
            strategy,
        )?),
        #[cfg(feature = "smb")]
        Arc::new(extended::<plenora_storage_providers::Smb>(
            credentials.clone(),
            strategy,
        )?),
        #[cfg(feature = "webdav")]
        Arc::new(extended::<plenora_storage_providers::WebDav>(
            credentials.clone(),
            strategy,
        )?),
    ];
    drop(credentials);
    let _ = strategy;
    Ok(providers)
}

#[cfg(any(
    feature = "local",
    feature = "azure",
    feature = "gcs",
    feature = "smb",
    feature = "webdav"
))]
fn extended<F: plenora_storage_providers::ProviderFactory>(
    credentials: Arc<dyn CredentialResolver>,
    strategy: UploadStrategy,
) -> StorageResult<plenora_storage_providers::Provider<F>> {
    match strategy {
        UploadStrategy::Buffered => Ok(plenora_storage_providers::Provider::new(credentials)),
        UploadStrategy::PrivateFile => {
            plenora_storage_providers::Provider::with_spooled_uploads(credentials)
        }
    }
}

mod files;
pub use files::{PutFileOptions, get_to_file, put_from_file};
