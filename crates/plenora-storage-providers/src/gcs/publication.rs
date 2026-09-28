//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{Bytes, GcsBackend, Method, Object, PutRequest, StorageResult, invalid, json, send};

pub async fn put(provider: &GcsBackend, request: &PutRequest, data: Bytes) -> StorageResult<()> {
    let mut url = provider.url(None, true)?;
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
