mod operations;
mod publication;

use crate::common::{
    Backend, ProviderFactory, Reader, failure, invalid, io_error, limit_error, metadata, page,
    parse, select,
};
use crate::keys::{portable_key, stage_name};
use async_trait::async_trait;
use bytes::Bytes;
use cap_std::fs::{Dir, OpenOptions};
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect,
    RetryDisposition, StorageResult, directory_may_contain,
};
use serde::Deserialize;
use std::{collections::BTreeMap, io::Write, path::Path, sync::Arc};
use tokio::io::AsyncReadExt;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Filesystem root accessed with process credentials through a directory capability.
pub struct LocalConnectionConfig {
    /// Absolute root, at most 4096 bytes without NUL; opened using process credentials.
    /// The connection must use `credential_ref = "local:process"`.
    pub root: String,
}
/// Local filesystem backend factory using process credentials.
pub struct Local;
#[async_trait]
impl ProviderFactory for Local {
    const ID: &'static str = "local";
    const CONTRACT: &'static str = "plenora-storage-local-connection-v1";
    const ATOMIC: bool = true;
    fn validate(connection: &ProviderConnection, _: &EngineConfig) -> StorageResult<()> {
        let cfg: LocalConnectionConfig = parse(connection)?;
        if !Path::new(&cfg.root).is_absolute()
            || cfg.root.len() > 4096
            || cfg.root.contains('\0')
            || connection.credential_ref != "local:process"
        {
            return Err(invalid("LOCAL_CONFIG_INVALID"));
        }
        Ok(())
    }
    async fn connect(
        connection: &ProviderConnection,
        _: &dyn CredentialResolver,
        _: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        let cfg: LocalConnectionConfig = parse(connection)?;
        let dir = blocking(move || {
            Dir::open_ambient_dir(cfg.root, cap_std::ambient_authority())
                .map_err(|error| io_error(&error, ErrorPhase::Connect, false))
        })
        .await?;
        Ok(Box::new(LocalBackend { dir: Arc::new(dir) }))
    }
}
struct LocalBackend {
    dir: Arc<Dir>,
}
struct LocalReader {
    file: tokio::fs::File,
}
#[async_trait]
impl Reader for LocalReader {
    async fn next(&mut self) -> StorageResult<Option<Bytes>> {
        let mut data = vec![0; 64 * 1024];
        let count = self
            .file
            .read(&mut data)
            .await
            .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
        data.truncate(count);
        Ok((count > 0).then(|| data.into()))
    }
}
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> StorageResult<T> + Send + 'static,
) -> StorageResult<T> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|_| failure(ErrorCategory::Internal, ErrorPhase::Commit, true))?
}
