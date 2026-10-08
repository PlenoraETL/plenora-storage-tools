use super::{smb_error, smb_login_error};
use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};
use smb2::types::{Command, status::NtStatus};

#[test]
fn an_internal_smb_failure_is_reported_as_internal_not_as_io() {
    let error = smb_error(
        &smb2::Error::Internal {
            what: "connection waiter map",
        },
        false,
    );
    assert_eq!(error.category, ErrorCategory::Internal);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(error.retry, RetryDisposition::Never);
}

#[test]
fn an_internal_failure_while_mutating_keeps_the_effect_unknown() {
    let error = smb_error(
        &smb2::Error::Internal {
            what: "connection crypto state",
        },
        true,
    );
    assert_eq!(error.category, ErrorCategory::Internal);
    assert_eq!(error.remote_effect, RemoteEffect::Unknown);
    assert_eq!(error.retry, RetryDisposition::RequiresRecovery);
}

fn session_failure(status: NtStatus) -> smb2::Error {
    smb2::Error::Protocol {
        status,
        command: Command::SessionSetup,
    }
}

fn login_axes(error: &smb2::Error) -> (ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition) {
    let mapped = smb_login_error(error);
    (
        mapped.category,
        mapped.phase,
        mapped.remote_effect,
        mapped.retry,
    )
}

/// The server's logon rejections are the only authentication failures.
#[test]
fn session_setup_logon_rejections_are_authentication_never() {
    for status in [
        NtStatus::LOGON_FAILURE,
        NtStatus::ACCOUNT_DISABLED,
        NtStatus::PASSWORD_EXPIRED,
    ] {
        assert_eq!(
            login_axes(&session_failure(status)),
            (
                ErrorCategory::Authentication,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Never
            )
        );
    }
}

/// A server refusing sessions for lack of resources, a lost connection or a
/// timeout during session setup is transient: nothing happened remotely, so
/// the retry is safe. Before 3.0.0 these were `io` in phase `read` with retry
/// `never`.
#[test]
fn session_setup_transient_refusals_are_safe_to_retry() {
    for error in [
        session_failure(NtStatus::INSUFF_SERVER_RESOURCES),
        session_failure(NtStatus::INSUFFICIENT_RESOURCES),
        session_failure(NtStatus::REQUEST_NOT_ACCEPTED),
        smb2::Error::Disconnected,
    ] {
        assert_eq!(
            login_axes(&error),
            (
                ErrorCategory::Transient,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Safe
            ),
            "{error:?}"
        );
    }
    assert_eq!(
        login_axes(&smb2::Error::Timeout),
        (
            ErrorCategory::Timeout,
            ErrorPhase::Connect,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
}

/// A local failure of the authentication exchange, or a status the client
/// does not expect, is an explicit protocol error and never reported as
/// rejected credentials.
#[test]
fn session_setup_unexpected_failures_are_protocol_not_authentication() {
    for error in [
        smb2::Error::Auth {
            message: "NTLM did not produce a session key".to_string(),
        },
        session_failure(NtStatus::INVALID_PARAMETER),
    ] {
        let mapped = smb_login_error(&error);
        assert_eq!(
            (
                mapped.category,
                mapped.phase,
                mapped.remote_effect,
                mapped.retry
            ),
            (
                ErrorCategory::Protocol,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Never
            ),
            "{error:?}"
        );
        assert!(!mapped.message.contains("NTLM"));
    }
}
