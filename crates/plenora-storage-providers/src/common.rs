use std::{collections::BTreeMap, marker::PhantomData, sync::Arc};

use async_trait::async_trait;
use bytes::Bytes;
use plenora_storage_core::{
    ArtifactMetadata, CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, EngineConfig,
    ErrorCategory, ErrorPhase, GetRequest, IntegrityMetadata, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StatRequest, StorageError,
    StorageProvider, StorageResult, TestResult, TransferResult, key_matches_prefix,
    validate_object_key, validate_object_prefix,
};
use serde::de::DeserializeOwned;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

/// Factory used by the additional provider adapters.
#[async_trait]
pub trait ProviderFactory: Send + Sync + 'static {
    const ID: &'static str;
    const CONTRACT: &'static str;
    const ATOMIC: bool;
    const METADATA: bool = false;
    fn validate(connection: &ProviderConnection, policy: &EngineConfig) -> StorageResult<()>;
    #[doc(hidden)]
    async fn connect(
        connection: &ProviderConnection,
        credentials: &dyn CredentialResolver,
        context: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>>;
}

#[doc(hidden)]
#[async_trait]
pub trait Reader: Send {
    async fn next(&mut self) -> StorageResult<Option<Bytes>>;
    async fn close(&mut self) -> StorageResult<()> {
        Ok(())
    }
}

#[doc(hidden)]
#[async_trait]
pub trait Backend: Send {
    async fn test(&mut self) -> StorageResult<()>;
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult>;
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata>;
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)>;
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()>;
    async fn delete(&mut self, key: &str) -> StorageResult<()>;
}

/// Provider with streaming downloads and bounded, fully validated uploads.
/// Upload and copy buffers are limited by `max_buffered_put_bytes`.
pub struct Provider<F: ProviderFactory> {
    credentials: Arc<dyn CredentialResolver>,
    factory: PhantomData<F>,
}

impl<F: ProviderFactory> Provider<F> {
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self {
            credentials,
            factory: PhantomData,
        }
    }

    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        self.validate_connection(connection, context.policy)?;
        context
            .control
            .run(
                F::connect(connection, self.credentials.as_ref(), context),
                ErrorPhase::Connect,
                false,
            )
            .await
            .map_err(|error| error.with_provider(F::ID))
    }

    fn validate_put(request: &PutRequest, context: &OperationContext<'_>) -> StorageResult<()> {
        validate_object_key(&request.key)?;
        if request.publication_policy == PublicationPolicy::AtomicRequired && !F::ATOMIC {
            return Err(StorageError::unsupported(
                "this provider cannot guarantee atomic publication",
            ));
        }
        if !F::METADATA && (request.content_type.is_some() || !request.metadata.is_empty()) {
            return Err(StorageError::unsupported(
                "this provider does not persist content type or custom metadata",
            ));
        }
        ArtifactMetadata {
            content_type: request.content_type.clone(),
            ..ArtifactMetadata::default()
        }
        .validate()?;
        if request.metadata.len() > 32
            || request.metadata.iter().any(|(k, v)| {
                k.is_empty()
                    || k.len() > 128
                    || v.len() > 1024
                    || k.chars().any(char::is_control)
                    || v.chars().any(char::is_control)
            })
        {
            return Err(invalid("PUT_METADATA_INVALID"));
        }
        if request
            .content_length
            .is_some_and(|count| count > buffer_limit(context))
        {
            return Err(limit_error());
        }
        Ok(())
    }
}

