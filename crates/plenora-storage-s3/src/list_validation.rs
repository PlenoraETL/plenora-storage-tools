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

#[derive(Debug)]
pub struct ValidatingClient(pub reqwest::Client);

#[async_trait]
impl HttpService for ValidatingClient {
    async fn call(&self, request: HttpRequest) -> Result<HttpResponse, HttpError> {
        let is_listing = request.method().as_str() == "GET"
            && request.uri().query().is_some_and(|query| {
                url::form_urlencoded::parse(query.as_bytes())
                    .any(|(name, value)| name == "list-type" && value == "2")
            });
        let response = self.0.call(request).await?;
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
    let listing: RawList = quick_xml::de::from_reader(bytes).map_err(|_| {
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

#[cfg(test)]
mod tests {
    use super::validate_list_response;

    #[test]
    fn isolated_invalid_keys_are_rejected_before_normalization() {
        for key in ["folder/", "/folder", "folder//file", "folder/../file"] {
            let xml = format!(
                "<ListBucketResult><Contents><Key>{key}</Key></Contents></ListBucketResult>"
            );
            assert_eq!(
                validate_list_response(xml.as_bytes())
                    .expect_err("invalid key")
                    .code,
                "OBJECT_KEY_UNREPRESENTABLE"
            );
        }
    }

    #[test]
    fn valid_names_and_xml_entities_remain_representable() {
        for key in ["folder/file", "folder/a&amp;b", "folder/a%2Fb", "folder/雪"] {
            let xml = format!(
                "<ListBucketResult><Contents><Key>{key}</Key></Contents></ListBucketResult>"
            );
            validate_list_response(xml.as_bytes()).expect("valid key");
        }
        validate_list_response(b"<ListBucketResult/>").expect("empty listing");
    }
}
