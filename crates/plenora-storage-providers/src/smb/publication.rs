//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    Bytes, ErrorCategory, ErrorKind, ErrorPhase, PutRequest, SmbBackend, StorageError,
    StorageResult, failure, invalid, smb_error,
};

pub async fn put(
    provider: &mut SmbBackend,
    request: &PutRequest,
    data: Bytes,
) -> StorageResult<()> {
    let path = provider.path(&request.key)?;
    let mut prepared = false;
    let result = async {
        if let Some((parent, _)) = request.key.rsplit_once('/') {
            let mut current = String::new();
            for part in parent.split('/') {
                if !current.is_empty() {
                    current.push('/');
                }
                current.push_str(part);
                let path = provider.path(&current)?;
                match provider.tree.stat(&mut provider.conn, &path).await {
                    Ok(info) if info.is_directory => {}
                    Ok(_) => return Err(invalid("SMB_PARENT_NOT_DIRECTORY")),
                    Err(error) if error.kind() == ErrorKind::NotFound => {
                        prepared = true;
                        if let Err(error) = provider
                            .tree
                            .create_directory(&mut provider.conn, &path)
                            .await
                            && error.kind() != ErrorKind::AlreadyExists
                        {
                            return Err(smb_error(&error, true));
                        }
                    }
                    Err(error) => return Err(smb_error(&error, false)),
                }
            }
        }
        let mut writer = if request.overwrite {
            provider
                .tree
                .create_file_writer(provider.conn.clone(), &path)
                .await
        } else {
            provider
                .tree
                .create_file_writer_exclusive(provider.conn.clone(), &path)
                .await
        }
        .map_err(|error| smb_error(&error, true))?;
        for chunk in data.chunks(64 * 1024) {
            writer.write_chunk(chunk).await.map_err(|error| {
                smb_error(&error, true).cleanup_unconfirmed("destination_may_be_partial")
            })?;
        }
        let size = writer.finish().await.map_err(|error| {
            smb_error(&error, true).cleanup_unconfirmed("destination_may_be_partial")
        })?;
        if size != data.len() as u64 {
            return Err(failure(ErrorCategory::Protocol, ErrorPhase::Commit, true));
        }
        Ok(())
    }
    .await;
    result.map_err(|error: StorageError| error.with_preparation_effect(prepared))
}
