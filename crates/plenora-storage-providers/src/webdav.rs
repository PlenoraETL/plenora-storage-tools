mod operations;
mod publication;

use crate::{
    common::{
        Backend, ProviderFactory, Reader, failure, invalid, limit_error, metadata, page, parse,
        select, transport_failure,
    },
    http,
};
use async_trait::async_trait;
use bytes::Bytes;
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect,
    RetryDisposition, StorageError, StorageResult, directory_may_contain, validate_object_key,
};
use reqwest::{Client, Method, RequestBuilder, Response, StatusCode};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use url::Url;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// `WebDAV` collection root; publication guarantees depend on the qualified server profile.
pub struct WebDavConnectionConfig {
    /// Collection URL ending in `/`, without credentials, query or fragment.
    /// Plaintext HTTP requires engine opt-in; redirects and proxies are disabled.
    pub endpoint: String,
}
/// `WebDAV` backend factory using bearer or basic authentication over an admitted endpoint.
pub struct WebDav;
#[async_trait]
impl ProviderFactory for WebDav {
    const ID: &'static str = "webdav";
    const CONTRACT: &'static str = "plenora-storage-webdav-connection-v1";
    const ATOMIC: bool = false;
    const SPOOLED_PUT: bool = true;
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
        let connector = http::Connector::new(&root, x).await?;
        let client = connector
            .client()
            .map_err(|error| transport_failure(&error, ErrorPhase::Connect, false))?;
        let upload = connector
            .upload_client()
            .map_err(|error| transport_failure(&error, ErrorPhase::Connect, false))?;
        let credential = credentials.resolve(&c.credential_ref)?;
        let auth = if let Some(token) = credential.optional("bearer_token") {
            Auth::Bearer(token.to_owned())
        } else {
            Auth::Basic(
                credential.required("username")?.to_owned(),
                credential.required("password")?.to_owned(),
            )
        };
        Ok(Box::new(Dav {
            root,
            client,
            upload,
            auth,
        }))
    }
}
enum Auth {
    Basic(String, String),
    Bearer(String),
}
struct Dav {
    root: Url,
    client: Client,
    /// Used for requests that upload a body (see `Connector::upload_client`).
    upload: Client,
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
            .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))
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
        let client = if method == Method::PUT {
            &self.upload
        } else {
            &self.client
        };
        let request = client.request(method, url);
        match &self.auth {
            Auth::Basic(user, password) => request.basic_auth(user, Some(password)),
            Auth::Bearer(token) => request.bearer_auth(token),
        }
    }
    async fn properties(&self, key: &str, depth: &str) -> StorageResult<Vec<DavEntry>> {
        let response = self.request(Method::from_bytes(b"PROPFIND").map_err(|_| invalid("METHOD_INVALID"))?,self.url(key)?)
            .header("Depth",depth).header("Content-Type","application/xml")
            .body("<?xml version=\"1.0\"?><d:propfind xmlns:d=\"DAV:\"><d:prop><d:resourcetype/><d:getcontentlength/><d:getetag/></d:prop></d:propfind>")
            .send().await.map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
        let mut response = checked(response, false)?;
        if response.status() != StatusCode::MULTI_STATUS {
            return Err(failure(ErrorCategory::Protocol, ErrorPhase::Read, false));
        }
        let mut data = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?
        {
            if data.len().saturating_add(chunk.len()) > 8 * 1024 * 1024 {
                return Err(limit_error());
            }
            data.extend_from_slice(&chunk);
        }
        parse_properties(&self.root, &data)
    }
}

fn parse_properties(root: &Url, data: &[u8]) -> StorageResult<Vec<DavEntry>> {
    let MultiStatusDocument::MultiStatus(parsed) = quick_xml::de::from_reader(data)
        .map_err(|_| failure(ErrorCategory::Protocol, ErrorPhase::Read, false))?;
    let root_path = decoded(root.path())?;
    let mut entries = Vec::new();
    for item in parsed.responses {
        let url = root
            .join(&item.href)
            .map_err(|_| invalid("WEBDAV_HREF_INVALID"))?;
        if url.origin() != root.origin()
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

#[cfg(test)]
#[path = "webdav_parser_tests.rs"]
mod parser_tests;

#[cfg(test)]
#[path = "webdav_status_tests.rs"]
mod status_tests;

#[cfg(fuzzing)]
pub(crate) fn fuzz_properties(data: &[u8]) {
    let root = Url::parse("https://fixture.invalid/storage/").unwrap();
    if let Ok(entries) = parse_properties(&root, data) {
        for entry in entries {
            assert!(entry.key.is_empty() || validate_object_key(&entry.key).is_ok());
            if let Some(etag) = entry.etag {
                assert!(etag.starts_with('"') && etag.ends_with('"'));
                assert!(!etag.contains(['\r', '\n']));
            }
        }
    }
}
#[derive(Deserialize)]
enum MultiStatusDocument {
    #[serde(rename = "multistatus")]
    MultiStatus(MultiStatus),
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
/// Classifies an HTTP failure. Only 401 rejects the credentials. 429 and
/// 502-504 are transient refusals: without a mutation nothing happened, so
/// the retry is safe; a mutation keeps an unknown effect. Before 3.0.0 they
/// were `protocol` with retry `never`, the same class of error as an FTP login
/// refused under load being reported as rejected credentials.
fn status_error(status: u16, mutating: bool) -> StorageError {
    let phase = if mutating {
        ErrorPhase::Commit
    } else {
        ErrorPhase::Read
    };
    let category = match status {
        401 => ErrorCategory::Authentication,
        403 => ErrorCategory::Authorization,
        404 => ErrorCategory::NotFound,
        409 | 412 => ErrorCategory::Conflict,
        405 | 501 => ErrorCategory::Unsupported,
        429 | 502..=504 if !mutating => {
            return StorageError::new(
                ErrorCategory::Transient,
                phase,
                RemoteEffect::None,
                RetryDisposition::Safe,
                "WEBDAV_TEMPORARILY_UNAVAILABLE",
                "WebDAV server is temporarily unavailable",
            );
        }
        429 | 502..=504 => ErrorCategory::Transient,
        _ => ErrorCategory::Protocol,
    };
    failure(category, phase, mutating)
}

fn checked(response: Response, mutating: bool) -> StorageResult<Response> {
    let status = response.status();
    if status.is_success()
        && status != StatusCode::ACCEPTED
        && !(mutating && status == StatusCode::MULTI_STATUS)
    {
        return Ok(response);
    }
    Err(status_error(status.as_u16(), mutating))
}
