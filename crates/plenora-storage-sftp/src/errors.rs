//! Protocol failures translated to redacted public effect and retry axes.

use super::{
    ErrorCategory, ErrorPhase, PROVIDER_ID, RemoteEffect, RetryDisposition, SftpError, StatusCode,
    StorageError,
};

pub fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "SFTP_CONFIGURATION_INVALID",
        "SFTP configuration does not match its public contract",
    )
    .with_provider(PROVIDER_ID)
}

pub fn map_sftp_error(error: SftpError, phase: ErrorPhase, mutating: bool) -> StorageError {
    let (category, effect, retry, code, message) = match error {
        SftpError::Status(status) if status.status_code == StatusCode::NoSuchFile => (
            ErrorCategory::NotFound,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_OBJECT_NOT_FOUND",
            "SFTP object was not found",
        ),
        SftpError::Status(status) if status.status_code == StatusCode::PermissionDenied => (
            ErrorCategory::Authorization,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_AUTHORIZATION_FAILED",
            "SFTP server denied the operation",
        ),
        SftpError::Status(status) if status.status_code == StatusCode::OpUnsupported => (
            ErrorCategory::Unsupported,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_OPERATION_UNSUPPORTED",
            "SFTP server does not support the operation",
        ),
        SftpError::Timeout => (
            ErrorCategory::Timeout,
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
            "SFTP_TIMEOUT",
            "SFTP operation timed out",
        ),
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "SFTP_MUTATION_FAILED",
            "SFTP mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "SFTP_REQUEST_FAILED",
            "SFTP request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
}

#[allow(
    clippy::needless_pass_by_value,
    reason = "Consume upstream errors at the redaction boundary and support map_err"
)]
pub fn map_ssh_connect_error(error: russh::Error) -> StorageError {
    let (category, retry, code, message) = match error {
        russh::Error::UnknownKey | russh::Error::KeyChanged { .. } => (
            ErrorCategory::Authentication,
            RetryDisposition::Never,
            "SFTP_HOST_KEY_REJECTED",
            "SFTP server host key was rejected",
        ),
        russh::Error::ConnectionTimeout
        | russh::Error::KeepaliveTimeout
        | russh::Error::InactivityTimeout => (
            ErrorCategory::Timeout,
            RetryDisposition::Safe,
            "SFTP_CONNECT_TIMEOUT",
            "SFTP connection timed out",
        ),
        _ => (
            ErrorCategory::Transient,
            RetryDisposition::Safe,
            "SFTP_CONNECT_FAILED",
            "SFTP connection failed",
        ),
    };
    StorageError::new(
        category,
        ErrorPhase::Connect,
        RemoteEffect::None,
        retry,
        code,
        message,
    )
    .with_provider(PROVIDER_ID)
}

pub fn transfer_io_error(phase: ErrorPhase, mutating: bool) -> StorageError {
    StorageError::new(
        ErrorCategory::Io,
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
        "SFTP_TRANSFER_IO_FAILED",
        "SFTP transfer stream failed",
    )
    .with_provider(PROVIDER_ID)
}

pub fn mutation_io_error(phase: ErrorPhase) -> StorageError {
    transfer_io_error(phase, true)
}

/// The destination is published, but its metadata could not be read back.
/// Publication is proved and nothing remains to reconcile, while repeating
/// the request would publish again: `committed`, never retried (case 9e of the
/// common runtime matrix, pending ratification in `plenora-contracts`).
pub fn committed_verification_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::Never,
        "SFTP_COMMITTED_METADATA_UNAVAILABLE",
        "SFTP destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

/// Maps a failed exclusive create.
///
/// Only the generic failure status proves the destination already exists, which
/// is how SFTP v3 reports a rejected exclusive create. Permission, unsupported
/// and transport failures are reported as themselves, so a timeout raised after
/// the server created the file is never announced as a conflict without effect.
pub fn map_exclusive_open_error(error: SftpError, code: &'static str) -> StorageError {
    match &error {
        SftpError::Status(status) if status.status_code == StatusCode::Failure => {
            conflict_error(code)
        }
        _ => map_sftp_error(error, ErrorPhase::Prepare, true),
    }
}

pub fn conflict_error(code: &'static str) -> StorageError {
    StorageError::new(
        ErrorCategory::Conflict,
        ErrorPhase::Prepare,
        RemoteEffect::None,
        RetryDisposition::Never,
        code,
        "SFTP destination already exists or could not be created exclusively",
    )
    .with_provider(PROVIDER_ID)
}

pub fn transfer_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeds the engine byte limit",
    )
    .with_provider(PROVIDER_ID)
}

pub fn list_scan_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "LIST_SCAN_LIMIT_EXCEEDED",
        "SFTP listing visited more directory entries than the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}
