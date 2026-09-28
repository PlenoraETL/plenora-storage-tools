//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    AsyncRead, CopyRequest, ErrorCategory, ErrorPhase, FtpProvider, ObjectMetadata,
    OperationContext, PROVIDER_ID, ProviderConnection, PutRequest, RemoteEffect, RetryDisposition,
    StorageError, StorageProvider, StorageResult, TransferResult, abort_transfer,
    committed_verification_error, copy_with_control, ensure_parent_directories, finish_upload,
    map_ftp_error, stat_file, transfer_limit_error, transfer_result, validate_file_metadata,
    validate_ftp_publication, validate_key,
};

pub async fn put(
    provider: &FtpProvider,
    connection: &ProviderConnection,
    request: &PutRequest,
    source: &mut (dyn AsyncRead + Send + Unpin),
    context: &OperationContext<'_>,
) -> StorageResult<TransferResult> {
    let outcome = async {
        let mut parent_effect = false;
        let result = async {
            if request
                .content_length
                .is_some_and(|length| length > context.policy.max_transfer_bytes)
            {
                return Err(transfer_limit_error()
                    .with_outcome(RemoteEffect::None, RetryDisposition::Never));
            }
            validate_ftp_publication(request.overwrite, request.publication_policy)?;
            validate_key(&request.key)?;
            validate_file_metadata(request)?;
            let mut remote = context
                .control
                .run(
                    provider.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            ensure_parent_directories(&mut remote.ftp, &request.key, context, &mut parent_effect)
                .await?;
            let mut stream = context
                .control
                .run(
                    async {
                        remote
                            .ftp
                            .put_with_stream(&request.key)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await?;
            let transfer = copy_with_control(source, &mut stream, context, true).await;
            let (bytes_transferred, digest) = match transfer {
                Ok(result) => result,
                Err(error) => {
                    abort_transfer(&mut remote.ftp, stream).await;
                    return Err(error);
                }
            };
            if request
                .content_length
                .is_some_and(|expected| expected != bytes_transferred)
            {
                abort_transfer(&mut remote.ftp, stream).await;
                return Err(StorageError::new(
                    ErrorCategory::InvalidConfiguration,
                    ErrorPhase::Commit,
                    RemoteEffect::Partial,
                    RetryDisposition::RequiresRecovery,
                    "CONTENT_LENGTH_MISMATCH",
                    "artifact length differs from declared content_length",
                )
                .with_provider(PROVIDER_ID));
            }
            context
                .control
                .run(finish_upload(stream), ErrorPhase::Commit, true)
                .await?;
            let published = stat_file(&mut remote.ftp, &request.key, context)
                .await
                .map_err(|_| committed_verification_error())?;
            if published.size != bytes_transferred {
                return Err(committed_verification_error());
            }
            Ok(transfer_result(
                request.key.clone(),
                bytes_transferred,
                digest,
            ))
        }
        .await;
        result.map_err(|error: StorageError| {
            error
                .with_preparation_effect(parent_effect)
                .with_provider(provider.id())
        })
    }
    .await;
    outcome.map_err(|error: StorageError| error.with_provider(provider.id()))
}

#[allow(
    clippy::too_many_lines,
    reason = "Keep two FTP control sessions, data completion and committed verification ordered"
)]
pub async fn copy(
    provider: &FtpProvider,
    connection: &ProviderConnection,
    request: &CopyRequest,
    context: &OperationContext<'_>,
) -> StorageResult<ObjectMetadata> {
    let outcome = async {
        let mut parent_effect = false;
        let result = async {
            validate_ftp_publication(request.overwrite, request.publication_policy)?;
            validate_key(&request.source_key)?;
            validate_key(&request.destination_key)?;
            // A provider-copy would open the destination for writing while the source is
            // still being read, destroying the object being copied.
            if request.source_key == request.destination_key {
                return Err(StorageError::invalid_configuration(
                    "COPY_TARGET_EQUALS_SOURCE",
                    "copy source and destination must differ",
                )
                .with_provider(PROVIDER_ID));
            }
            let mut source_remote = context
                .control
                .run(
                    provider.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let mut destination_remote = context
                .control
                .run(
                    provider.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let expected_size = stat_file(&mut source_remote.ftp, &request.source_key, context)
                .await?
                .size;
            ensure_parent_directories(
                &mut destination_remote.ftp,
                &request.destination_key,
                context,
                &mut parent_effect,
            )
            .await?;
            let mut source = context
                .control
                .run(
                    async {
                        source_remote
                            .ftp
                            .retr_as_stream(&request.source_key)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            let opened = context
                .control
                .run(
                    async {
                        destination_remote
                            .ftp
                            .put_with_stream(&request.destination_key)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await;
            let mut destination = match opened {
                Ok(destination) => destination,
                Err(error) => {
                    abort_transfer(&mut source_remote.ftp, source).await;
                    return Err(error);
                }
            };
            let transferred =
                match copy_with_control(&mut source, &mut destination, context, true).await {
                    Ok((count, _)) => count,
                    Err(error) => {
                        abort_transfer(&mut destination_remote.ftp, destination).await;
                        abort_transfer(&mut source_remote.ftp, source).await;
                        return Err(error);
                    }
                };
            if let Err(error) = context
                .control
                .run(finish_upload(destination), ErrorPhase::Commit, true)
                .await
            {
                abort_transfer(&mut source_remote.ftp, source).await;
                return Err(error);
            }
            // The destination is published from here on. Draining the source
            // transfer and reading the published metadata can still fail, but never
            // without a remote effect.
            context
                .control
                .run(
                    async {
                        source
                            .finish()
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Cleanup, false))
                    },
                    ErrorPhase::Cleanup,
                    true,
                )
                .await
                .map_err(|_| committed_verification_error())?;
            let published = stat_file(
                &mut destination_remote.ftp,
                &request.destination_key,
                context,
            )
            .await
            .map_err(|_| committed_verification_error())?;
            if published.size != transferred || transferred != expected_size {
                return Err(committed_verification_error());
            }
            Ok(published)
        }
        .await;
        result.map_err(|error: StorageError| {
            error
                .with_preparation_effect(parent_effect)
                .with_provider(provider.id())
        })
    }
    .await;
    outcome.map_err(|error: StorageError| error.with_provider(provider.id()))
}
