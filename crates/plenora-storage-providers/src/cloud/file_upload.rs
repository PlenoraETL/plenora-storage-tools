//! Conditional Put Blob from a private file, bounded by the service's single-PUT limit.

use super::{
    AzureConnectionConfig, ErrorCategory, ErrorPhase, PutRequest, StorageResult, failure, http,
    invalid, transport_failure,
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use hmac::{Hmac, KeyInit, Mac};
use plenora_storage_core::CredentialMaterial;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use sha2::Sha256;
use std::collections::BTreeMap;
use url::Url;

// REST version 2019-12-12 and newer accepts at most 5,000 MiB per Put Blob.
const MAX_SINGLE_PUT: u64 = 5_000 * 1024 * 1024;

pub(super) struct FileUpload {
    connector: http::Connector,
    root: Url,
    account: String,
    container: String,
    token: Option<String>,
    key: Option<String>,
}

impl FileUpload {
    pub(super) fn new(
        config: &AzureConnectionConfig,
        connector: &http::Connector,
        material: &CredentialMaterial,
    ) -> StorageResult<Self> {
        let token = material.optional("bearer_token").map(str::to_owned);
        let key = if token.is_none() {
            Some(material.required("account_key")?.to_owned())
        } else {
            None
        };
        Ok(Self {
            connector: connector.clone(),
            root: Url::parse(&config.endpoint).map_err(|_| invalid("ENDPOINT_INVALID"))?,
            account: config.account.clone(),
            container: config.container.clone(),
            token,
            key,
        })
    }

    pub(super) async fn put(
        &self,
        request: &PutRequest,
        file: tokio::fs::File,
        size: u64,
    ) -> StorageResult<()> {
        if size > MAX_SINGLE_PUT {
            return Err(crate::common::limit_error());
        }
        // Buffered and read-only operations use object_store's client. Build this
        // separate transport only for a prepared upload, retaining the pinned DNS.
        let client = self
            .connector
            .client()
            .map_err(|error| transport_failure(&error, ErrorPhase::Connect, false))?;
        let mut url = self.root.clone();
        {
            let mut path = url
                .path_segments_mut()
                .map_err(|()| invalid("ENDPOINT_INVALID"))?;
            path.pop_if_empty().push(&self.container);
            for segment in request.key.split('/') {
                path.push(segment);
            }
        }
        let mut headers = HeaderMap::new();
        insert(&mut headers, "content-length", &size.to_string())?;
        insert(&mut headers, "x-ms-version", "2023-11-03")?;
        insert(&mut headers, "x-ms-blob-type", "BlockBlob")?;
        let date_format = time::format_description::parse_borrowed::<2>("[weekday repr:short], [day padding:zero] [month repr:short] [year] [hour]:[minute]:[second] GMT")
            .map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
        let date = time::OffsetDateTime::now_utc()
            .format(&date_format)
            .map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
        insert(&mut headers, "x-ms-date", &date)?;
        if !request.overwrite {
            insert(&mut headers, "if-none-match", "*")?;
        }
        if let Some(content_type) = &request.content_type {
            insert(&mut headers, "x-ms-blob-content-type", content_type)?;
        }
        for (name, value) in &request.metadata {
            insert(&mut headers, &format!("x-ms-meta-{name}"), value)?;
        }
        let authorization = if let Some(token) = &self.token {
            format!("Bearer {token}")
        } else {
            let key = STANDARD
                .decode(
                    self.key
                        .as_deref()
                        .ok_or_else(|| invalid("AZURE_SIGNING_FAILED"))?,
                )
                .map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
            let mut mac = Hmac::<Sha256>::new_from_slice(&key)
                .map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
            mac.update(canonical(&self.account, &url, &headers)?.as_bytes());
            format!(
                "SharedKey {}:{}",
                self.account,
                STANDARD.encode(mac.finalize().into_bytes())
            )
        };
        let mut authorization =
            HeaderValue::from_str(&authorization).map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
        authorization.set_sensitive(true);
        headers.insert("authorization", authorization);
        let request = client
            .put(url)
            .headers(headers)
            .body(reqwest::Body::wrap_stream(
                tokio_util::io::ReaderStream::new(file),
            ));
        let response = crate::watched::send(request, self.connector.idle())
            .await
            .map_err(|error| transport_failure(&*error, ErrorPhase::Commit, true))?;
        let category = match response.status().as_u16() {
            201 => return Ok(()),
            401 => ErrorCategory::Authentication,
            403 => ErrorCategory::Authorization,
            404 => ErrorCategory::NotFound,
            409 | 412 => ErrorCategory::Conflict,
            _ => ErrorCategory::Protocol,
        };
        // Never read the error body: endpoints, payloads and provider messages
        // cannot become public diagnostics, even after a lost commit response.
        Err(failure(category, ErrorPhase::Commit, true))
    }
}

fn insert(headers: &mut HeaderMap, name: &str, value: &str) -> StorageResult<()> {
    let name =
        HeaderName::from_bytes(name.as_bytes()).map_err(|_| invalid("PUT_METADATA_INVALID"))?;
    let value = HeaderValue::from_str(value).map_err(|_| invalid("PUT_METADATA_INVALID"))?;
    if headers.insert(name, value).is_some() {
        return Err(invalid("PUT_METADATA_INVALID"));
    }
    Ok(())
}

fn canonical(account: &str, url: &Url, headers: &HeaderMap) -> StorageResult<String> {
    let mut value = "PUT\n".to_owned();
    for name in [
        "content-encoding",
        "content-language",
        "content-length",
        "content-md5",
        "content-type",
        "date",
        "if-modified-since",
        "if-match",
        "if-none-match",
        "if-unmodified-since",
        "range",
    ] {
        let part = headers
            .get(name)
            .map_or(Ok(""), |value| value.to_str())
            .map_err(|_| invalid("AZURE_SIGNING_FAILED"))?;
        if !(name == "content-length" && part == "0") {
            value.push_str(part);
        }
        value.push('\n');
    }
    let mut azure = BTreeMap::new();
    for (name, part) in headers {
        if name.as_str().starts_with("x-ms-") {
            azure.insert(
                name.as_str(),
                canonical_header(part.to_str().map_err(|_| invalid("AZURE_SIGNING_FAILED"))?),
            );
        }
    }
    for (name, part) in azure {
        value.push_str(name);
        value.push(':');
        value.push_str(&part);
        value.push('\n');
    }
    value.push('/');
    value.push_str(account);
    value.push_str(url.path());
    // This path deliberately issues only Put Blob, without query parameters.
    if url.query().is_some() {
        return Err(invalid("AZURE_SIGNING_FAILED"));
    }
    Ok(value)
}

fn canonical_header(raw: &str) -> String {
    // Azure folds linear whitespace outside quoted strings. Escaped quotes
    // inside a quoted value must not change the folding state.
    let mut result = String::with_capacity(raw.len());
    let (mut quoted, mut escaped, mut space) = (false, false, false);
    for character in raw.chars() {
        if !quoted && matches!(character, ' ' | '\t') {
            space = !result.is_empty();
            continue;
        }
        if space {
            result.push(' ');
            space = false;
        }
        result.push(character);
        if escaped {
            escaped = false;
        } else if quoted && character == '\\' {
            escaped = true;
        } else if character == '"' {
            quoted = !quoted;
        }
    }
    result
}

#[cfg(test)]
#[path = "file_upload_tests.rs"]
mod tests;
