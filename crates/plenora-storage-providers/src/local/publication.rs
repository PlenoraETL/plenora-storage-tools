//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    Bytes, ErrorPhase, LocalBackend, OpenOptions, PutRequest, RemoteEffect, RetryDisposition,
    StorageResult, Write, blocking, io_error, portable_key, stage_name,
};

pub async fn put(provider: &LocalBackend, request: &PutRequest, data: Bytes) -> StorageResult<()> {
    portable_key(&request.key)?;
    let key = request.key.clone();
    let overwrite = request.overwrite;
    let dir = provider.dir.clone();
    blocking(move || {
        let (parent, name) = key.rsplit_once('/').unwrap_or(("", key.as_str()));
        let mut prepared = false;
        if !parent.is_empty() && dir.open_dir(parent).is_err() {
            prepared = true;
            dir.create_dir_all(parent).map_err(|error| {
                io_error(&error, ErrorPhase::Prepare, true).with_preparation_effect(true)
            })?;
        }
        // Keep an open parent capability throughout staging and publication.
        let parent = if parent.is_empty() {
            dir.try_clone()
        } else {
            dir.open_dir(parent)
        }
        .map_err(|error| {
            io_error(&error, ErrorPhase::Prepare, false).with_preparation_effect(prepared)
        })?;
        let stage = stage_name();
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        let mut owned = false;
        let result: StorageResult<()> = (|| {
            let mut file = parent
                .open_with(&stage, &options)
                .map_err(|error| io_error(&error, ErrorPhase::Prepare, true))?;
            owned = true;
            file.write_all(&data)
                .map_err(|error| io_error(&error, ErrorPhase::Write, true))?;
            file.sync_all()
                .map_err(|error| io_error(&error, ErrorPhase::Write, true))?;
            drop(file);
            if overwrite {
                parent
                    .rename(&stage, &parent, name)
                    .map_err(|error| io_error(&error, ErrorPhase::Commit, true))?;
            } else {
                parent
                    .hard_link(&stage, &parent, name)
                    .map_err(|error| io_error(&error, ErrorPhase::Commit, true))?;
                parent.remove_file(&stage).map_err(|error| {
                    io_error(&error, ErrorPhase::Cleanup, true)
                        .with_outcome(RemoteEffect::Committed, RetryDisposition::RequiresRecovery)
                })?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            // Only the unique staging name belongs to this operation.
            let cleaned = owned && parent.remove_file(&stage).is_ok();
            return Err(
                if cleaned && error.remote_effect != RemoteEffect::Committed {
                    error.rolled_back()
                } else {
                    error
                }
                .with_preparation_effect(prepared),
            );
        }
        Ok(())
    })
    .await
}
