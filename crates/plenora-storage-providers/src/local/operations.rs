//! Backend operations; connection and authentication remain in the parent.

use super::{
    BTreeMap, Backend, Bytes, ErrorPhase, LocalBackend, LocalReader, ObjectMetadata,
    ProviderListRequest, ProviderListResult, PutRequest, Reader, StorageResult, async_trait,
    blocking, directory_may_contain, invalid, io_error, limit_error, metadata, page, portable_key,
    select,
};

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
        super::publication::put(self, request, data).await
    }
    async fn put_file(
        &mut self,
        request: &PutRequest,
        file: tokio::fs::File,
        size: u64,
    ) -> StorageResult<()> {
        super::publication::put_file(self, request, file, size).await
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
