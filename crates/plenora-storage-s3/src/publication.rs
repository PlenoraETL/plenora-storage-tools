//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    AmazonS3, ArtifactMetadata, AsyncRead, AsyncReadExt, CopyMode, CopyOptions, CopyRequest,
    Digest, ErrorCategory, ErrorPhase, ObjectMetadata, ObjectStore, ObjectStoreExt,
    OperationContext, PROVIDER_ID, Path, ProviderConnection, PutMode, PutMultipartOptions,
    PutOptions, PutRequest, RemoteEffect, RetryDisposition, S3Provider, Sha256, StorageError,
    StorageResult, TransferResult, WriteMultipart, abort_multipart, buffered_put_limit_error,
    committed_verification_error, map_store_error, public_metadata, put_attributes, required_path,
    sha256_metadata, transfer_limit_error, validate_metadata,
};

#[allow(
    clippy::too_many_lines,
    reason = "Keep multipart initiation, abort ownership and final ambiguous commit in one state sequence"
)]
pub async fn put(
    provider: &S3Provider,
    connection: &ProviderConnection,
    request: &PutRequest,
    source: &mut (dyn AsyncRead + Send + Unpin),
    context: &OperationContext<'_>,
) -> StorageResult<TransferResult> {
    if request
        .content_length
        .is_some_and(|length| length > context.policy.max_transfer_bytes)
    {
        return Err(transfer_limit_error());
    }
    validate_metadata(request)?;
    let path = required_path(&request.key)?;
    let store = provider.connect(connection, context).await?;
    if !request.overwrite {
        return conditional_put(&store, &path, request, source, context).await;
    }
    let options = PutMultipartOptions {
        attributes: put_attributes(request),
        ..PutMultipartOptions::default()
    };
    let upload = context
        .control
        .run(
            async {
                store
                    .put_multipart_opts(&path, options)
                    .await
                    .map_err(|error| map_store_error(error, ErrorPhase::Prepare, true))
            },
            ErrorPhase::Prepare,
            true,
        )
        .await?;
    let mut writer = WriteMultipart::new(upload);
    let mut buffer = vec![0_u8; 64 * 1024];
    let mut transferred = 0_u64;
    let mut digest = Sha256::new();
    loop {
        let read = match context
            .control
            .run(
                async {
                    source.read(&mut buffer).await.map_err(|_| {
                        StorageError::new(
                            ErrorCategory::Io,
                            ErrorPhase::Read,
                            RemoteEffect::Unknown,
                            RetryDisposition::RequiresRecovery,
                            "ARTIFACT_READ_FAILED",
                            "artifact source read failed",
                        )
                    })
                },
                ErrorPhase::Read,
                true,
            )
            .await
        {
            Ok(read) => read,
            Err(error) => return Err(abort_multipart(writer, error).await),
        };
        if read == 0 {
            break;
        }
        transferred = match transferred.checked_add(read as u64) {
            Some(total) if total <= context.policy.max_transfer_bytes => total,
            _ => return Err(abort_multipart(writer, transfer_limit_error()).await),
        };
        digest.update(&buffer[..read]);
        writer.write(&buffer[..read]);
        if let Err(error) = context
            .control
            .run(
                async {
                    writer
                        .wait_for_capacity(4)
                        .await
                        .map_err(|source| map_store_error(source, ErrorPhase::Write, true))
                },
                ErrorPhase::Write,
                true,
            )
            .await
        {
            return Err(abort_multipart(writer, error).await);
        }
    }
    if request
        .content_length
        .is_some_and(|expected| expected != transferred)
    {
        let error = StorageError::invalid_configuration(
            "CONTENT_LENGTH_MISMATCH",
            "artifact length differs from declared content_length",
        )
        .with_provider(PROVIDER_ID);
        return Err(abort_multipart(writer, error).await);
    }
    let result = context
        .control
        .run(
            async move {
                writer
                    .finish()
                    .await
                    .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await?;
    let checksum = sha256_metadata(digest);
    Ok(TransferResult {
        key: request.key.clone(),
        bytes_transferred: transferred,
        artifact: ArtifactMetadata {
            content_type: request.content_type.clone(),
            size: Some(transferred),
            sha256: Some(checksum.value.clone()),
        },
        checksum,
        etag: result.e_tag,
        version: result.version,
    })
}

pub async fn copy(
    provider: &S3Provider,
    connection: &ProviderConnection,
    request: &CopyRequest,
    context: &OperationContext<'_>,
) -> StorageResult<ObjectMetadata> {
    if !request.overwrite {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "S3_COPY_CREATE_IF_ABSENT_UNSUPPORTED",
            "this S3 adapter cannot guarantee atomic create-if-absent for copy",
        )
        .with_provider(PROVIDER_ID));
    }
    let source = required_path(&request.source_key)?;
    let destination = required_path(&request.destination_key)?;
    if source == destination {
        return Err(StorageError::invalid_configuration(
            "COPY_TARGET_EQUALS_SOURCE",
            "copy source and destination must differ",
        ));
    }
    let store = provider.connect(connection, context).await?;
    context
        .control
        .run(
            async {
                store
                    .copy_opts(
                        &source,
                        &destination,
                        CopyOptions::new().with_mode(CopyMode::Overwrite),
                    )
                    .await
                    .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await?;
    // The destination is published from here on. The mapping is applied to
    // the outcome of `run`, so a deadline or cancellation raised during the
    // read-back is also reported as committed rather than unknown.
    context
        .control
        .run(
            async {
                store
                    .head(&destination)
                    .await
                    .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                    .and_then(public_metadata)
            },
            ErrorPhase::Cleanup,
            true,
        )
        .await
        .map_err(|_| committed_verification_error())
}

async fn conditional_put(
    store: &AmazonS3,
    path: &Path,
    request: &PutRequest,
    source: &mut (dyn AsyncRead + Send + Unpin),
    context: &OperationContext<'_>,
) -> StorageResult<TransferResult> {
    // Native create-if-absent is a single conditional PUT, so the whole
    // artifact must be buffered. That memory is bounded separately from
    // the streaming transfer limit.
    let buffered_limit = context
        .policy
        .max_buffered_put_bytes
        .min(context.policy.max_transfer_bytes);
    if request
        .content_length
        .is_some_and(|length| length > buffered_limit)
    {
        return Err(buffered_put_limit_error());
    }
    let mut payload = Vec::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    let mut digest = Sha256::new();
    loop {
        let read = context
            .control
            .run(
                async {
                    source.read(&mut buffer).await.map_err(|_| {
                        StorageError::new(
                            ErrorCategory::Io,
                            ErrorPhase::Read,
                            RemoteEffect::None,
                            RetryDisposition::Safe,
                            "ARTIFACT_READ_FAILED",
                            "artifact source read failed before conditional publication",
                        )
                    })
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        if read == 0 {
            break;
        }
        if payload.len().saturating_add(read) as u64 > buffered_limit {
            return Err(buffered_put_limit_error());
        }
        digest.update(&buffer[..read]);
        payload.extend_from_slice(&buffer[..read]);
    }
    let transferred = payload.len() as u64;
    if request
        .content_length
        .is_some_and(|expected| expected != transferred)
    {
        return Err(StorageError::invalid_configuration(
            "CONTENT_LENGTH_MISMATCH",
            "artifact length differs from declared content_length",
        )
        .with_provider(PROVIDER_ID));
    }
    let options = PutOptions {
        mode: PutMode::Create,
        attributes: put_attributes(request),
        ..PutOptions::default()
    };
    let result = context
        .control
        .run(
            async {
                store
                    .put_opts(path, payload.into(), options)
                    .await
                    .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await?;
    let checksum = sha256_metadata(digest);
    Ok(TransferResult {
        key: request.key.clone(),
        bytes_transferred: transferred,
        artifact: ArtifactMetadata {
            content_type: request.content_type.clone(),
            size: Some(transferred),
            sha256: Some(checksum.value.clone()),
        },
        checksum,
        etag: result.e_tag,
        version: result.version,
    })
}
