//! GCS JSON API. OAuth tokens are supplied/refreshed by the host resolver.
use crate::{
    common::{
        Backend, ProviderFactory, Reader, failure, invalid, limit_error, page, parse, select,
    },
    http,
};
use async_trait::async_trait;
use bytes::Bytes;
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, StorageResult,
};
use reqwest::{Client, Method, RequestBuilder, Response};
use serde::{Deserialize, de::DeserializeOwned};
use std::collections::{BTreeMap, BTreeSet};
use url::Url;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GcsConnectionConfig {
    pub endpoint: String,
    pub bucket: String,
}
pub struct Gcs;
#[async_trait]
impl ProviderFactory for Gcs {
    const ID: &'static str = "gcs";
    const CONTRACT: &'static str = "plenora-storage-gcs-connection-v1";
    const ATOMIC: bool = true;
    const METADATA: bool = true;
    fn validate(connection: &ProviderConnection, policy: &EngineConfig) -> StorageResult<()> {
        let cfg: GcsConnectionConfig = parse(connection)?;
        let url = http::endpoint(&cfg.endpoint, policy)?;
        if url.path() != "/"
            || cfg.bucket.is_empty()
            || cfg.bucket.len() > 255
            || !cfg
                .bucket
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
        {
            return Err(invalid("GCS_CONFIG_INVALID"));
        }
        Ok(())
    }
    async fn connect(
        connection: &ProviderConnection,
        credentials: &dyn CredentialResolver,
        context: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        let cfg: GcsConnectionConfig = parse(connection)?;
        let root = http::endpoint(&cfg.endpoint, context.policy)?;
        let client = http::Connector::new(&root, context)
            .await?
            .client()
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Connect, false))?;
        let material = credentials.resolve(&connection.credential_ref)?;
        Ok(Box::new(GcsBackend {
            root,
            client,
            bucket: cfg.bucket,
            token: material.required("bearer_token")?.to_owned(),
        }))
    }
}
struct GcsBackend {
    root: Url,
    client: Client,
    bucket: String,
    token: String,
}
impl GcsBackend {
    fn url(&self, key: Option<&str>, upload: bool) -> StorageResult<Url> {
        let mut url = self.root.clone();
        {
            let mut parts = url
                .path_segments_mut()
                .map_err(|()| invalid("GCS_ENDPOINT_INVALID"))?;
            parts.clear();
            if upload {
                parts.push("upload");
            }
            parts.extend(["storage", "v1", "b", &self.bucket, "o"]);
            if let Some(key) = key {
                parts.push(key);
            }
        }
        Ok(url)
    }
    fn request(&self, method: Method, url: Url) -> RequestBuilder {
        self.client.request(method, url).bearer_auth(&self.token)
    }
    async fn object(&self, key: &str) -> StorageResult<Object> {
        let response = send(
            self.request(Method::GET, self.url(Some(key), false)?),
            false,
        )
        .await?;
        let object: Object = json(response).await?;
        if object.name != key {
            return Err(invalid("GCS_OBJECT_NAME_MISMATCH"));
        }
        Ok(object)
    }
}
#[derive(Deserialize)]
struct Object {
    name: String,
    size: String,
    generation: String,
    etag: Option<String>,
    updated: Option<String>,
}
impl Object {
    fn metadata(self) -> StorageResult<ObjectMetadata> {
        plenora_storage_core::validate_object_key(&self.name)?;
        Ok(ObjectMetadata {
            key: self.name,
            size: self.size.parse().map_err(|_| invalid("GCS_SIZE_INVALID"))?,
            last_modified: self.updated,
            etag: self.etag,
            version: Some(self.generation),
        })
    }
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ObjectPage {
    #[serde(default)]
    items: Vec<Object>,
    next_page_token: Option<String>,
}
struct GcsReader {
    response: Response,
}
#[async_trait]
impl Reader for GcsReader {
    async fn next(&mut self) -> StorageResult<Option<Bytes>> {
        self.response
            .chunk()
            .await
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Read, false))
    }
}
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
        let mut url = self.url(None, true)?;
        url.query_pairs_mut().append_pair("uploadType", "multipart");
        if !request.overwrite {
            url.query_pairs_mut().append_pair("ifGenerationMatch", "0");
        }
        let boundary = loop {
            let candidate = crate::keys::stage_name();
            if !data
                .windows(candidate.len())
                .any(|window| window == candidate.as_bytes())
            {
                break candidate;
            }
        };
        let expected_size = data.len() as u64;
        let content_type = request
            .content_type
            .as_deref()
            .unwrap_or("application/octet-stream");
        let meta = serde_json::json!({"name":request.key,"contentType":content_type,"metadata":request.metadata});
        let start = Bytes::from(format!(
            "--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
        ));
        let end = Bytes::from(format!("\r\n--{boundary}--\r\n"));
        let length = start.len() + data.len() + end.len();
        let body = reqwest::Body::wrap_stream(futures_util::stream::iter([
            Ok::<_, std::io::Error>(start),
            Ok(data),
            Ok(end),
        ]));
        let response = send(
            self.request(Method::POST, url)
                .header(
                    "Content-Type",
                    format!("multipart/related; boundary={boundary}"),
                )
                .header("Content-Length", length)
                .body(body),
            true,
        )
        .await?;
        let object: Object = json(response)
            .await
            .map_err(|error| error.cleanup_unconfirmed("upload_response_invalid"))?;
        if object.name != request.key || object.size.parse::<u64>().ok() != Some(expected_size) {
            return Err(
                invalid("GCS_OBJECT_NAME_MISMATCH").cleanup_unconfirmed("upload_response_invalid")
            );
        }
        Ok(())
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
async fn send(request: RequestBuilder, mutating: bool) -> StorageResult<Response> {
    let response = request.send().await.map_err(|_| {
        failure(
            ErrorCategory::Io,
            if mutating {
                ErrorPhase::Commit
            } else {
                ErrorPhase::Read
            },
            mutating,
        )
    })?;
    if response.status().is_success() {
        return Ok(response);
    }
    let category = match response.status().as_u16() {
        401 => ErrorCategory::Authentication,
        403 => ErrorCategory::Authorization,
        404 => ErrorCategory::NotFound,
        409 | 412 => ErrorCategory::Conflict,
        _ => ErrorCategory::Protocol,
    };
    Err(failure(
        category,
        if mutating {
            ErrorPhase::Commit
        } else {
            ErrorPhase::Read
        },
        mutating,
    ))
}
async fn json<T: DeserializeOwned>(mut response: Response) -> StorageResult<T> {
    let mut data = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Read, false))?
    {
        if data.len().saturating_add(chunk.len()) > 8 * 1024 * 1024 {
            return Err(limit_error());
        }
        data.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&data)
        .map_err(|_| failure(ErrorCategory::Protocol, ErrorPhase::Read, false))
}
