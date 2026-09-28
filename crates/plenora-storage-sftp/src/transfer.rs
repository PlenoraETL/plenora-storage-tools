//! Bounded transfer, listing and publication primitives.

use super::{
    ArtifactMetadata, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, CLEANUP_BUDGET, Digest,
    ErrorCategory, ErrorPhase, IntegrityMetadata, Metadata, ObjectMetadata, OffsetDateTime,
    OperationContext, Ordering, PROVIDER_ID, Packet, ProviderListRequest, RawSftpSession,
    RemoteEffect, RetryDisposition, Rfc3339, SftpError, SftpSession, Sha256, StatusCode,
    StorageError, StorageResult, SystemTime, TEMPORARY_NAME_NONCE, TransferResult,
    configuration_error, list_scan_limit_error, map_sftp_error, transfer_io_error,
    transfer_limit_error, validate_key,
};

pub async fn qualify_atomic_session(session: &RawSftpSession) -> StorageResult<()> {
    let version = session
        .init()
        .await
        .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
    if version
        .extensions
        .get("posix-rename@openssh.com")
        .map(String::as_str)
        != Some("1")
    {
        return Err(StorageError::unsupported(
            "SFTP atomic replacement requires posix-rename@openssh.com version 1",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

pub async fn atomic_replace(
    session: &RawSftpSession,
    source: &str,
    destination: &str,
) -> StorageResult<()> {
    let mut payload = Vec::new();
    for path in [source, destination] {
        let length = u32::try_from(path.len()).map_err(|_| configuration_error())?;
        payload.extend_from_slice(&length.to_be_bytes());
        payload.extend_from_slice(path.as_bytes());
    }
    let response = session
        .extended("posix-rename@openssh.com", payload)
        .await
        .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))?;
    match response {
        Packet::Status(status) if status.status_code == StatusCode::Ok => Ok(()),
        Packet::Status(status) => Err(map_sftp_error(status.into(), ErrorPhase::Commit, true)),
        _ => Err(map_sftp_error(
            SftpError::UnexpectedPacket,
            ErrorPhase::Commit,
            true,
        )),
    }
}

/// Builds a staging name that no other client can collide with.
///
/// A process id and a local counter are not enough: two clients on different
/// machines can produce the same pair, and an exclusive create that loses that
/// race would report a conflict against a file it does not own.
pub fn temporary_path(destination: &str) -> String {
    let nonce = TEMPORARY_NAME_NONCE.fetch_add(1, Ordering::Relaxed);
    let elapsed = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |value| value.as_nanos());
    let mut digest = Sha256::new();
    digest.update(destination.as_bytes());
    digest.update(std::process::id().to_le_bytes());
    digest.update(nonce.to_le_bytes());
    digest.update(elapsed.to_le_bytes());
    let unique = hex::encode(digest.finalize());
    format!("{destination}.plenora-tmp-{}", &unique[..32])
}

pub fn remote_path(root: &str, key: &str) -> String {
    if root == "." {
        format!("./{key}")
    } else if root.ends_with('/') {
        format!("{root}{key}")
    } else {
        format!("{root}/{key}")
    }
}

pub fn relative_key(root: &str, path: &str) -> StorageResult<String> {
    let normalized_root = root.trim_end_matches('/');
    let key = path
        .strip_prefix(normalized_root)
        .unwrap_or(path)
        .trim_start_matches('/')
        .to_owned();
    validate_key(&key)?;
    Ok(key)
}

pub async fn scan_directory<F>(
    listing: &RawSftpSession,
    directory: &str,
    context: &OperationContext<'_>,
    scanned: &mut usize,
    mut visit: F,
) -> StorageResult<()>
where
    F: FnMut(russh_sftp::protocol::File) -> StorageResult<()> + Send,
{
    context
        .control
        .run(
            async {
                let handle = listing
                    .opendir(directory)
                    .await
                    .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?
                    .handle;
                loop {
                    let batch = match listing.readdir(&handle).await {
                        Ok(batch) => batch,
                        Err(SftpError::Status(status)) if status.status_code == StatusCode::Eof => {
                            break;
                        }
                        Err(error) => return Err(map_sftp_error(error, ErrorPhase::Read, false)),
                    };
                    for entry in batch.files {
                        if matches!(entry.filename.as_str(), "." | "..") {
                            continue;
                        }
                        if *scanned >= context.policy.max_list_items {
                            return Err(list_scan_limit_error());
                        }
                        *scanned += 1;
                        visit(entry)?;
                    }
                }
                listing
                    .close(handle)
                    .await
                    .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?;
                Ok(())
            },
            ErrorPhase::Read,
            false,
        )
        .await
}

pub async fn ensure_parent_directories(
    sftp: &SftpSession,
    path: &str,
    context: &OperationContext<'_>,
    parent_effect: &mut bool,
) -> StorageResult<()> {
    let Some((parent, _)) = path.rsplit_once('/') else {
        return Ok(());
    };
    let absolute = parent.starts_with('/');
    let mut current = if absolute {
        "/".to_owned()
    } else {
        String::new()
    };
    for part in parent
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
    {
        if !current.is_empty() && current != "/" {
            current.push('/');
        }
        current.push_str(part);
        let exists = context
            .control
            .run(
                async {
                    sftp.try_exists(&current)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Prepare, false))
                },
                ErrorPhase::Prepare,
                false,
            )
            .await?;
        if !exists {
            *parent_effect = true;
            context
                .control
                .run(
                    async {
                        if let Err(error) = sftp.create_dir(&current).await {
                            // mkdir can lose a race against another writer.
                            // Only a verified directory satisfies this step.
                            if let Ok(metadata) = sftp.metadata(&current).await
                                && metadata.file_type().is_dir()
                            {
                                return Ok(());
                            }
                            return Err(map_sftp_error(error, ErrorPhase::Prepare, true));
                        }
                        Ok(())
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await?;
        }
    }
    Ok(())
}

pub async fn copy_with_control<R, W>(
    source: &mut R,
    destination: &mut W,
    context: &OperationContext<'_>,
    mutating: bool,
) -> StorageResult<(u64, Sha256)>
where
    R: AsyncRead + Send + Unpin + ?Sized,
    W: AsyncWrite + Send + Unpin + ?Sized,
{
    let mut buffer = vec![0_u8; 64 * 1_024];
    let mut transferred = 0_u64;
    let mut digest = Sha256::new();
    loop {
        let read = context
            .control
            .run(
                async {
                    source
                        .read(&mut buffer)
                        .await
                        .map_err(|_| transfer_io_error(ErrorPhase::Read, mutating))
                },
                ErrorPhase::Read,
                mutating,
            )
            .await?;
        if read == 0 {
            break;
        }
        transferred = transferred
            .checked_add(read as u64)
            .filter(|total| *total <= context.policy.max_transfer_bytes)
            .ok_or_else(transfer_limit_error)?;
        digest.update(&buffer[..read]);
        context
            .control
            .run(
                async {
                    destination
                        .write_all(&buffer[..read])
                        .await
                        .map_err(|_| transfer_io_error(ErrorPhase::Write, mutating))
                },
                ErrorPhase::Write,
                mutating,
            )
            .await?;
    }
    Ok((transferred, digest))
}

pub fn list_limit(
    request: &ProviderListRequest,
    context: &OperationContext<'_>,
) -> StorageResult<usize> {
    let limit = request.max_items.unwrap_or(1_000);
    if limit == 0 || limit > context.policy.max_list_items {
        return Err(StorageError::new(
            ErrorCategory::ResourceLimit,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "LIST_LIMIT_INVALID",
            "list max_items is zero or exceeds the engine policy",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(limit)
}

pub fn public_metadata(key: String, metadata: &Metadata) -> ObjectMetadata {
    ObjectMetadata {
        key,
        size: metadata.len(),
        last_modified: metadata.modified().ok().and_then(format_system_time),
        etag: None,
        version: None,
    }
}

pub fn format_system_time(value: SystemTime) -> Option<String> {
    OffsetDateTime::from(value).format(&Rfc3339).ok()
}

pub fn transfer_result(key: String, bytes_transferred: u64, digest: Sha256) -> TransferResult {
    let checksum = IntegrityMetadata {
        algorithm: "sha256".to_owned(),
        value: hex::encode(digest.finalize()),
    };
    TransferResult {
        key,
        bytes_transferred,
        artifact: ArtifactMetadata {
            content_type: None,
            size: Some(bytes_transferred),
            sha256: Some(checksum.value.clone()),
        },
        checksum,
        etag: None,
        version: None,
    }
}

/// Removes an unpublished staging object and restates the outcome.
///
/// A confirmed removal turns an ambiguous failure into a rolled back one; an
/// unconfirmed removal is reported rather than leaving a `.plenora-tmp-*` object
/// behind silently. The original cause is preserved either way.
pub async fn discard_staged_object(
    sftp: &SftpSession,
    staged: bool,
    write_path: &str,
    error: StorageError,
) -> StorageError {
    if !staged {
        return error;
    }
    match tokio::time::timeout(CLEANUP_BUDGET, sftp.remove_file(write_path)).await {
        Ok(Ok(())) => error.rolled_back(),
        _ => error.cleanup_unconfirmed("staging_remove_failed"),
    }
}