#[async_trait]
impl<F: ProviderFactory> StorageProvider for Provider<F> {
    fn id(&self) -> &'static str {
        F::ID
    }
    fn config_contract(&self) -> &'static str {
        F::CONTRACT
    }
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        policy: &EngineConfig,
    ) -> StorageResult<()> {
        connection.validate()?;
        if connection.provider != F::ID || connection.config_contract != F::CONTRACT {
            return Err(invalid("PROVIDER_CONFIG_MISMATCH"));
        }
        F::validate(connection, policy).map_err(|error| error.with_provider(F::ID))
    }
    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: F::ID.to_owned(),
            config_contract: F::CONTRACT.to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .map(str::to_owned)
                .to_vec(),
            attributes: BTreeMap::from([
                ("api".to_owned(), F::ID.to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "false".to_owned()),
                (
                    "put_buffer_limit".to_owned(),
                    "max_buffered_put_bytes".to_owned(),
                ),
                (
                    "copy_buffer_limit".to_owned(),
                    "max_buffered_put_bytes".to_owned(),
                ),
                ("atomic_publication".to_owned(), F::ATOMIC.to_string()),
                ("put_create_if_absent_atomic".to_owned(), "true".to_owned()),
                ("copy_create_if_absent_atomic".to_owned(), "true".to_owned()),
                ("list_order".to_owned(), "lexicographic".to_owned()),
            ]),
        }
    }
    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let outcome = async {
            let mut backend = self.connect(connection, context).await?;
            context
                .control
                .run(backend.test(), ErrorPhase::Probe, false)
                .await?;
            Ok(TestResult {
                provider: F::ID.to_owned(),
                reachable: true,
            })
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
        let outcome = async {
            validate_object_prefix(request.prefix.as_deref().unwrap_or_default())?;
            if let Some(after) = &request.start_after {
                validate_object_key(after)?;
            }
            let limit = request.max_items.unwrap_or(1000);
            if limit == 0 || limit > context.policy.max_list_items {
                return Err(limit_error());
            }
            let mut backend = self.connect(connection, context).await?;
            context
                .control
                .run(backend.list(request, limit), ErrorPhase::Read, false)
                .await
                .map_err(|error| error.with_provider(F::ID))
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let outcome = async {
            validate_object_key(&request.key)?;
            let mut backend = self.connect(connection, context).await?;
            context
                .control
                .run(backend.stat(&request.key), ErrorPhase::Read, false)
                .await
                .map_err(|error| error.with_provider(F::ID))
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let outcome = async {
            validate_object_key(&request.key)?;
            let mut backend = self.connect(connection, context).await?;
            let (meta, mut reader) = context
                .control
                .run(backend.get(&request.key), ErrorPhase::Read, false)
                .await?;
            if meta.size > context.policy.max_transfer_bytes {
                return Err(limit_error());
            }
            let mut size = 0_u64;
            let mut digest = Sha256::new();
            loop {
                let next = context
                    .control
                    .run(reader.next(), ErrorPhase::Read, size > 0)
                    .await
                    .map_err(|error| {
                        if size > 0 {
                            error.cleanup_unconfirmed("artifact_sink_may_be_partial")
                        } else {
                            error
                        }
                    })?;
                let Some(chunk) = next else {
                    break;
                };
                if size.saturating_add(chunk.len() as u64) > context.policy.max_transfer_bytes {
                    return Err(limit_error().cleanup_unconfirmed("artifact_sink_may_be_partial"));
                }
                context
                    .control
                    .run(
                        async {
                            sink.write_all(&chunk)
                                .await
                                .map_err(|error| io_error(&error, ErrorPhase::Write, true))
                        },
                        ErrorPhase::Write,
                        true,
                    )
                    .await?;
                size += chunk.len() as u64;
                digest.update(&chunk);
            }
            context
                .control
                .run(reader.close(), ErrorPhase::Cleanup, size > 0)
                .await
                .map_err(|error| error.cleanup_unconfirmed("artifact_sink_may_be_partial"))?;
            if size != meta.size {
                return Err(invalid("CONTENT_LENGTH_MISMATCH")
                    .cleanup_unconfirmed("artifact_sink_may_be_partial"));
            }
            context
                .control
                .run(
                    async {
                        sink.flush()
                            .await
                            .map_err(|error| io_error(&error, ErrorPhase::Write, true))
                    },
                    ErrorPhase::Write,
                    true,
                )
                .await?;
            Ok(transfer(&request.key, size, digest))
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn put(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let outcome = async {
            Self::validate_put(request, context)?;
            self.validate_connection(connection, context.policy)?;
            let mut data = Vec::new();
            let mut buffer = vec![0; 64 * 1024];
            loop {
                let count = context
                    .control
                    .run(
                        async {
                            source
                                .read(&mut buffer)
                                .await
                                .map_err(|error| io_error(&error, ErrorPhase::Read, false))
                        },
                        ErrorPhase::Read,
                        false,
                    )
                    .await?;
                if count == 0 {
                    break;
                }
                if data.len().saturating_add(count) as u64 > buffer_limit(context) {
                    return Err(limit_error());
                }
                data.extend_from_slice(&buffer[..count]);
            }
            if request
                .content_length
                .is_some_and(|count| count != data.len() as u64)
            {
                return Err(invalid("CONTENT_LENGTH_MISMATCH"));
            }
            let size = data.len() as u64;
            let digest = Sha256::new_with_prefix(&data);
            let mut backend = self.connect(connection, context).await?;
            context
                .control
                .run(backend.put(request, data.into()), ErrorPhase::Commit, true)
                .await
                .map_err(|error| error.with_provider(F::ID))?;
            let mut result = transfer(&request.key, size, digest);
            result
                .artifact
                .content_type
                .clone_from(&request.content_type);
            Ok(result)
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        let outcome = async {
            validate_object_key(&request.key)?;
            let mut backend = self.connect(connection, context).await?;
            let result = context
                .control
                .run(backend.delete(&request.key), ErrorPhase::Commit, true)
                .await;
            let deleted = match result {
                Ok(()) => true,
                Err(error)
                    if request.ignore_missing && error.category == ErrorCategory::NotFound =>
                {
                    false
                }
                Err(error) => return Err(error.with_provider(F::ID)),
            };
            Ok(DeleteResult {
                key: request.key.clone(),
                deleted,
            })
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let outcome = async {
            validate_object_key(&request.source_key)?;
            let put = PutRequest {
                key: request.destination_key.clone(),
                overwrite: request.overwrite,
                publication_policy: request.publication_policy,
                content_type: None,
                content_length: None,
                metadata: BTreeMap::new(),
            };
            Self::validate_put(&put, context)?;
            if request.source_key == request.destination_key {
                return Err(invalid("COPY_TARGET_EQUALS_SOURCE"));
            }
            let mut backend = self.connect(connection, context).await?;
            let (meta, mut reader) = context
                .control
                .run(backend.get(&request.source_key), ErrorPhase::Read, false)
                .await?;
            if meta.size > buffer_limit(context) {
                return Err(limit_error());
            }
            let mut data = Vec::new();
            while let Some(chunk) = context
                .control
                .run(reader.next(), ErrorPhase::Read, false)
                .await?
            {
                if data.len().saturating_add(chunk.len()) as u64 > buffer_limit(context) {
                    return Err(limit_error());
                }
                data.extend_from_slice(&chunk);
            }
            context
                .control
                .run(reader.close(), ErrorPhase::Cleanup, false)
                .await?;
            if data.len() as u64 != meta.size {
                return Err(invalid("CONTENT_LENGTH_MISMATCH"));
            }
            context
                .control
                .run(backend.put(&put, data.into()), ErrorPhase::Commit, true)
                .await
                .map_err(|error| error.with_provider(F::ID))?;
            // Size is known from the bytes published; no fallible read-after-write needed.
            Ok(metadata(&request.destination_key, meta.size))
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(F::ID))
    }
}

pub fn parse<T: DeserializeOwned>(connection: &ProviderConnection) -> StorageResult<T> {
    serde_json::from_value(connection.config.clone())
        .map_err(|_| invalid("PROVIDER_CONFIG_INVALID"))
}
pub fn invalid(code: &str) -> StorageError {
    StorageError::invalid_configuration(code, "storage configuration or request is invalid")
}
pub fn limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Validate,
        RemoteEffect::None,
        RetryDisposition::Never,
        "TRANSFER_OR_LIST_LIMIT_EXCEEDED",
        "operation exceeds configured resource limits",
    )
}
pub fn buffer_limit(context: &OperationContext<'_>) -> u64 {
    context
        .policy
        .max_transfer_bytes
        .min(context.policy.max_buffered_put_bytes)
}
pub fn metadata(key: &str, size: u64) -> ObjectMetadata {
    ObjectMetadata {
        key: key.to_owned(),
        size,
        last_modified: None,
        etag: None,
        version: None,
    }
}
fn transfer(key: &str, size: u64, digest: Sha256) -> TransferResult {
    let hash = hex::encode(digest.finalize());
    TransferResult {
        key: key.to_owned(),
        bytes_transferred: size,
        checksum: IntegrityMetadata {
            algorithm: "sha256".to_owned(),
            value: hash.clone(),
        },
        artifact: ArtifactMetadata {
            content_type: None,
            size: Some(size),
            sha256: Some(hash),
        },
        etag: None,
        version: None,
    }
}
pub fn io_error(error: &std::io::Error, phase: ErrorPhase, mutating: bool) -> StorageError {
    let category = match error.kind() {
        std::io::ErrorKind::NotFound => ErrorCategory::NotFound,
        std::io::ErrorKind::AlreadyExists => ErrorCategory::Conflict,
        std::io::ErrorKind::PermissionDenied => ErrorCategory::Authorization,
        _ => ErrorCategory::Io,
    };
    failure(category, phase, mutating)
}
pub fn failure(category: ErrorCategory, phase: ErrorPhase, mutating: bool) -> StorageError {
    let effect = mutating
        && !matches!(
            category,
            ErrorCategory::NotFound
                | ErrorCategory::Conflict
                | ErrorCategory::Authentication
                | ErrorCategory::Authorization
        );
    StorageError::new(
        category,
        phase,
        if effect {
            RemoteEffect::Unknown
        } else {
            RemoteEffect::None
        },
        if effect {
            RetryDisposition::RequiresRecovery
        } else {
            RetryDisposition::Never
        },
        "PROVIDER_OPERATION_FAILED",
        "storage provider operation failed",
    )
}
pub fn select(
    selected: &mut BTreeMap<String, ObjectMetadata>,
    item: ObjectMetadata,
    request: &ProviderListRequest,
    limit: usize,
) -> StorageResult<()> {
    validate_object_key(&item.key)
        .map_err(|_| failure(ErrorCategory::Protocol, ErrorPhase::Read, false))?;
    if key_matches_prefix(&item.key, request.prefix.as_deref().unwrap_or_default())
        && request.start_after.as_ref().is_none_or(|k| item.key > *k)
    {
        selected.insert(item.key.clone(), item);
        if selected.len() > limit + 1 {
            selected.pop_last();
        }
    }
    Ok(())
}
pub fn page(mut selected: BTreeMap<String, ObjectMetadata>, limit: usize) -> ProviderListResult {
    let truncated = selected.len() > limit;
    if truncated {
        selected.pop_last();
    }
    let next_start_after = if truncated {
        selected.last_key_value().map(|(k, _)| k.clone())
    } else {
        None
    };
    ProviderListResult {
        objects: selected.into_values().collect(),
        truncated,
        next_start_after,
    }
}
