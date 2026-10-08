use super::{
    SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE, arm_response_timeout, response_timeout, smb_error,
    smb_login_error,
};
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

/// A connection armed for an operation waits for each response as long as
/// the time remaining before the deadline. Before 3.0.0 it kept the library's
/// fixed 30 s whatever the deadline. The wait itself is the library's,
/// covered by its own response-timeout tests; it measures silence with
/// `std::time::Instant`, which paused tokio time cannot drive.
#[tokio::test]
async fn an_armed_smb_connection_waits_as_long_as_the_deadline_allows() {
    let mock = std::sync::Arc::new(smb2::transport::MockTransport::new());
    let conn = smb2::client::connection::Connection::from_transport(
        Box::new(mock.clone()),
        Box::new(mock),
        "fixture",
    )
    .expect("connection");
    arm_response_timeout(&conn, Some(std::time::Duration::from_secs(600)));
    assert_eq!(
        conn.response_timeout(),
        Some(std::time::Duration::from_secs(600))
    );
    arm_response_timeout(&conn, None);
    assert_eq!(
        conn.response_timeout(),
        Some(SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE)
    );
}

/// The response timeout is the time remaining, never zero, and the declared
/// limit without a deadline.
#[test]
fn smb_response_timeout_follows_the_remaining_time() {
    use std::time::Duration;
    assert_eq!(
        response_timeout(Some(Duration::from_secs(600))),
        Duration::from_secs(600)
    );
    assert_eq!(
        response_timeout(Some(Duration::ZERO)),
        Duration::from_millis(1)
    );
    assert_eq!(
        response_timeout(None),
        SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE
    );
}

/// A response the client gave up waiting for is a `timeout` with the effect of
/// the operation; before 3.0.0 it was `io`. A lost connection stays `io`.
#[test]
fn an_smb_timeout_is_reported_as_timeout() {
    let read = smb_error(&smb2::Error::Timeout, false);
    assert_eq!(
        (read.category, read.remote_effect, read.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
    let write = smb_error(&smb2::Error::Timeout, true);
    assert_eq!(
        (write.category, write.remote_effect, write.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery
        )
    );
    assert_eq!(
        smb_error(&smb2::Error::Disconnected, false).category,
        ErrorCategory::Io
    );
}
