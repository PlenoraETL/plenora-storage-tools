use crate::common::{
    Backend, ProviderFactory, Reader, failure, invalid, io_error, limit_error, metadata, page,
    parse, select,
};
use async_trait::async_trait;
use bytes::Bytes;
use cap_std::fs::{Dir, OpenOptions};
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect,
    RetryDisposition, StorageResult, directory_may_contain, validate_object_key,
};
use serde::Deserialize;
use std::{collections::BTreeMap, io::Write, path::Path, sync::Arc};
use tokio::io::AsyncReadExt;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalConnectionConfig {
    pub root: String,
}
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
#[async_trait]
impl Backend for LocalBackend {
    async fn test(&mut self) -> StorageResult<()> {
        let dir = self.dir.clone();
        blocking(move || {
            dir.entries()
                .map_err(|error| io_error(&error, ErrorPhase::Probe, false))?;
            Ok(())
        })
        .await
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let dir = self.dir.clone();
        let request = request.clone();
        blocking(move || {
            let mut stack = vec![String::new()];
            let mut selected = BTreeMap::new();
            let mut scanned = 0_usize;
            while let Some(prefix) = stack.pop() {
                let current = if prefix.is_empty() {
                    dir.try_clone()
                } else {
                    dir.open_dir(&prefix)
                }
                .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
                for entry in current
                    .entries()
                    .map_err(|error| io_error(&error, ErrorPhase::Read, false))?
                {
                    scanned += 1;
                    if scanned > 1_000_000 {
                        return Err(limit_error());
                    }
                    let entry = entry.map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
                    let name = entry
                        .file_name()
                        .into_string()
                        .map_err(|_| invalid("LOCAL_NAME_NOT_UTF8"))?;
                    if name.starts_with(".plenora-stage-") {
                        continue;
                    }
                    let key = if prefix.is_empty() {
                        name
                    } else {
                        format!("{prefix}/{name}")
                    };
                    portable_key(&key)?;
                    let ty = entry
                        .file_type()
                        .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
                    if ty.is_symlink() {
                        return Err(invalid("LOCAL_SYMLINK_FORBIDDEN"));
                    }
                    if ty.is_dir() {
                        if directory_may_contain(
                            &key,
                            request.prefix.as_deref().unwrap_or_default(),
                        ) {
                            stack.push(key);
                        }
                    } else if ty.is_file() {
                        let size = entry
                            .metadata()
                            .map_err(|error| io_error(&error, ErrorPhase::Read, false))?
                            .len();
                        select(&mut selected, metadata(&key, size), &request, limit)?;
                    }
                }
            }
            Ok(page(selected, limit))
        })
        .await
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        portable_key(key)?;
        let key = key.to_owned();
        let dir = self.dir.clone();
        blocking(move || {
            let meta = dir
                .metadata(&key)
                .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
            if !meta.is_file() {
                return Err(invalid("LOCAL_REGULAR_FILE_REQUIRED"));
            }
            Ok(metadata(&key, meta.len()))
        })
        .await
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        portable_key(key)?;
        let key = key.to_owned();
        let dir = self.dir.clone();
        let (meta, file) = blocking(move || {
            let file = dir
                .open(&key)
                .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
            let meta = file
                .metadata()
                .map_err(|error| io_error(&error, ErrorPhase::Read, false))?;
            if !meta.is_file() {
                return Err(invalid("LOCAL_REGULAR_FILE_REQUIRED"));
            }
            Ok((metadata(&key, meta.len()), file.into_std()))
        })
        .await?;
        Ok((
            meta,
            Box::new(LocalReader {
                file: tokio::fs::File::from_std(file),
            }),
        ))
    }
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()> {
        portable_key(&request.key)?;
        let key = request.key.clone();
        let overwrite = request.overwrite;
        let dir = self.dir.clone();
        blocking(move || {
            let (parent, name) = key.rsplit_once('/').unwrap_or(("", key.as_str()));
            let mut prepared = false;
            if !parent.is_empty() && dir.open_dir(parent).is_err() {
                prepared = true;
                dir.create_dir_all(parent).map_err(|error| {
                    io_error(&error, ErrorPhase::Prepare, true).with_preparation_effect(true)
                })?;
            }
            // Keep an open parent capability throughout staging and publication.
            let parent = if parent.is_empty() {
                dir.try_clone()
            } else {
                dir.open_dir(parent)
            }
            .map_err(|error| {
                io_error(&error, ErrorPhase::Prepare, false).with_preparation_effect(prepared)
            })?;
            let stage = stage_name();
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            let mut owned = false;
            let result: StorageResult<()> = (|| {
                let mut file = parent
                    .open_with(&stage, &options)
                    .map_err(|error| io_error(&error, ErrorPhase::Prepare, true))?;
                owned = true;
                file.write_all(&data)
                    .map_err(|error| io_error(&error, ErrorPhase::Write, true))?;
                file.sync_all()
                    .map_err(|error| io_error(&error, ErrorPhase::Write, true))?;
                drop(file);
                if overwrite {
                    parent
                        .rename(&stage, &parent, name)
                        .map_err(|error| io_error(&error, ErrorPhase::Commit, true))?;
                } else {
                    parent
                        .hard_link(&stage, &parent, name)
                        .map_err(|error| io_error(&error, ErrorPhase::Commit, true))?;
                    parent.remove_file(&stage).map_err(|error| {
                        io_error(&error, ErrorPhase::Cleanup, true).with_outcome(
                            RemoteEffect::Committed,
                            RetryDisposition::RequiresRecovery,
                        )
                    })?;
                }
                Ok(())
            })();
            if let Err(error) = result {
                // Only the unique staging name belongs to this operation.
                let cleaned = owned && parent.remove_file(&stage).is_ok();
                return Err(
                    if cleaned && error.remote_effect != RemoteEffect::Committed {
                        error.rolled_back()
                    } else {
                        error
                    }
                    .with_preparation_effect(prepared),
                );
            }
            Ok(())
        })
        .await
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()> {
        portable_key(key)?;
        let dir = self.dir.clone();
        let key = key.to_owned();
        blocking(move || {
            dir.remove_file(key)
                .map_err(|error| io_error(&error, ErrorPhase::Commit, true))
        })
        .await
    }
}
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> StorageResult<T> + Send + 'static,
) -> StorageResult<T> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|_| failure(ErrorCategory::Internal, ErrorPhase::Commit, true))?
}
pub fn stage_name() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NONCE: AtomicU64 = AtomicU64::new(0);
    let time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!(
        ".plenora-stage-{}-{time}-{}",
        std::process::id(),
        NONCE.fetch_add(1, Ordering::Relaxed)
    )
}
pub fn portable_key(key: &str) -> StorageResult<()> {
    validate_object_key(key)?;
    for part in key.split('/') {
        let stem = part
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        if part.ends_with(['.', ' '])
            || part.chars().any(|connection| {
                connection.is_control()
                    || "<>:\"|?*".contains(connection)
                    || ('\u{f000}'..='\u{f0ff}').contains(&connection)
            })
            || matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.as_bytes()[3].is_ascii_digit())
            || part.starts_with(".plenora-stage-")
        {
            return Err(invalid("FILESYSTEM_KEY_INVALID"));
        }
    }
    Ok(())
}
