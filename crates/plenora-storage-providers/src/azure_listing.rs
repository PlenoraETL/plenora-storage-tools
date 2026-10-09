//! Validate raw Azure names before `object_store` normalizes them into paths.
use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::client::{HttpError, HttpErrorKind, HttpRequest, HttpResponse, HttpService};
use plenora_storage_core::{StorageResult, validate_object_key};
use serde::Deserialize;

/// The HTTP service `object_store` runs Azure requests on: every request goes
/// through `crate::watched` with the operation's inactivity limit.
#[derive(Debug)]
pub struct ValidatingClient {
    pub client: reqwest::Client,
    pub idle: Option<std::time::Duration>,
}

#[async_trait]
impl HttpService for ValidatingClient {
    async fn call(&self, request: HttpRequest) -> Result<HttpResponse, HttpError> {
        let listing = request.method().as_str() == "GET"
            && request.uri().query().is_some_and(|query| {
                url::form_urlencoded::parse(query.as_bytes())
                    .any(|(name, value)| name == "comp" && value == "list")
            });
        let response = crate::watched::call(&self.client, self.idle, request).await?;
        if !listing || !response.status().is_success() {
            return Ok(response);
        }
        let (parts, body) = response.into_parts();
        let mut stream = body.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            if chunk.len() > 32 * 1024 * 1024 - bytes.len() {
                return Err(HttpError::new(
                    HttpErrorKind::Decode,
                    crate::common::limit_error(),
                ));
            }
            bytes.extend_from_slice(&chunk);
        }
        validate(&bytes).map_err(|error| HttpError::new(HttpErrorKind::Decode, error))?;
        Ok(HttpResponse::from_parts(parts, bytes.into()))
    }
}

#[derive(Deserialize)]
enum ListingDocument {
    EnumerationResults(Listing),
}

#[derive(Deserialize)]
struct Listing {
    #[serde(rename = "Blobs", default)]
    blobs: Blobs,
}
#[derive(Default, Deserialize)]
struct Blobs {
    #[serde(rename = "Blob", default)]
    objects: Vec<Blob>,
}
#[derive(Deserialize)]
struct Blob {
    #[serde(rename = "Name")]
    name: Name,
}
#[derive(Deserialize)]
struct Name {
    #[serde(rename = "$text", default)]
    text: String,
    #[serde(rename = "@Encoded", default)]
    encoded: Option<String>,
}
fn validate(bytes: &[u8]) -> StorageResult<()> {
    let ListingDocument::EnumerationResults(listing) = quick_xml::de::from_reader(bytes)
        .map_err(|_| crate::common::invalid("AZURE_LIST_RESPONSE_INVALID"))?;
    for blob in listing.blobs.objects {
        if blob.name.encoded.is_some() {
            return Err(crate::common::invalid("OBJECT_KEY_UNREPRESENTABLE"));
        }
        validate_object_key(&blob.name.text)
            .map_err(|_| crate::common::invalid("OBJECT_KEY_UNREPRESENTABLE"))?;
    }
    Ok(())
}

#[cfg(fuzzing)]
pub(crate) fn fuzz_listing(bytes: &[u8]) {
    if let Err(error) = validate(bytes) {
        assert!(matches!(
            error.code.as_str(),
            "AZURE_LIST_RESPONSE_INVALID" | "OBJECT_KEY_UNREPRESENTABLE"
        ));
        assert!(error.details.is_empty());
    }
}

#[cfg(test)]
#[path = "azure_listing_tests.rs"]
mod tests;
