//! File transfers shared by the CLI and language bindings.
use plenora_storage_core::{
    Engine, ErrorCategory, ErrorPhase, ExecutionControl, GetRequest, ProviderConnection,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};
use std::path::{Path, PathBuf};
use tokio::fs;

pub async fn get_to_file(
    engine: &Engine,
    connection: &ProviderConnection,
    key: String,
    output: &Path,
    overwrite: bool,
    control: &ExecutionControl,
) -> StorageResult<plenora_storage_core::TransferResult> {
    // Every local admission check runs before the staging file is created, so a
    // request the Engine would reject produces no filesystem effect at all.
    engine.preflight(connection, &[&key])?;
    control.check(ErrorPhase::Prepare, false)?;
    // The artifact is always staged first, so a failed download never publishes
    // a partial file and the destination only ever appears complete.
    let temporary = temporary_output_path(output)?;
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .await
        .map_err(|_| artifact_io_error("OUTPUT_STAGING_CREATE_FAILED"))?;
    let result = engine
        .get(connection, &GetRequest { key }, &mut file, control)
        .await;
    let downloaded = match result {
        Ok(result) => file
            .sync_all()
            .await
            .map_err(|_| artifact_io_error("OUTPUT_STAGING_SYNC_FAILED"))
            .map(|()| result),
        Err(error) => Err(error),
    };
    drop(file);
    let published = match downloaded {
        Ok(result) => match control.check(ErrorPhase::Commit, false) {
            Ok(()) => publish_output(&temporary, output, overwrite)
                .await
                .map(|()| result),
            Err(error) => Err(error),
        },
        Err(error) => Err(error),
    };
    match published {
        Ok(result) => Ok(result),
        // The staging file is the only place downloaded bytes ever landed, so
        // removing it undoes the whole local effect. A removal that fails leaves
        // an incomplete artifact behind and is reported alongside the cause,
        // never in place of it.
        Err(error) => Err(if fs::remove_file(&temporary).await.is_ok() {
            error.rolled_back()
        } else {
            error.cleanup_unconfirmed("staging_remove_failed")
        }),
    }
}

/// Publishes a staged artifact.
///
/// With `overwrite=false` the destination is created by linking the staged file
/// into place. The link fails if anything already holds that name, so there is
/// no probe window, and no file this command did not create is ever replaced or
/// removed. With `overwrite=true` the replacement is a rename, which is atomic
/// on both Unix and Windows.
async fn publish_output(temporary: &Path, output: &Path, overwrite: bool) -> StorageResult<()> {
    if overwrite {
        return fs::rename(temporary, output)
            .await
            .map_err(|error| publish_error(&error));
    }
    fs::hard_link(temporary, output)
        .await
        .map_err(|error| publish_error(&error))?;
    // The artifact is published at this point. A staging file that cannot be
    // removed is a hidden leftover, never a partial artifact, so it does not
    // turn a successful publication into a failure.
    let _ = fs::remove_file(temporary).await;
    Ok(())
}

fn publish_error(error: &std::io::Error) -> StorageError {
    use std::io::ErrorKind;
    let (category, code, message) = match error.kind() {
        ErrorKind::AlreadyExists => (
            ErrorCategory::Conflict,
            "OUTPUT_EXISTS",
            "output destination already exists",
        ),
        ErrorKind::PermissionDenied => (
            ErrorCategory::Authorization,
            "OUTPUT_PUBLISH_DENIED",
            "publishing the artifact to the output destination was denied",
        ),
        // Create-if-absent needs a link-capable filesystem. Degrading to a probe
        // plus rename would silently drop the no-clobber guarantee, so the
        // limitation is reported instead.
        ErrorKind::Unsupported => (
            ErrorCategory::Unsupported,
            "OUTPUT_ATOMIC_PUBLISH_UNSUPPORTED",
            "the output filesystem cannot publish an artifact atomically",
        ),
        _ => (
            ErrorCategory::Io,
            "OUTPUT_PUBLISH_FAILED",
            "publishing the artifact to the output destination failed",
        ),
    };
    StorageError::new(
        category,
        ErrorPhase::Commit,
        RemoteEffect::None,
        RetryDisposition::Never,
        code,
        message,
    )
}

pub struct PutFileOptions {
    pub key: String,
    pub input: PathBuf,
    pub overwrite: bool,
    pub publication_policy: PublicationPolicy,
    pub content_type: Option<String>,
}

pub async fn put_from_file(
    engine: &Engine,
    connection: &ProviderConnection,
    options: PutFileOptions,
    control: &ExecutionControl,
) -> StorageResult<plenora_storage_core::TransferResult> {
    engine.preflight(connection, &[&options.key])?;
    control.check(ErrorPhase::Prepare, false)?;
    let metadata = fs::metadata(&options.input)
        .await
        .map_err(|_| artifact_io_error("INPUT_METADATA_FAILED"))?;
    if !metadata.is_file() {
        return Err(StorageError::invalid_configuration(
            "INPUT_NOT_REGULAR_FILE",
            "upload input must be a regular file",
        ));
    }
    let mut file = fs::File::open(&options.input)
        .await
        .map_err(|_| artifact_io_error("INPUT_OPEN_FAILED"))?;
    engine
        .put(
            connection,
            &PutRequest {
                key: options.key,
                overwrite: options.overwrite,
                publication_policy: options.publication_policy,
                content_type: options.content_type,
                content_length: Some(metadata.len()),
                metadata: std::collections::BTreeMap::new(),
            },
            &mut file,
            control,
        )
        .await
}

fn temporary_output_path(output: &Path) -> StorageResult<PathBuf> {
    static NONCE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let filename = output.file_name().ok_or_else(|| {
        StorageError::invalid_configuration("OUTPUT_PATH_INVALID", "output path is invalid")
    })?;
    Ok(output.with_file_name(format!(
        ".{}.plenora-storage-{}-{}.part",
        filename.to_string_lossy(),
        std::process::id(),
        NONCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    )))
}

fn artifact_io_error(code: &'static str) -> StorageError {
    StorageError::new(
        ErrorCategory::Io,
        ErrorPhase::Write,
        RemoteEffect::None,
        RetryDisposition::Never,
        code,
        "local artifact operation failed",
    )
}
