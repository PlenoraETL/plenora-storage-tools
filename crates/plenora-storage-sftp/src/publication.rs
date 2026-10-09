//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    AsyncRead, AsyncWriteExt, CopyRequest, ErrorCategory, ErrorPhase, ObjectMetadata, OpenFlags,
    OperationContext, PROVIDER_ID, ProviderConnection, PutRequest, RemoteEffect, RetryDisposition,
    SftpProvider, StorageError, StorageResult, TransferResult, atomic_replace,
    committed_verification_error, copy_with_control, discard_staged_object,
    ensure_parent_directories, flush_error, map_exclusive_open_error, map_sftp_error,
    mutation_stream_error, public_metadata, remote_path, temporary_path, transfer_limit_error,
    transfer_result, validate_file_metadata, validate_key, validate_sftp_publication,
};

#[allow(
    clippy::too_many_lines,
    reason = "Audit exclusive staging ownership, directory effects and atomic rename in order"
)]
pub async fn put(
    provider: &SftpProvider,
    connection: &ProviderConnection,
    request: &PutRequest,
    source: &mut (dyn AsyncRead + Send + Unpin),
    context: &OperationContext<'_>,
) -> StorageResult<TransferResult> {
    let mut parent_effect = false;
    let result = async {
        if request
            .content_length
            .is_some_and(|length| length > context.policy.max_transfer_bytes)
        {
            return Err(
                transfer_limit_error().with_outcome(RemoteEffect::None, RetryDisposition::Never)
            );
        }
        let atomic_publish =
            validate_sftp_publication(connection, request.overwrite, request.publication_policy)?;
        validate_key(&request.key)?;
        validate_file_metadata(request)?;
        let remote = context
            .control
            .run(
                provider.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let atomic = if atomic_publish {
            Some(
                context
                    .control
                    .run(
                        remote.atomic_session(context.control),
                        ErrorPhase::Connect,
                        false,
                    )
                    .await?,
            )
        } else {
            None
        };
        let destination_path = remote_path(&remote.root, &request.key);
        ensure_parent_directories(&remote.sftp, &destination_path, context, &mut parent_effect)
            .await?;
        let write_path = if atomic_publish {
            temporary_path(&destination_path)
        } else {
            destination_path.clone()
        };
        let flags = if atomic_publish || !request.overwrite {
            OpenFlags::CREATE | OpenFlags::EXCLUDE | OpenFlags::WRITE
        } else {
            OpenFlags::CREATE | OpenFlags::TRUNCATE | OpenFlags::WRITE
        };
        let opened = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .open_with_flags(write_path.clone(), flags)
                        .await
                        .map_err(|error| {
                            if request.overwrite {
                                map_sftp_error(error, ErrorPhase::Prepare, true)
                            } else {
                                map_exclusive_open_error(error, "SFTP_CREATE_CONFLICT")
                            }
                        })
                },
                ErrorPhase::Prepare,
                true,
            )
            .await;
        // Deliberately no cleanup on this path. An exclusive create can fail
        // precisely because the name is already held, and this operation cannot
        // prove it owns a file it did not open. Deleting it would be a
        // destructive guess; the error already reports an ambiguous outcome.
        let mut file = opened?;
        let transfer = copy_with_control(source, &mut file, context, true).await;
        let (bytes_transferred, digest) = match transfer {
            Ok(result) => result,
            Err(error) => {
                return Err(discard_staged_object(
                    &remote.sftp,
                    atomic_publish,
                    &write_path,
                    error,
                )
                .await);
            }
        };
        if request
            .content_length
            .is_some_and(|expected| expected != bytes_transferred)
        {
            let error = StorageError::new(
                ErrorCategory::InvalidConfiguration,
                ErrorPhase::Commit,
                RemoteEffect::Partial,
                RetryDisposition::RequiresRecovery,
                "CONTENT_LENGTH_MISMATCH",
                "artifact length differs from declared content_length",
            )
            .with_provider(PROVIDER_ID);
            return Err(
                discard_staged_object(&remote.sftp, atomic_publish, &write_path, error).await,
            );
        }
        if let Err(error) = finish_written_file(&mut file, context).await {
            return Err(
                discard_staged_object(&remote.sftp, atomic_publish, &write_path, error).await,
            );
        }
        if let Some(session) = atomic.as_ref()
            && let Err(error) = context
                .control
                .run(
                    atomic_replace(session, &write_path, &destination_path),
                    ErrorPhase::Commit,
                    true,
                )
                .await
        {
            return Err(discard_staged_object(&remote.sftp, true, &write_path, error).await);
        }
        Ok(transfer_result(
            request.key.clone(),
            bytes_transferred,
            digest,
        ))
    }
    .await;
    result.map_err(|error: StorageError| error.with_preparation_effect(parent_effect))
}

