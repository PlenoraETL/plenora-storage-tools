use super::{
    SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE, arm_response_timeout, response_timeout,
    smb_connect_error, smb_error, smb_login_error,
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

/// An SMB server played in memory by the library's mock transport: no
/// sockets, so paused tokio time drives every wait deterministically.
mod fake_smb {
    use smb2::{
        client::connection::Connection,
        msg::{echo::EchoResponse, header::Header, negotiate::NegotiateResponse},
        pack::{Guid, Pack, ReadCursor, Unpack, WriteCursor},
        transport::MockTransport,
        types::{
            Command, Dialect, MessageId,
            flags::{Capabilities, SecurityMode},
            status::NtStatus,
        },
    };
    use std::{sync::Arc, time::Duration};

    /// A response frame for request `id`.
    pub fn frame(command: Command, status: NtStatus, id: MessageId, body: &dyn Pack) -> Vec<u8> {
        let mut header = Header::new_request(command);
        header.flags.set_response();
        header.credits = 64;
        header.status = status;
        header.message_id = id;
        let mut cursor = WriteCursor::new();
        header.pack(&mut cursor);
        body.pack(&mut cursor);
        cursor.into_inner()
    }

    /// A negotiated connection, armed as the provider arms it for an
    /// operation with `remaining` before its deadline.
    pub async fn connection(remaining: Option<Duration>) -> (Arc<MockTransport>, Connection) {
        let mock = Arc::new(MockTransport::new());
        mock.queue_response(frame(
            Command::Negotiate,
            NtStatus::SUCCESS,
            MessageId(0),
            &NegotiateResponse {
                security_mode: SecurityMode::new(SecurityMode::SIGNING_ENABLED),
                dialect_revision: Dialect::Smb3_0_2,
                server_guid: Guid::ZERO,
                capabilities: Capabilities::new(0),
                max_transact_size: 65_536,
                max_read_size: 65_536,
                max_write_size: 65_536,
                system_time: 0,
                server_start_time: 0,
                security_buffer: vec![0x60, 0x00],
                negotiate_contexts: Vec::new(),
            },
        ));
        let mut conn =
            Connection::from_transport(Box::new(mock.clone()), Box::new(mock.clone()), "fixture")
                .expect("connection");
        super::arm_response_timeout(&conn, remaining);
        conn.negotiate().await.expect("negotiate");
        (mock, conn)
    }

    /// Sends an ECHO and waits for its outcome, with the (virtual) time it
    /// took; `server` plays the server's side once the request is out.
    pub async fn echo<F, Fut>(
        mock: &Arc<MockTransport>,
        conn: &Connection,
        server: F,
    ) -> (smb2::Result<()>, Duration)
    where
        F: FnOnce(Arc<MockTransport>, MessageId) -> Fut,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let started = tokio::time::Instant::now();
        let request = {
            let conn = conn.clone();
            tokio::spawn(async move {
                conn.execute(Command::Echo, &smb2::msg::echo::EchoRequest, None)
                    .await
                    .map(drop)
            })
        };
        while mock.sent_count() < 2 {
            tokio::task::yield_now().await;
        }
        let sent = mock.sent_message(1).expect("echo");
        let id = Header::unpack(&mut ReadCursor::new(&sent))
            .expect("header")
            .message_id;
        let server = tokio::spawn(server(mock.clone(), id));
        let result = request.await.expect("request task");
        server.abort();
        (result, started.elapsed())
    }

    /// A server that never answers.
    pub async fn silent(_: Arc<MockTransport>, _: MessageId) {}

    /// A server that answers `STATUS_PENDING` every 20 s for 180 s, then
    /// completes the request.
    pub async fn pending_for_three_minutes(mock: Arc<MockTransport>, id: MessageId) {
        for _ in 0..9 {
            tokio::time::sleep(Duration::from_secs(20)).await;
            mock.queue_response(frame(Command::Echo, NtStatus::PENDING, id, &EchoResponse));
        }
        mock.queue_response(frame(Command::Echo, NtStatus::SUCCESS, id, &EchoResponse));
    }

    /// A server that keeps talking on the connection every 4 s, but never
    /// answers the request: the keepalive sees a live connection.
    pub async fn chatty(mock: Arc<MockTransport>, _: MessageId) {
        for id in 1_000_u64..1_100 {
            tokio::time::sleep(Duration::from_secs(4)).await;
            mock.queue_response(frame(
                Command::Echo,
                NtStatus::SUCCESS,
                MessageId(id),
                &EchoResponse,
            ));
        }
    }
}

fn axes(error: &smb2::Error, mutating: bool) -> (ErrorCategory, RemoteEffect, RetryDisposition) {
    let error = smb_error(error, mutating);
    (error.category, error.remote_effect, error.retry)
}

