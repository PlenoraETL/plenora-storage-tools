//! Protocol failures translated to redacted public effect and retry axes.

use super::{ErrorCategory, ErrorPhase, PROVIDER_ID, RemoteEffect, RetryDisposition, StorageError};

pub fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "S3_CONFIG_INVALID",
        "S3 connection configuration is invalid",
    )
    .with_provider(PROVIDER_ID)
}

pub fn unrepresentable_key_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "OBJECT_KEY_UNREPRESENTABLE",
        "storage backend returned a name that is not a valid public object key",
    )
    .with_provider(PROVIDER_ID)
}

pub fn transfer_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeds the engine byte limit",
    )
    .with_provider(PROVIDER_ID)
}

pub fn buffered_put_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "BUFFERED_PUT_LIMIT_EXCEEDED",
        "conditional upload exceeds the engine in-memory buffering limit",
    )
    .with_provider(PROVIDER_ID)
}

pub fn committed_verification_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::RequiresRecovery,
        "S3_COMMITTED_METADATA_UNAVAILABLE",
        "S3 destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

pub fn artifact_sink_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Write,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeded the engine byte limit after publishing sink bytes",
    )
    .with_provider(PROVIDER_ID)
}

#[allow(
    clippy::needless_pass_by_value,
    reason = "Consume upstream errors at the redaction boundary and support map_err"
)]
pub fn map_store_error(
    error: object_store::Error,
    phase: ErrorPhase,
    mutating: bool,
) -> StorageError {
    // Preserve local response-validation failures through object_store's HTTP
    // error wrappers, without exposing backend URLs or response contents.
    let mut cause: &dyn std::error::Error = &error;
    loop {
        if let Some(error) = cause.downcast_ref::<StorageError>() {
            return error.clone();
        }
        let Some(source) = cause.source() else {
            break;
        };
        cause = source;
    }
    let (category, effect, retry, code, message) = match error {
        object_store::Error::NotFound { .. } => (
            ErrorCategory::NotFound,
            RemoteEffect::None,
            RetryDisposition::Never,
            "OBJECT_NOT_FOUND",
            "storage object was not found",
        ),
        object_store::Error::AlreadyExists { .. }
        | object_store::Error::Precondition { .. }
        | object_store::Error::NotModified { .. } => (
            ErrorCategory::Conflict,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PRECONDITION_FAILED",
            "storage operation precondition failed",
        ),
        object_store::Error::PermissionDenied { .. } => (
            ErrorCategory::Authorization,
            RemoteEffect::None,
            RetryDisposition::Never,
            "AUTHORIZATION_FAILED",
            "storage provider denied the operation",
        ),
        object_store::Error::Unauthenticated { .. } => (
            ErrorCategory::Authentication,
            RemoteEffect::None,
            RetryDisposition::Never,
            "AUTHENTICATION_FAILED",
            "storage provider rejected the credentials",
        ),
        object_store::Error::NotSupported { .. } | object_store::Error::NotImplemented { .. } => (
            ErrorCategory::Unsupported,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PROVIDER_OPERATION_UNSUPPORTED",
            "storage provider does not support the operation",
        ),
        object_store::Error::InvalidPath { .. }
        | object_store::Error::UnknownConfigurationKey { .. } => (
            ErrorCategory::InvalidConfiguration,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PROVIDER_CONFIGURATION_INVALID",
            "storage provider configuration is invalid",
        ),
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "PROVIDER_MUTATION_FAILED",
            "storage mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "PROVIDER_REQUEST_FAILED",
            "storage provider request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
}