#[allow(
    clippy::too_many_lines,
    reason = "Audit source preservation, staging ownership and post-commit verification together"
)]
pub async fn copy(
    provider: &SftpProvider,
    connection: &ProviderConnection,
    request: &CopyRequest,
    context: &OperationContext<'_>,
) -> StorageResult<ObjectMetadata> {
    let mut parent_effect = false;
    let result = async {
        let atomic_publish =
            validate_sftp_publication(connection, request.overwrite, request.publication_policy)?;
        validate_key(&request.source_key)?;
        validate_key(&request.destination_key)?;
        // A provider-copy would open the destination for truncation while the
        // source is still open, destroying the object being copied.
        if request.source_key == request.destination_key {
            return Err(StorageError::invalid_configuration(
                "COPY_TARGET_EQUALS_SOURCE",
                "copy source and destination must differ",
            )
            .with_provider(PROVIDER_ID));
        }
        let remote = context
            .control
            .run(
                provider.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let atomic = if atomic_publish {
            Some(
                context
                    .control
                    .run(
                        remote.atomic_session(context.control),
                        ErrorPhase::Connect,
                        false,
                    )
                    .await?,
            )
        } else {
            None
        };
        let source_path = remote_path(&remote.root, &request.source_key);
        let destination_path = remote_path(&remote.root, &request.destination_key);
        ensure_parent_directories(&remote.sftp, &destination_path, context, &mut parent_effect)
            .await?;
        let mut source = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .open(source_path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        let write_path = if atomic_publish {
            temporary_path(&destination_path)
        } else {
            destination_path.clone()
        };
        let flags = if atomic_publish || !request.overwrite {
            OpenFlags::CREATE | OpenFlags::EXCLUDE | OpenFlags::WRITE
        } else {
            OpenFlags::CREATE | OpenFlags::TRUNCATE | OpenFlags::WRITE
        };
        let opened = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .open_with_flags(write_path.clone(), flags)
                        .await
                        .map_err(|error| {
                            if request.overwrite {
                                map_sftp_error(error, ErrorPhase::Prepare, true)
                            } else {
                                map_exclusive_open_error(error, "SFTP_COPY_CONFLICT")
                            }
                        })
                },
                ErrorPhase::Prepare,
                true,
            )
            .await;
        // Deliberately no cleanup on this path, for the same reason as `put`:
        // the operation cannot prove it owns a file it did not open.
        let mut destination = opened?;
        if let Err(error) = copy_with_control(&mut source, &mut destination, context, true).await {
            return Err(
                discard_staged_object(&remote.sftp, atomic_publish, &write_path, error).await,
            );
        }
        if let Err(error) = finish_written_file(&mut destination, context).await {
            return Err(
                discard_staged_object(&remote.sftp, atomic_publish, &write_path, error).await,
            );
        }
        if let Some(session) = atomic.as_ref()
            && let Err(error) = context
                .control
                .run(
                    atomic_replace(session, &write_path, &destination_path),
                    ErrorPhase::Commit,
                    true,
                )
                .await
        {
            return Err(discard_staged_object(&remote.sftp, true, &write_path, error).await);
        }
        // The destination is published from here on: a failed read-back must
        // not be reported as an operation without a remote effect.
        let metadata = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .metadata(destination_path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Cleanup,
                true,
            )
            .await
            .map_err(|_| committed_verification_error())?;
        Ok(public_metadata(request.destination_key.clone(), &metadata))
    }
    .await;
    result.map_err(|error: StorageError| error.with_preparation_effect(parent_effect))
}

/// Completes a written file: `fsync`, then close, under the operation
/// control. Both requests use the session's request timeout (see
/// `request_timeout_secs`), so a slow but answered `fsync` succeeds within the
/// deadline; an unanswered one is a `timeout` with an unknown effect.
pub async fn finish_written_file(
    file: &mut russh_sftp::client::fs::File,
    context: &OperationContext<'_>,
) -> StorageResult<()> {
    context
        .control
        .run(
            async {
                file.sync_all().await.map_err(flush_error)?;
                file.shutdown()
                    .await
                    .map_err(|error| mutation_stream_error(&error, ErrorPhase::Commit))
            },
            ErrorPhase::Commit,
            true,
        )
        .await
}
