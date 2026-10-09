//! Check raw list keys before `object_store::Path` can strip their slashes.

use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::client::{HttpError, HttpErrorKind, HttpRequest, HttpResponse, HttpService};
use plenora_storage_core::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError, StorageResult,
    validate_object_key,
};
use serde::Deserialize;

use crate::{PROVIDER_ID, unrepresentable_key_error};

// A normal ListObjectsV2 page has at most 1,000 entries. This also bounds
// memory when a nonconforming endpoint sends an oversized XML response.
const MAX_LIST_RESPONSE_BYTES: usize = 32 * 1_024 * 1_024;

/// The HTTP service `object_store` runs S3 requests on: every request goes
/// through `watched` with the operation's inactivity limit.
#[derive(Debug)]
pub struct ValidatingClient {
    pub client: reqwest::Client,
    pub idle: Option<std::time::Duration>,
}

#[async_trait]
impl HttpService for ValidatingClient {
    async fn call(&self, request: HttpRequest) -> Result<HttpResponse, HttpError> {
        let is_listing = request.method().as_str() == "GET"
            && request.uri().query().is_some_and(|query| {
                url::form_urlencoded::parse(query.as_bytes())
                    .any(|(name, value)| name == "list-type" && value == "2")
            });
        let response = crate::watched::call(&self.client, self.idle, request).await?;
        if !is_listing || !response.status().is_success() {
            return Ok(response);
        }
        let (parts, body) = response.into_parts();
        let mut stream = body.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            if chunk.len() > MAX_LIST_RESPONSE_BYTES - bytes.len() {
                return Err(validation_error(
                    StorageError::new(
                        ErrorCategory::ResourceLimit,
                        ErrorPhase::Read,
                        RemoteEffect::None,
                        RetryDisposition::Never,
                        "S3_LIST_RESPONSE_TOO_LARGE",
                        "S3 list response exceeds the in-memory response limit",
                    )
                    .with_provider(PROVIDER_ID),
                ));
            }
            bytes.extend_from_slice(&chunk);
        }
        validate_list_response(&bytes).map_err(validation_error)?;
        Ok(HttpResponse::from_parts(parts, bytes.into()))
    }
}

fn validation_error(error: StorageError) -> HttpError {
    HttpError::new(HttpErrorKind::Decode, error)
}

#[derive(Deserialize)]
enum ListDocument {
    ListBucketResult(RawList),
}

#[derive(Deserialize)]
struct RawList {
    #[serde(rename = "Contents", default)]
    contents: Vec<RawObject>,
    #[serde(rename = "EncodingType", default)]
    encoding_type: Option<String>,
}

#[derive(Deserialize)]
struct RawObject {
    #[serde(rename = "Key")]
    key: String,
}

fn validate_list_response(bytes: &[u8]) -> StorageResult<()> {
    let ListDocument::ListBucketResult(listing) =
        quick_xml::de::from_reader(bytes).map_err(|_| {
            StorageError::new(
                ErrorCategory::Protocol,
                ErrorPhase::Read,
                RemoteEffect::None,
                RetryDisposition::Never,
                "S3_LIST_RESPONSE_INVALID",
                "S3 list response is invalid",
            )
            .with_provider(PROVIDER_ID)
        })?;
    // The client did not request URL-encoded keys and its path layer does not
    // decode them. Never publish an unexpectedly encoded name as a literal key.
    if listing.encoding_type.is_some() {
        return Err(unrepresentable_key_error());
    }
    for object in listing.contents {
        validate_object_key(&object.key).map_err(|_| unrepresentable_key_error())?;
    }
    Ok(())
}

#[cfg(fuzzing)]
pub(crate) fn fuzz_listing(bytes: &[u8]) {
    if let Err(error) = validate_list_response(bytes) {
        assert!(matches!(
            error.code.as_str(),
            "S3_LIST_RESPONSE_INVALID" | "OBJECT_KEY_UNREPRESENTABLE"
        ));
        assert!(error.details.is_empty());
    }
}

#[cfg(test)]
#[path = "list_validation_tests.rs"]
mod tests;
