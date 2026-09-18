//! Validate raw Azure names before `object_store` normalizes them into paths.
use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::client::{HttpError, HttpErrorKind, HttpRequest, HttpResponse, HttpService};
use plenora_storage_core::{StorageResult, validate_object_key};
use serde::Deserialize;

#[derive(Debug)]
pub struct ValidatingClient(pub reqwest::Client);

#[async_trait]
impl HttpService for ValidatingClient {
    async fn call(&self, request: HttpRequest) -> Result<HttpResponse, HttpError> {
        let listing = request.method().as_str() == "GET"
            && request.uri().query().is_some_and(|query| {
                url::form_urlencoded::parse(query.as_bytes())
                    .any(|(name, value)| name == "comp" && value == "list")
            });
        let response = self.0.call(request).await?;
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
    let listing: Listing = quick_xml::de::from_reader(bytes)
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

#[cfg(test)]
mod tests {
    use super::validate;
    #[test]
    fn rejects_names_that_path_normalization_would_alias() {
        for key in ["folder/", "/folder", "a//b", "a/../b", ""] {
            let xml = format!(
                "<EnumerationResults><Blobs><Blob><Name>{key}</Name></Blob></Blobs></EnumerationResults>"
            );
            assert!(validate(xml.as_bytes()).is_err());
        }
        assert!(validate(b"<EnumerationResults><Blobs><Blob><Name Encoded=\"true\">a%2Fb</Name></Blob></Blobs></EnumerationResults>").is_err());
        validate(b"<EnumerationResults><Blobs><Blob><Name>a&amp;b/c</Name></Blob></Blobs></EnumerationResults>").unwrap();
        validate(b"<EnumerationResults><Blobs/></EnumerationResults>").unwrap();
    }
}
