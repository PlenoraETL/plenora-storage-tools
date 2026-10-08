mod operations;
mod spool;
mod spooled;

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

/// Supplies connection validation and backend construction for an adapter.
///
/// Implementations must declare only publication and metadata guarantees they
/// enforce. The shared provider validates requests before consuming upload data
/// and treats interrupted mutations conservatively when their effect is unknown.
#[async_trait]
pub trait ProviderFactory: Send + Sync + 'static {
    /// Stable backend identifier.
    const ID: &'static str;
    /// Accepted versioned connection contract.
    const CONTRACT: &'static str;
    /// Whether this backend can enforce atomic publication under its documented conditions.
    const ATOMIC: bool;
    /// Whether content type and custom metadata can be persisted.
    const METADATA: bool = false;
    /// Whether this adapter implements publication from a private prepared file.
    const SPOOLED_PUT: bool = false;
    /// Maximum prepared upload/copy size admitted by this backend's protocol path.
    const SPOOLED_MAX_BYTES: u64 = u64::MAX;
    /// Checks configuration and engine policy without opening a connection.
    ///
    /// # Errors
    /// Returns a public configuration or policy error without credential values.
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
    async fn put_file(&mut self, _: &PutRequest, _: tokio::fs::File, _: u64) -> StorageResult<()> {
        Err(StorageError::unsupported(
            "provider does not support prepared uploads",
        ))
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()>;
}

/// Provider with streaming downloads and bounded, fully validated uploads.
///
/// Upload and copy buffers are limited by `max_buffered_put_bytes` by default.
/// Explicit private-file preparation uses `max_transfer_bytes` and protocol limits.
/// The limit is per operation; callers must also bound aggregate concurrency.
/// Backend connection, commit and read failures retain their error effect axes.
pub struct Provider<F: ProviderFactory> {
    credentials: Arc<dyn CredentialResolver>,
    factory: PhantomData<F>,
    spooled_uploads: bool,
}

impl<F: ProviderFactory> Provider<F> {
    /// Retains a resolver without contacting the backend or requesting secrets.
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self {
            credentials,
            factory: PhantomData,
            spooled_uploads: false,
        }
    }

    /// Opt into private disk preparation instead of retaining upload/copy payloads in memory.
    /// Total bytes remain bounded by `EngineConfig::max_transfer_bytes`.
    ///
    /// # Errors
    /// Returns unsupported when the backend has no prepared-file publication path.
    pub fn with_spooled_uploads(credentials: Arc<dyn CredentialResolver>) -> StorageResult<Self> {
        if !F::SPOOLED_PUT {
            return Err(StorageError::unsupported(
                "provider does not support prepared uploads",
            ));
        }
        Ok(Self {
            credentials,
            factory: PhantomData,
            spooled_uploads: true,
        })
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

    fn validate_put(
        &self,
        request: &PutRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<()> {
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
        if request.content_length.is_some_and(|count| {
            count
                > if self.spooled_uploads {
                    context.policy.max_transfer_bytes.min(F::SPOOLED_MAX_BYTES)
                } else {
                    buffer_limit(context)
                }
        }) {
            return Err(limit_error());
        }
        Ok(())
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
        std::io::ErrorKind::StorageFull | std::io::ErrorKind::QuotaExceeded => {
            ErrorCategory::ResourceLimit
        }
        _ => ErrorCategory::Io,
    };
    failure(category, phase, mutating)
}
#[cfg(any(
    feature = "azure",
    feature = "gcs",
    feature = "webdav",
    feature = "smb"
))]
/// A client library gave up waiting for the server. Without a mutation
/// nothing happened and the retry is safe; during one the outcome is unknown.
pub fn timed_out(phase: ErrorPhase, mutating: bool) -> StorageError {
    StorageError::new(
        ErrorCategory::Timeout,
        phase,
        if mutating {
            RemoteEffect::Unknown
        } else {
            RemoteEffect::None
        },
        if mutating {
            RetryDisposition::RequiresRecovery
        } else {
            RetryDisposition::Safe
        },
        "PROVIDER_REQUEST_TIMED_OUT",
        "storage provider request timed out",
    )
}

#[cfg(any(feature = "azure", feature = "gcs", feature = "webdav"))]
/// Whether a client error, or any error it wraps, reports that a request
/// timed out.
pub fn is_timeout(error: &(dyn std::error::Error + 'static)) -> bool {
    let mut current = Some(error);
    while let Some(error) = current {
        if error
            .downcast_ref::<reqwest::Error>()
            .is_some_and(reqwest::Error::is_timeout)
        {
            return true;
        }
        #[cfg(feature = "azure")]
        if error
            .downcast_ref::<object_store::client::HttpError>()
            .is_some_and(|error| error.kind() == object_store::client::HttpErrorKind::Timeout)
        {
            return true;
        }
        if error
            .downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::TimedOut)
        {
            return true;
        }
        current = error.source();
    }
    false
}

#[cfg(any(feature = "azure", feature = "gcs", feature = "webdav"))]
/// A transport failure: a timeout when the client gave up waiting, `io`
/// otherwise.
pub fn transport_failure(
    error: &(dyn std::error::Error + 'static),
    phase: ErrorPhase,
    mutating: bool,
) -> StorageError {
    if is_timeout(error) {
        timed_out(phase, mutating)
    } else {
        failure(ErrorCategory::Io, phase, mutating)
    }
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

#[cfg(test)]
#[path = "common_tests.rs"]
mod tests;
