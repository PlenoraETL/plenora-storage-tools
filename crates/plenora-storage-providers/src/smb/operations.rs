//! Backend operations; connection and authentication remain in the parent.

use super::{
    BTreeMap, Backend, Bytes, ObjectMetadata, ProviderListRequest, ProviderListResult, PutRequest,
    Reader, SmbBackend, SmbReader, StorageResult, async_trait, invalid, metadata, page, smb_error,
};

#[async_trait]
impl Backend for SmbBackend {
    async fn test(&mut self) -> StorageResult<()> {
        let path = self.path("")?;
        let info = self
            .tree
            .stat(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        if !info.is_directory {
            return Err(invalid("SMB_ROOT_NOT_DIRECTORY"));
        }
        Ok(())
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let mut stack = vec![String::new()];
        let mut selected = BTreeMap::new();
        let mut scanned = 0_usize;
        while let Some(parent) = stack.pop() {
            let path = self.path(&parent)?;
            crate::smb_listing::directory(
                &mut self.conn,
                &self.tree,
                &path,
                &parent,
                request,
                limit,
                &mut selected,
                &mut stack,
                &mut scanned,
            )
            .await?;
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        let path = self.path(key)?;
        let info = self
            .tree
            .stat(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        if info.is_directory {
            return Err(invalid("SMB_REGULAR_FILE_REQUIRED"));
        }
        Ok(metadata(key, info.size))
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let path = self.path(key)?;
        let reader = self
            .tree
            .open_file_reader(self.conn.clone(), &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        Ok((
            metadata(key, reader.size()),
            Box::new(SmbReader {
                reader: Some(reader),
                offset: 0,
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
        let path = self.path(key)?;
        self.stat(key).await?;
        self.tree
            .delete_file(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, true))
    }
}
