//! Bounded transfer, listing and publication primitives.

use super::{
    ArtifactMetadata, AsyncBufRead, AsyncBufReadExt, AsyncFtpStream, AsyncRead, AsyncReadExt,
    AsyncRustlsStream, AsyncWrite, AsyncWriteExt, BufReader, CLEANUP_BUDGET, Digest, ErrorCategory,
    ErrorPhase, File, FtpError, IntegrityMetadata, ListParser, MAX_MLSD_LINE_BYTES, ObjectMetadata,
    OffsetDateTime, OperationContext, PROVIDER_ID, ParseError, ProviderListRequest, RemoteEffect,
    RetryDisposition, Rfc3339, Sha256, Status, StorageError, StorageResult, SystemTime,
    TransferResult, TransferStream, list_parse_error, list_scan_limit_error, map_ftp_error,
    transfer_io_error, transfer_limit_error,
};

pub async fn ensure_parent_directories(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
    parent_effect: &mut bool,
) -> StorageResult<()> {
    let Some((parent, _)) = key.rsplit_once('/') else {
        return Ok(());
    };
    let mut current = String::new();
    for part in parent.split('/') {
        if !current.is_empty() {
            current.push('/');
        }
        current.push_str(part);
        if !context
            .control
            .run(
                ftp_directory_exists(ftp, &current),
                ErrorPhase::Probe,
                false,
            )
            .await?
        {
            *parent_effect = true;
            context
                .control
                .run(
                    async {
                        if let Err(error) = ftp.mkdir(&current).await {
                            // A competing writer may create this shared parent
                            // after our probe. A 550 alone is ambiguous: accept
                            // it only after MLST proves an existing directory.
                            if is_file_unavailable(&error)
                                && let Ok(line) = ftp.mlst(Some(&current)).await
                                && let Ok(file) = parse_listing_entry(&line)
                                && file.is_directory()
                            {
                                return Ok(());
                            }
                            return Err(map_ftp_error(error, ErrorPhase::Prepare, true));
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

/// Weak existence probe used only to decide whether a parent directory still
/// has to be created. A wrong answer here is self-correcting: the following
/// `MKD` fails and the error is reported.
pub async fn ftp_directory_exists(ftp: &mut AsyncFtpStream, path: &str) -> StorageResult<bool> {
    match ftp.mlst(Some(path)).await {
        Ok(_) => Ok(true),
        Err(error) if is_file_unavailable(&error) => Ok(false),
        Err(error) => Err(map_ftp_error(error, ErrorPhase::Probe, false)),
    }
}

/// Proves an object is absent by listing its parent directory.
///
/// FTP reports "not found", "permission denied" and other unavailability with
/// the same 550 status, so a failed `MLST` on the object cannot be read as
/// absence. A parent that cannot be listed is an error, never an absence.
pub async fn ftp_object_exists(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
) -> StorageResult<bool> {
    let (parent, name) = key.rsplit_once('/').unwrap_or((".", key));
    let mut found = false;
    scan_directory(ftp, parent, context, &mut 0, |file| {
        if file.name() == name {
            found = file.is_file();
        }
        Ok(())
    })
    .await?;
    Ok(found)
}

// suppaftp 12.0.1 indexes UNIX.mode by byte length and then character count.
// Reject non-octal/non-ASCII modes before either shared MLSD/MLST path reaches
// that parser; malformed server data must become a redacted protocol error.
pub fn parse_listing_entry(line: &str) -> Result<File, ParseError> {
    for fact in line.split(';') {
        let mut parts = fact.split('=');
        if let (Some(name), Some(value)) = (parts.next(), parts.next())
            && name.to_lowercase() == "unix.mode"
            && (!(3..=4).contains(&value.len()) || !value.bytes().all(|b| matches!(b, b'0'..=b'7')))
        {
            return Err(ParseError::SyntaxError);
        }
    }
    // Upstream MLSD and MLST delegate to the same parse_mlsx implementation.
    ListParser::parse_mlsd(line)
}

pub async fn read_listing_line<R: AsyncBufRead + Unpin>(
    reader: &mut R,
) -> StorageResult<Option<String>> {
    let mut bytes = Vec::new();
    let read = reader
        .take(MAX_MLSD_LINE_BYTES + 1)
        .read_until(b'\n', &mut bytes)
        .await
        .map_err(|_| transfer_io_error(ErrorPhase::Read, false))?;
    if read == 0 {
        return Ok(None);
    }
    if read as u64 > MAX_MLSD_LINE_BYTES {
        return Err(list_scan_limit_error());
    }
    if bytes.last() == Some(&b'\n') {
        bytes.pop();
    }
    if bytes.last() == Some(&b'\r') {
        bytes.pop();
    }
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| list_parse_error())
}

pub async fn scan_directory<F>(
    ftp: &mut AsyncFtpStream,
    directory: &str,
    context: &OperationContext<'_>,
    scanned: &mut usize,
    mut visit: F,
) -> StorageResult<()>
where
    F: FnMut(File) -> StorageResult<()> + Send,
{
    context
        .control
        .run(
            async {
                let (_, stream) = ftp
                    .custom_data_command(
                        format!("MLSD {directory}"),
                        &[Status::AboutToSend, Status::AlreadyOpen],
                    )
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))?;
                let mut reader = BufReader::new(stream);
                while let Some(line) = read_listing_line(&mut reader).await? {
                    if *scanned >= context.policy.max_list_items {
                        return Err(list_scan_limit_error());
                    }
                    *scanned += 1;
                    let file = parse_listing_entry(&line).map_err(|_| list_parse_error())?;
                    visit(file)?;
                }
                reader
                    .into_inner()
                    .finish()
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await
}

pub async fn stat_file(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
) -> StorageResult<ObjectMetadata> {
    let line = context
        .control
        .run(
            async {
                ftp.mlst(Some(key))
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await?;
    let file = parse_listing_entry(&line).map_err(|_| {
        StorageError::new(
            ErrorCategory::Protocol,
            ErrorPhase::Read,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_STAT_PARSE_FAILED",
            "FTP MLST response is invalid",
        )
        .with_provider(PROVIDER_ID)
    })?;
    Ok(public_metadata(key.to_owned(), &file))
}

pub async fn finish_upload(mut stream: TransferStream<AsyncRustlsStream>) -> StorageResult<()> {
    if matches!(stream.get_ref(), suppaftp::tokio::AsyncDataStream::Ssl(_)) {
        // Keep the socket alive until the peer completes TLS shutdown. Dropping
        // it immediately after our write shutdown can truncate queued data on
        // Windows, even if the server subsequently reports a successful STOR.
        stream
            .shutdown()
            .await
            .map_err(|_| transfer_io_error(ErrorPhase::Commit, true))?;
        let mut unexpected = [0_u8; 1];
        if stream
            .read(&mut unexpected)
            .await
            .map_err(|_| transfer_io_error(ErrorPhase::Commit, true))?
            != 0
        {
            return Err(transfer_io_error(ErrorPhase::Commit, true));
        }
    }
    stream
        .finish()
        .await
        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
}

/// Tears down an open data transfer.
///
/// `ABOR` only closes the data connection: FTP does not promise the partially
/// written object is removed, so the remote outcome the caller already reported
/// stays ambiguous and is left untouched.
pub async fn abort_transfer(ftp: &mut AsyncFtpStream, stream: TransferStream<AsyncRustlsStream>) {
    let _ = tokio::time::timeout(CLEANUP_BUDGET, ftp.abort(stream)).await;
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
    context
        .control
        .run(
            async {
                destination
                    .flush()
                    .await
                    .map_err(|_| transfer_io_error(ErrorPhase::Write, mutating))
            },
            ErrorPhase::Write,
            mutating,
        )
        .await?;
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

pub fn public_metadata(key: String, file: &File) -> ObjectMetadata {
    ObjectMetadata {
        key,
        size: u64::try_from(file.size()).unwrap_or(u64::MAX),
        last_modified: format_system_time(file.modified()),
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

pub fn is_file_unavailable(error: &FtpError) -> bool {
    matches!(
        error,
        FtpError::UnexpectedResponse(response) if response.status == Status::FileUnavailable
    )
}
