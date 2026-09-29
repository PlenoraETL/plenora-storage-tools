//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{Bytes, GcsBackend, Method, Object, PutRequest, StorageResult, invalid, json, send};
use futures_util::{Stream, StreamExt};
use tokio::io::{AsyncReadExt, AsyncSeekExt};

pub async fn put(provider: &GcsBackend, request: &PutRequest, data: Bytes) -> StorageResult<()> {
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
    publish(
        provider,
        request,
        boundary,
        futures_util::stream::iter([Ok(data)]),
        expected_size,
    )
    .await
}

pub async fn put_file(
    provider: &GcsBackend,
    request: &PutRequest,
    mut file: tokio::fs::File,
    size: u64,
) -> StorageResult<()> {
    let boundary = loop {
        let candidate = crate::keys::stage_name();
        if !contains_boundary(&mut file, candidate.as_bytes()).await? {
            break candidate;
        }
    };
    file.rewind()
        .await
        .map_err(|error| preparation_error(&error))?;
    publish(
        provider,
        request,
        boundary,
        tokio_util::io::ReaderStream::new(file),
        size,
    )
    .await
}

fn preparation_error(error: &std::io::Error) -> plenora_storage_core::StorageError {
    crate::common::io_error(error, plenora_storage_core::ErrorPhase::Prepare, false)
}

async fn contains_boundary(file: &mut tokio::fs::File, boundary: &[u8]) -> StorageResult<bool> {
    file.rewind()
        .await
        .map_err(|error| preparation_error(&error))?;
    let mut buffer = vec![0; 64 * 1024 + boundary.len()];
    let mut kept = 0;
    loop {
        let count = file
            .read(&mut buffer[kept..])
            .await
            .map_err(|error| preparation_error(&error))?;
        if count == 0 {
            return Ok(false);
        }
        let filled = kept + count;
        if buffer[..filled]
            .windows(boundary.len())
            .any(|window| window == boundary)
        {
            return Ok(true);
        }
        kept = (boundary.len() - 1).min(filled);
        buffer.copy_within(filled - kept..filled, 0);
    }
}

async fn publish(
    provider: &GcsBackend,
    request: &PutRequest,
    boundary: String,
    payload: impl Stream<Item = Result<Bytes, std::io::Error>> + Send + 'static,
    expected_size: u64,
) -> StorageResult<()> {
    let mut url = provider.url(None, true)?;
    url.query_pairs_mut().append_pair("uploadType", "multipart");
    if !request.overwrite {
        url.query_pairs_mut().append_pair("ifGenerationMatch", "0");
    }
    let content_type = request
        .content_type
        .as_deref()
        .unwrap_or("application/octet-stream");
    let meta = serde_json::json!({"name":request.key,"contentType":content_type,"metadata":request.metadata});
    let start = Bytes::from(format!(
        "--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
    ));
    let end = Bytes::from(format!("\r\n--{boundary}--\r\n"));
    let length = expected_size
        .checked_add(start.len() as u64 + end.len() as u64)
        .ok_or_else(crate::common::limit_error)?;
    let body = reqwest::Body::wrap_stream(
        futures_util::stream::iter([Ok(start)])
            .chain(payload)
            .chain(futures_util::stream::iter([Ok(end)])),
    );
    let response = send(
        provider
            .request(Method::POST, url)
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

#[cfg(test)]
#[path = "publication_tests.rs"]
mod tests;
