//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    Bytes, Dav, ErrorCategory, ErrorPhase, Method, PutRequest, StatusCode, StorageError,
    StorageResult, checked, failure, invalid,
};

pub async fn put(provider: &Dav, r: &PutRequest, data: Bytes) -> StorageResult<()> {
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
                let response = provider
                    .request(
                        Method::from_bytes(b"MKCOL").map_err(|_| invalid("METHOD_INVALID"))?,
                        provider.url(&current)?,
                    )
                    .send()
                    .await
                    .map_err(|_| failure(ErrorCategory::Io, ErrorPhase::Prepare, true))?;
                let status = response.status();
                if let Err(error) = checked(response, true) {
                    // Servers can report 405, 409 or even 500 when MKCOL
                    // loses a race. Reconcile with a read, never a retry of
                    // the mutation, and require proof of the exact collection.
                    if (status == StatusCode::METHOD_NOT_ALLOWED
                        || status == StatusCode::CONFLICT
                        || status.is_server_error())
                        && let Ok(entries) = provider.properties(&current, "0").await
                        && entries
                            .iter()
                            .any(|entry| entry.key == current && entry.directory)
                    {
                        continue;
                    }
                    return Err(error);
                }
            }
        }
        let mut request = provider
            .request(Method::PUT, provider.url(&r.key)?)
            .body(data);
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
