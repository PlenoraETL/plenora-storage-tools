//! Backend operations; connection and authentication remain in the parent.

use super::{
    BTreeMap, BTreeSet, Backend, Bytes, Dav, DavReader, ErrorCategory, ErrorPhase, Method,
    ObjectMetadata, ProviderListRequest, ProviderListResult, PutRequest, Reader, StorageError,
    StorageResult, async_trait, checked, directory_may_contain, failure, invalid, limit_error,
    metadata, page, select, transport_failure,
};

#[async_trait]
impl Backend for Dav {
    async fn test(&mut self) -> StorageResult<()> {
        let entries = self.properties("", "0").await?;
        if !entries.iter().any(|e| e.key.is_empty() && e.directory) {
            return Err(invalid("WEBDAV_COLLECTION_REQUIRED"));
        }
        Ok(())
    }
    async fn list(
        &mut self,
        r: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let mut stack = vec![String::new()];
        let mut seen = BTreeSet::new();
        let mut selected = BTreeMap::new();
        while let Some(dir) = stack.pop() {
            if !seen.insert(dir.clone()) {
                return Err(invalid("WEBDAV_COLLECTION_CYCLE"));
            }
            if seen.len() > 100_000 {
                return Err(limit_error());
            }
            for entry in self.properties(&dir, "1").await? {
                if entry.key == dir {
                    continue;
                }
                // A Depth:1 response must contain direct children only.
                if entry.key.rsplit_once('/').map_or("", |(parent, _)| parent) != dir {
                    return Err(invalid("WEBDAV_DEPTH_VIOLATION"));
                }
                if entry.directory {
                    if directory_may_contain(&entry.key, r.prefix.as_deref().unwrap_or_default()) {
                        stack.push(entry.key);
                    }
                } else {
                    select(&mut selected, metadata(&entry.key, entry.size), r, limit)?;
                }
            }
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        self.properties(key, "0")
            .await?
            .into_iter()
            .find(|e| e.key == key && !e.directory)
            .map(|e| {
                let mut meta = metadata(key, e.size);
                meta.etag = e.etag;
                meta
            })
            .ok_or_else(|| failure(ErrorCategory::NotFound, ErrorPhase::Read, false))
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let meta = self.stat(key).await?;
        let response = self
            .request(Method::GET, self.url(key)?)
            .send()
            .await
            .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
        Ok((
            meta,
            Box::new(DavReader {
                response: checked(response, false)?,
            }),
        ))
    }
    async fn put(&mut self, r: &PutRequest, data: Bytes) -> StorageResult<()> {
        super::publication::put(self, r, data).await
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
        // Do not allow a file deletion request to recursively remove a collection.
        let meta = self.stat(key).await?;
        let etag = meta
            .etag
            .filter(|value| value.starts_with('"') && value.ends_with('"'))
            .ok_or_else(|| {
                StorageError::unsupported("WebDAV file deletion requires a strong ETag")
            })?;
        let response = self
            .request(Method::DELETE, self.url(key)?)
            .header("If-Match", etag)
            .send()
            .await
            .map_err(|error| transport_failure(&error, ErrorPhase::Commit, true))?;
        checked(response, true)?;
        Ok(())
    }
}
