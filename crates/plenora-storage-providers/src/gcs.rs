//! GCS JSON API. OAuth tokens are supplied/refreshed by the host resolver.
mod operations;
mod publication;

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
/// Non-secret GCS JSON API addressing; OAuth tokens come from the resolver.
pub struct GcsConnectionConfig {
    /// JSON API origin with path `/`, without credentials, query or fragment.
    /// Plaintext HTTP requires engine opt-in; redirects and proxies are disabled.
    pub endpoint: String,
    /// Bucket name within the configured endpoint; no embedded credentials.
    pub bucket: String,
}
/// GCS JSON API backend factory; the host supplies and refreshes bearer tokens.
pub struct Gcs;
#[async_trait]
impl ProviderFactory for Gcs {
    const ID: &'static str = "gcs";
    const CONTRACT: &'static str = "plenora-storage-gcs-connection-v1";
    const ATOMIC: bool = true;
    const SPOOLED_PUT: bool = true;
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