const SAFE_TIMEOUT: (ErrorCategory, RemoteEffect, RetryDisposition) = (
    ErrorCategory::Timeout,
    RemoteEffect::None,
    RetryDisposition::Safe,
);
const MUTATING_TIMEOUT: (ErrorCategory, RemoteEffect, RetryDisposition) = (
    ErrorCategory::Timeout,
    RemoteEffect::Unknown,
    RetryDisposition::RequiresRecovery,
);

/// A server that stops answering is given up on after the declared limit
/// without a deadline, and after the time remaining with one. The library
/// reports it as `ServerUnresponsive` (its keepalive got no answer either):
/// that is a `timeout`, with the effect of the operation. Before 3.0.0 the
/// limit was 30 s whatever the deadline, and the failure was `io`.
#[tokio::test(start_paused = true)]
async fn a_silent_smb_server_is_a_timeout_at_the_declared_limit() {
    use std::time::Duration;
    for (remaining, limit) in [
        (None, SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE),
        (Some(Duration::from_secs(600)), Duration::from_secs(600)),
    ] {
        let (mock, conn) = fake_smb::connection(remaining).await;
        let (result, elapsed) = fake_smb::echo(&mock, &conn, fake_smb::silent).await;
        let error = result.expect_err("silence");
        assert!(
            matches!(
                error,
                smb2::Error::ServerUnresponsive { .. } | smb2::Error::Timeout
            ),
            "{error:?}"
        );
        assert!(elapsed >= limit, "{elapsed:?}");
        assert!(elapsed <= limit + Duration::from_secs(2), "{elapsed:?}");
        assert_eq!(axes(&error, false), SAFE_TIMEOUT);
        assert_eq!(axes(&error, true), MUTATING_TIMEOUT);
    }
}

/// `STATUS_PENDING` restarts the wait: an operation the server keeps
/// acknowledging lasts minutes past the declared limit without failing.
#[tokio::test(start_paused = true)]
async fn an_smb_operation_the_server_keeps_pending_is_not_cut() {
    let (mock, conn) = fake_smb::connection(None).await;
    let (result, elapsed) = fake_smb::echo(&mock, &conn, fake_smb::pending_for_three_minutes).await;
    result.expect("completed");
    assert!(
        elapsed >= std::time::Duration::from_secs(180),
        "{elapsed:?}"
    );
}

/// On a connection the keepalive proves alive, a request without an answer
/// waits six times the declared limit, then is a `timeout`, not `io`.
#[tokio::test(start_paused = true)]
async fn an_unanswered_request_on_a_live_smb_connection_is_a_timeout() {
    let (mock, conn) = fake_smb::connection(None).await;
    let (result, elapsed) = fake_smb::echo(&mock, &conn, fake_smb::chatty).await;
    let error = result.expect_err("unanswered");
    let ceiling = SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE * 6;
    assert!(elapsed >= ceiling, "{elapsed:?}");
    assert!(
        elapsed <= ceiling + std::time::Duration::from_secs(2),
        "{elapsed:?}"
    );
    assert_eq!(axes(&error, false), SAFE_TIMEOUT);
    assert_eq!(axes(&error, true), MUTATING_TIMEOUT);
}

/// A connection that ran out of budget on every address is a `timeout` in
/// `connect`, safe to retry because nothing was sent; a refusal, or a mix of
/// refusals and timeouts, stays `io`. Before 3.0.0 every connection failure
/// was `io`, whatever the cause.
#[test]
fn smb_connection_failures_keep_their_cause() {
    use smb2::transport::ConnectAttempt;
    use std::io::ErrorKind;
    let failed = |kinds: &[Option<ErrorKind>]| smb2::Error::ConnectFailed {
        host: "fixture".to_owned(),
        attempts: kinds
            .iter()
            .map(|&error_kind| ConnectAttempt {
                addr: "192.0.2.1:445".parse().expect("address"),
                error_kind,
            })
            .collect(),
    };
    let axes = |error: Option<&smb2::Error>| {
        let error = smb_connect_error(error);
        (
            error.category,
            error.phase,
            error.remote_effect,
            error.retry,
        )
    };
    let timeout = (
        ErrorCategory::Timeout,
        ErrorPhase::Connect,
        RemoteEffect::None,
        RetryDisposition::Safe,
    );
    assert_eq!(axes(Some(&failed(&[None]))), timeout);
    assert_eq!(
        axes(Some(&failed(&[None, Some(ErrorKind::TimedOut)]))),
        timeout
    );
    assert_eq!(axes(Some(&smb2::Error::Timeout)), timeout);
    for refused in [
        failed(&[Some(ErrorKind::ConnectionRefused)]),
        failed(&[None, Some(ErrorKind::ConnectionRefused)]),
        failed(&[]),
    ] {
        assert_eq!(axes(Some(&refused)).0, ErrorCategory::Io, "{refused:?}");
    }
    assert_eq!(axes(None).0, ErrorCategory::Io);
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
