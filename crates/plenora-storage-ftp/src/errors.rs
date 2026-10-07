//! Protocol failures translated to redacted public effect and retry axes.

use super::{
    ErrorCategory, ErrorPhase, FtpError, PROVIDER_ID, RemoteEffect, Response, RetryDisposition,
    Status, StorageError,
};

pub fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "FTP_CONFIGURATION_INVALID",
        "FTP configuration does not match its public contract",
    )
    .with_provider(PROVIDER_ID)
}

/// Classifies a failed `USER`/`PASS` exchange by the server's reply code.
///
/// Only 430 (invalid username or password), 530 (not logged in) and 532
/// (account required) reject the credentials. Any other 4xx reply is a
/// transient refusal, for example pure-ftpd's
/// `421 ... users (the maximum) are already logged in`: logging in has no
/// remote effect, so it is safe to retry. Any other reply is unexpected and
/// stays an explicit protocol error; transport failures follow the general
/// mapping. Before 3.0.0 every login failure was reported as
/// `FTP_AUTHENTICATION_FAILED` with retry `never`.
pub fn map_ftp_auth_error(error: FtpError) -> StorageError {
    let FtpError::UnexpectedResponse(response) = error else {
        return match error {
            FtpError::BadResponse => login_protocol_error(),
            other => map_ftp_error(other, ErrorPhase::Connect, false),
        };
    };
    match reply_code(&response) {
        Some(430 | 530 | 532) => StorageError::new(
            ErrorCategory::Authentication,
            ErrorPhase::Connect,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_AUTHENTICATION_FAILED",
            "FTP server rejected the credentials",
        )
        .with_provider(PROVIDER_ID),
        Some(400..=499) => StorageError::new(
            ErrorCategory::Transient,
            ErrorPhase::Connect,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "FTP_LOGIN_TEMPORARILY_REFUSED",
            "FTP server temporarily refused the login",
        )
        .with_provider(PROVIDER_ID),
        _ => login_protocol_error(),
    }
}

/// The three-digit reply code that opens the response line. The parsed
/// [`Status`] cannot be used alone: codes the client library does not name
/// collapse into `Status::Unknown` and would lose their class.
fn reply_code(response: &Response) -> Option<u32> {
    let digits = response.body.get(..3)?;
    if !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(digits).ok()?.parse().ok()
}

fn login_protocol_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Connect,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_LOGIN_UNEXPECTED_RESPONSE",
        "FTP server answered the login with an unexpected response",
    )
    .with_provider(PROVIDER_ID)
}

pub fn map_ftp_error(error: FtpError, phase: ErrorPhase, mutating: bool) -> StorageError {
    let (category, effect, retry, code, message) = match error {
        // Status 550 covers "not found" and several other unavailability
        // conditions. Absence may only be claimed when no mutation was started;
        // otherwise the remote outcome is genuinely ambiguous.
        FtpError::UnexpectedResponse(response)
            if response.status == Status::FileUnavailable && !mutating =>
        {
            (
                ErrorCategory::NotFound,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_OBJECT_NOT_FOUND",
                "FTP object was not found or unavailable",
            )
        }
        FtpError::UnexpectedResponse(response)
            if matches!(
                response.status,
                Status::NotLoggedIn | Status::InvalidCredentials
            ) =>
        {
            (
                ErrorCategory::Authentication,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_AUTHENTICATION_FAILED",
                "FTP server rejected the credentials",
            )
        }
        FtpError::UnexpectedResponse(response) if response.status == Status::ExceededStorage => (
            ErrorCategory::ResourceLimit,
            if mutating {
                RemoteEffect::Unknown
            } else {
                RemoteEffect::None
            },
            if mutating {
                RetryDisposition::RequiresRecovery
            } else {
                RetryDisposition::Never
            },
            "FTP_STORAGE_LIMIT_EXCEEDED",
            "FTP server storage limit was exceeded",
        ),
        FtpError::UnexpectedResponse(response)
            if matches!(
                response.status,
                Status::NotImplemented | Status::NotImplementedParameter
            ) =>
        {
            (
                ErrorCategory::Unsupported,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_OPERATION_UNSUPPORTED",
                "FTP server does not support the operation",
            )
        }
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "FTP_MUTATION_FAILED",
            "FTP mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "FTP_REQUEST_FAILED",
            "FTP request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
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
        "FTP_TRANSFER_IO_FAILED",
        "FTP transfer stream failed",
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
        "FTP listing visited more directory entries than the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}

pub fn list_parse_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_LIST_PARSE_FAILED",
        "FTP MLSD response is invalid",
    )
    .with_provider(PROVIDER_ID)
}

pub fn list_name_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_LIST_NAME_UNREPRESENTABLE",
        "FTP listing returned a name that is not a valid public object key",
    )
    .with_provider(PROVIDER_ID)
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
        "FTP_COMMITTED_METADATA_UNAVAILABLE",
        "FTP destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

/// The published or transferred object does not have the transferred size:
/// a committed effect that differs from the request remains to be reconciled.
pub fn committed_mismatch_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::RequiresRecovery,
        "FTP_COMMITTED_SIZE_MISMATCH",
        "FTP transfer completed with a size that differs from the transferred bytes",
    )
    .with_provider(PROVIDER_ID)
}
