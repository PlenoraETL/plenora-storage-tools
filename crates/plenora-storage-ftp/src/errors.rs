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

/// The reply code of a complete FTP reply, parsed strictly as RFC 959
/// section 4.2 defines it; `None` for any malformation.
///
/// The parsed [`Status`] cannot be used: codes the client library does not
/// name collapse into `Status::Unknown`, and suppaftp accepts a terminal line
/// whose code differs from the opening one while keeping the whole body.
///
/// - A reply ends with its last line; lines end with CRLF or LF. Mixed line
///   ends within one reply are accepted on purpose: they cannot change the
///   code that decides, while rejecting them would turn a real 530 from a
///   careless server into a protocol error. The structure of the codes stays
///   strict.
/// - A single-line reply is one line `NNN` or `NNN text`.
/// - A multiline reply opens with `NNN-text` and ends with `NNN` or
///   `NNN text` carrying the same code. Lines in between are free text, as
///   RFC 959 allows, but none may already be that terminator.
/// - A code is exactly three ASCII digits followed by a space, `-` (opening
///   line only) or the end of the line.
fn reply_code(response: &Response) -> Option<u32> {
    let body = response.body.strip_suffix(b"\n").unwrap_or(&response.body);
    let lines: Vec<&[u8]> = body
        .split(|byte| *byte == b'\n')
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
        .collect();
    let (first, rest) = lines.split_first()?;
    let (code, continued) = line_code(first)?;
    if !continued {
        return rest.is_empty().then_some(code);
    }
    let (last, middle) = rest.split_last()?;
    let terminator = |line: &[u8]| matches!(line_code(line), Some((value, false)) if value == code);
    (terminator(last) && !middle.iter().any(|line| terminator(line))).then_some(code)
}

/// `(code, continued)` for a line opening with a reply code: `continued` is
/// true for `NNN-`. `None` when the line does not open with exactly three
/// digits followed by a space, `-` or the end of the line.
fn line_code(line: &[u8]) -> Option<(u32, bool)> {
    let (digits, after) = line.split_at_checked(3)?;
    if !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let continued = match after.first() {
        None | Some(b' ') => false,
        Some(b'-') => true,
        Some(_) => return None,
    };
    let code = digits
        .iter()
        .fold(0, |value, digit| value * 10 + u32::from(digit - b'0'));
    Some((code, continued))
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
