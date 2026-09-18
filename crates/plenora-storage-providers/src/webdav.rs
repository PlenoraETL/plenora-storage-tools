use crate::{
    common::{
        Backend, ProviderFactory, Reader, failure, invalid, limit_error, metadata, page, parse,
        select,
    },
    http,
};
use async_trait::async_trait;
use bytes::Bytes;
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, StorageError,
    StorageResult, directory_may_contain, validate_object_key,
};
use reqwest::{Client, Method, RequestBuilder, Response, StatusCode};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use url::Url;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebDavConnectionConfig {
    pub endpoint: String,
}
pub struct WebDav;
#[async_trait]
impl ProviderFactory for WebDav {
    const ID: &'static str = "webdav";
    const CONTRACT: &'static str = "plenora-storage-webdav-connection-v1";
    const ATOMIC: bool = false;
    fn validate(c: &ProviderConnection, p: &EngineConfig) -> StorageResult<()> {
        let cfg: WebDavConnectionConfig = parse(c)?;
        let url = http::endpoint(&cfg.endpoint, p)?;
        if !url.path().ends_with('/') {
            return Err(invalid("WEBDAV_ROOT_MUST_END_WITH_SLASH"));
        }
        Ok(())
    }
    async fn connect(
        c: &ProviderConnection,
        credentials: &dyn CredentialResolver,
        x: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        let cfg: WebDavConnectionConfig = parse(c)?;
        let root = http::endpoint(&cfg.endpoint, x.policy)?;
        let client = http::Connector::new(&root, x)
            .await?
            .client()
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Connect, false))?;
        let credential = credentials.resolve(&c.credential_ref)?;
        let auth = if let Some(token) = credential.optional("bearer_token") {
            Auth::Bearer(token.to_owned())
        } else {
            Auth::Basic(
                credential.required("username")?.to_owned(),
                credential.required("password")?.to_owned(),
            )
        };
        Ok(Box::new(Dav { root, client, auth }))
    }
}
enum Auth {
    Basic(String, String),
    Bearer(String),
}
struct Dav {
    root: Url,
    client: Client,
    auth: Auth,
}
struct DavReader {
    response: Response,
}
#[async_trait]
impl Reader for DavReader {
    async fn next(&mut self) -> StorageResult<Option<Bytes>> {
        self.response
            .chunk()
            .await
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Read, false))
    }
}
impl Dav {
    fn url(&self, key: &str) -> StorageResult<Url> {
        if !key.is_empty() {
            validate_object_key(key)?;
        }
        let mut url = self.root.clone();
        if !key.is_empty() {
            let mut parts = url
                .path_segments_mut()
                .map_err(|()| invalid("WEBDAV_ENDPOINT_INVALID"))?;
            parts.pop_if_empty();
            for part in key.split('/') {
                parts.push(part);
            }
        }
        Ok(url)
    }
    fn request(&self, method: Method, url: Url) -> RequestBuilder {
        let request = self.client.request(method, url);
        match &self.auth {
            Auth::Basic(user, password) => request.basic_auth(user, Some(password)),
            Auth::Bearer(token) => request.bearer_auth(token),
        }
    }
    async fn properties(&self, key: &str, depth: &str) -> StorageResult<Vec<DavEntry>> {
        let response = self.request(Method::from_bytes(b"PROPFIND").map_err(|_| invalid("METHOD_INVALID"))?,self.url(key)?)
            .header("Depth",depth).header("Content-Type","application/xml")
            .body("<?xml version=\"1.0\"?><d:propfind xmlns:d=\"DAV:\"><d:prop><d:resourcetype/><d:getcontentlength/><d:getetag/></d:prop></d:propfind>")
            .send().await.map_err(|_| failure(ErrorCategory::Io,ErrorPhase::Read,false))?;
        let mut response = checked(response, false)?;
        if response.status() != StatusCode::MULTI_STATUS {
            return Err(failure(ErrorCategory::Protocol, ErrorPhase::Read, false));
        }
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
        let parsed: MultiStatus = quick_xml::de::from_reader(data.as_slice())
            .map_err(|_| failure(ErrorCategory::Protocol, ErrorPhase::Read, false))?;
        let root_path = decoded(self.root.path())?;
        let mut entries = Vec::new();
        for item in parsed.responses {
            let url = self
                .root
                .join(&item.href)
                .map_err(|_| invalid("WEBDAV_HREF_INVALID"))?;
            if url.origin() != self.root.origin()
                || url.query().is_some()
                || url.fragment().is_some()
                || !url.username().is_empty()
                || url.password().is_some()
            {
                return Err(invalid("WEBDAV_HREF_OUTSIDE_ROOT"));
            }
            let path = decoded(url.path())?;
            let key = path
                .strip_prefix(&root_path)
                .ok_or_else(|| invalid("WEBDAV_HREF_OUTSIDE_ROOT"))?
                .trim_end_matches('/')
                .to_owned();
            if !key.is_empty() {
                validate_object_key(&key)?;
            }
            let mut directory = false;
            let mut size = None;
            let mut etag = None;
            let mut valid = false;
            for prop in item.propstats {
                if prop.status.split_whitespace().nth(1) == Some("200") {
                    valid = true;
                    if prop.prop.etag.is_some() {
                        etag = prop.prop.etag.and_then(|value| strong_etag(&value));
                    }
                    directory |= prop
                        .prop
                        .resource_type
                        .is_some_and(|r| r.collection.is_some());
                    if let Some(length) = prop.prop.length {
                        size = Some(
                            length
                                .parse::<u64>()
                                .map_err(|_| invalid("WEBDAV_LENGTH_INVALID"))?,
                        );
                    }
                }
            }
            if !valid || (!directory && size.is_none()) {
                return Err(failure(ErrorCategory::Protocol, ErrorPhase::Read, false));
            }
            entries.push(DavEntry {
                key,
                directory,
                size: size.unwrap_or(0),
                etag,
            });
        }
        Ok(entries)
    }
}
#[derive(Deserialize)]
struct MultiStatus {
    #[serde(rename = "response", default)]
    responses: Vec<DavResponse>,
}
#[derive(Deserialize)]
struct DavResponse {
    href: String,
    #[serde(rename = "propstat", default)]
    propstats: Vec<PropStat>,
}
#[derive(Deserialize)]
struct PropStat {
    status: String,
    prop: Prop,
}
#[derive(Deserialize)]
struct Prop {
    #[serde(rename = "resourcetype")]
    resource_type: Option<ResourceType>,
    #[serde(rename = "getcontentlength")]
    length: Option<String>,
    #[serde(rename = "getetag")]
    etag: Option<String>,
}
#[derive(Deserialize)]
struct ResourceType {
    collection: Option<serde::de::IgnoredAny>,
}
struct DavEntry {
    key: String,
    directory: bool,
    size: u64,
    etag: Option<String>,
}
fn decoded(value: &str) -> StorageResult<String> {
    percent_encoding::percent_decode_str(value)
        .decode_utf8()
        .map(std::borrow::Cow::into_owned)
        .map_err(|_| invalid("WEBDAV_HREF_INVALID"))
}
fn strong_etag(value: &str) -> Option<String> {
    // Some DAV servers return the opaque tag without HTTP's surrounding quotes.
    // Accept that representation, but never upgrade a weak validator.
    if value.is_empty() || value.starts_with("W/") {
        return None;
    }
    let opaque = value
        .strip_prefix('"')
        .and_then(|s| s.strip_suffix('"'))
        .unwrap_or(value);
    if !opaque
        .bytes()
        .all(|b| b == 0x21 || (0x23..=0x7e).contains(&b))
    {
        return None;
    }
    Some(format!("\"{opaque}\""))
}
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
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Read, false))?;
        Ok((
            meta,
            Box::new(DavReader {
                response: checked(response, false)?,
            }),
        ))
    }
    async fn put(&mut self, r: &PutRequest, data: Bytes) -> StorageResult<()> {
        let mut prepared = false;
        let result = async {
            if let Some((parent, _)) = r.key.rsplit_once('/') {
                let mut current = String::new();
                for segment in parent.split('/') {
                    if !current.is_empty() {
                        current.push('/');
                    }
                    current.push_str(segment);
                    prepared = true;
                    let response = self
                        .request(
                            Method::from_bytes(b"MKCOL").map_err(|_| invalid("METHOD_INVALID"))?,
                            self.url(&current)?,
                        )
                        .send()
                        .await
                        .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Prepare, true))?;
                    if response.status() != StatusCode::METHOD_NOT_ALLOWED {
                        checked(response, true)?;
                    }
                }
            }
            let mut request = self.request(Method::PUT, self.url(&r.key)?).body(data);
            if !r.overwrite {
                request = request.header("If-None-Match", "*");
            }
            let response = request
                .send()
                .await
                .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Commit, true))?;
            checked(response, true)?;
            Ok(())
        }
        .await;
        result.map_err(|e: StorageError| e.with_preparation_effect(prepared))
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
            .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Commit, true))?;
        checked(response, true)?;
        Ok(())
    }
}
fn checked(response: Response, mutating: bool) -> StorageResult<Response> {
    let status = response.status();
    if status.is_success()
        && status != StatusCode::ACCEPTED
        && !(mutating && status == StatusCode::MULTI_STATUS)
    {
        return Ok(response);
    }
    let category = match status.as_u16() {
        401 => ErrorCategory::Authentication,
        403 => ErrorCategory::Authorization,
        404 => ErrorCategory::NotFound,
        409 | 412 => ErrorCategory::Conflict,
        405 | 501 => ErrorCategory::Unsupported,
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
