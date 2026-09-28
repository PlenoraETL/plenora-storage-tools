//! Backend operations; connection and authentication remain in the parent.

use super::{
    BTreeMap, BTreeSet, Backend, Bytes, GcsBackend, GcsReader, Method, ObjectMetadata, ObjectPage,
    ProviderListRequest, ProviderListResult, PutRequest, Reader, StorageResult, async_trait,
    invalid, json, page, select, send,
};

#[async_trait]
impl Backend for GcsBackend {
    async fn test(&mut self) -> StorageResult<()> {
        let mut url = self.url(None, false)?;
        url.query_pairs_mut().append_pair("maxResults", "1");
        let _: ObjectPage = json(send(self.request(Method::GET, url), false).await?).await?;
        Ok(())
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let mut token: Option<String> = None;
        let mut seen = BTreeSet::new();
        let mut selected = BTreeMap::new();
        loop {
            let mut url = self.url(None, false)?;
            {
                let mut query = url.query_pairs_mut();
                query.append_pair("maxResults", "1000");
                if let Some(prefix) = request.prefix.as_deref().filter(|p| !p.is_empty()) {
                    query.append_pair("prefix", &format!("{}/", prefix.trim_end_matches('/')));
                }
                if let Some(after) = &request.start_after {
                    query.append_pair("startOffset", after);
                }
                if let Some(token) = &token {
                    query.append_pair("pageToken", token);
                }
            }
            let page: ObjectPage = json(send(self.request(Method::GET, url), false).await?).await?;
            for object in page.items {
                select(&mut selected, object.metadata()?, request, limit)?;
            }
            token = page.next_page_token.filter(|t| !t.is_empty());
            match &token {
                None => break,
                Some(t) if t.len() > 8192 || !seen.insert(t.clone()) || seen.len() > 100_000 => {
                    return Err(invalid("GCS_PAGINATION_INVALID"));
                }
                Some(_) => {}
            }
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        self.object(key).await?.metadata()
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let meta = self.object(key).await?.metadata()?;
        let mut url = self.url(Some(key), false)?;
        url.query_pairs_mut()
            .append_pair("alt", "media")
            .append_pair("generation", meta.version.as_deref().unwrap_or_default());
        let response = send(self.request(Method::GET, url), false).await?;
        Ok((meta, Box::new(GcsReader { response })))
    }
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()> {
        super::publication::put(self, request, data).await
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()> {
        send(
            self.request(Method::DELETE, self.url(Some(key), false)?),
            true,
        )
        .await?;
        Ok(())
    }
}
