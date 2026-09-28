//! Backend operations; connection and authentication remain in the parent.

use super::{
    BTreeMap, Backend, Bytes, Cloud, CloudReader, ObjectMetadata, ObjectStore, ObjectStoreExt,
    ProviderListRequest, ProviderListResult, PutRequest, Reader, StorageResult, StreamExt,
    async_trait, page, path, public_meta, select, store_error,
};

#[async_trait]
impl Backend for Cloud {
    async fn test(&mut self) -> StorageResult<()> {
        self.store
            .list(None)
            .next()
            .await
            .transpose()
            .map_err(|error| store_error(error, false))?;
        Ok(())
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let prefix = request
            .prefix
            .as_deref()
            .filter(|policy| !policy.is_empty())
            .map(|policy| path(policy.trim_end_matches('/')))
            .transpose()?;
        let mut stream = self.store.list(prefix.as_ref());
        let mut selected = BTreeMap::new();
        // Azure listing order must not be inferred from a generic store.
        // Retain only the smallest page; deadline bounds total enumeration time.
        while let Some(item) = stream.next().await {
            let item = item.map_err(|error| store_error(error, false))?;
            select(&mut selected, public_meta(item)?, request, limit)?;
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        public_meta(
            self.store
                .head(&path(key)?)
                .await
                .map_err(|error| store_error(error, false))?,
        )
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let result = self
            .store
            .get(&path(key)?)
            .await
            .map_err(|error| store_error(error, false))?;
        let meta = public_meta(result.meta.clone())?;
        Ok((
            meta,
            Box::new(CloudReader {
                stream: result.into_stream(),
            }),
        ))
    }
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()> {
        super::publication::put(self, request, data).await
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()> {
        // Some object stores return success for an absent key. Probe preserves
        // the v1 ignore_missing=false behavior (concurrent deletion is allowed).
        self.stat(key).await?;
        self.store
            .delete(&path(key)?)
            .await
            .map_err(|error| store_error(error, true))
    }
}
