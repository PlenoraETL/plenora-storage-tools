use super::{
    SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE, arm_response_timeout, first_connection,
    response_timeout, smb_error, smb_login_error,
};
use plenora_storage_core::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError,
};
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
            Command, CreditCharge, Dialect, MessageId,
            flags::{Capabilities, SecurityMode},
            status::NtStatus,
        },
    };
    use std::{sync::Arc, time::Duration};

    /// Credits the fake server grants at negotiate.
    pub const GRANTED: u16 = 64;

    /// Yields until `ready` holds. A condition that never comes true fails
    /// the test after 10 s of real time instead of hanging it.
    pub async fn yield_until(mut ready: impl FnMut() -> bool, what: &str) {
        let started = std::time::Instant::now();
        while !ready() {
            assert!(started.elapsed() < Duration::from_secs(10), "{what}");
            tokio::task::yield_now().await;
        }
    }

    /// A response frame for request `id`.
    pub fn frame(command: Command, status: NtStatus, id: MessageId, body: &dyn Pack) -> Vec<u8> {
        let mut header = Header::new_request(command);
        header.flags.set_response();
        header.credits = GRANTED;
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

    /// The message id of the `n`-th frame the client sent.
    pub fn sent_id(mock: &MockTransport, n: usize) -> MessageId {
        let sent = mock.sent_message(n).expect("sent frame");
        Header::unpack(&mut ReadCursor::new(&sent))
            .expect("header")
            .message_id
    }

    /// Sends an ECHO costing `charge` credits.
    pub async fn send_echo(conn: &Connection, charge: u16) -> smb2::Result<()> {
        conn.execute_with_credits(
            Command::Echo,
            &smb2::msg::echo::EchoRequest,
            None,
            CreditCharge(charge),
        )
        .await
        .map(drop)
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

/// Runs one ECHO through the operation control, as the provider runs every
/// SMB operation, while `server` plays the server's side; returns the
/// outcome with the (virtual) time it took.
async fn echo_under_control<F, Fut>(
    remaining: Option<std::time::Duration>,
    mutating: bool,
    server: F,
) -> (plenora_storage_core::StorageResult<()>, std::time::Duration)
where
    F: FnOnce(std::sync::Arc<smb2::transport::MockTransport>, smb2::types::MessageId) -> Fut
        + Send
        + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    let control = remaining.map_or_else(
        plenora_storage_core::ExecutionControl::default,
        |remaining| {
            plenora_storage_core::ExecutionControl::default()
                // On the paused clock: earlier cases of a test have moved
                // it ahead of the real one.
                .with_deadline(tokio::time::Instant::now().into_std() + remaining)
        },
    );
    let (mock, conn) = fake_smb::connection(remaining).await;
    let started = tokio::time::Instant::now();
    let watched = mock.clone();
    let player = tokio::spawn(async move {
        fake_smb::yield_until(|| watched.sent_count() >= 2, "the echo was never sent").await;
        server(watched.clone(), fake_smb::sent_id(&watched, 1)).await;
    });
    let phase = if mutating {
        ErrorPhase::Commit
    } else {
        ErrorPhase::Read
    };
    let result = control
        .run(
            async {
                fake_smb::send_echo(&conn, 1)
                    .await
                    .map_err(|error| smb_error(&error, mutating))
            },
            phase,
            mutating,
        )
        .await;
    let elapsed = started.elapsed();
    player.abort();
    (result, elapsed)
}

fn axes(error: StorageError) -> (ErrorCategory, RemoteEffect, RetryDisposition) {
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
/// without a deadline, and at the deadline with one: a `timeout` with the
/// effect of the operation. The library reports the silence as
/// `ServerUnresponsive` (its keepalive got no answer either). Before 3.0.0
/// the limit was 30 s whatever the deadline, and the failure was `io`.
#[tokio::test(start_paused = true)]
async fn a_silent_smb_server_is_a_timeout_at_the_declared_limit() {
    use std::time::Duration;
    for (remaining, limit) in [
        (None, SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE),
        (Some(Duration::from_secs(600)), Duration::from_secs(600)),
    ] {
        for (mutating, expected) in [(false, SAFE_TIMEOUT), (true, MUTATING_TIMEOUT)] {
            let (result, elapsed) = echo_under_control(remaining, mutating, fake_smb::silent).await;
            assert_eq!(axes(result.expect_err("silence")), expected);
            assert!(elapsed + Duration::from_secs(1) >= limit, "{elapsed:?}");
            assert!(elapsed <= limit + Duration::from_secs(2), "{elapsed:?}");
        }
    }
}

/// `STATUS_PENDING` restarts the wait: an operation the server keeps
/// acknowledging lasts minutes past the declared limit without failing.
#[tokio::test(start_paused = true)]
async fn an_smb_operation_the_server_keeps_pending_is_not_cut() {
    let (result, elapsed) =
        echo_under_control(None, false, fake_smb::pending_for_three_minutes).await;
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
    let (result, elapsed) = echo_under_control(None, true, fake_smb::chatty).await;
    assert_eq!(axes(result.expect_err("unanswered")), MUTATING_TIMEOUT);
    let ceiling = SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE * 6;
    assert!(elapsed >= ceiling, "{elapsed:?}");
    assert!(
        elapsed <= ceiling + std::time::Duration::from_secs(2),
        "{elapsed:?}"
    );
}

/// A request the connection's credits cannot fund, with nothing outstanding
/// to bring more, fails at once as a resource limit of the connection: no
/// wait ran out, so it is not a `timeout`. Before this fix it was reported
/// as one.
#[tokio::test(start_paused = true)]
async fn credits_that_cannot_arrive_are_a_resource_limit_not_a_timeout() {
    let (_mock, conn) = fake_smb::connection(None).await;
    let started = tokio::time::Instant::now();
    let control = plenora_storage_core::ExecutionControl::default();
    let result = control
        .run(
            async {
                fake_smb::send_echo(&conn, fake_smb::GRANTED + 1)
                    .await
                    .map_err(|error| smb_error(&error, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await;
    let error = result.expect_err("unfundable");
    assert_eq!(
        (error.category, error.remote_effect, error.retry),
        (
            ErrorCategory::ResourceLimit,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
    assert_eq!(error.code, "SMB_CREDITS_EXHAUSTED");
    assert_eq!(started.elapsed(), std::time::Duration::ZERO);
}

/// A wait for credits that a pending response could still bring, and that
/// runs out of time, is a `timeout`.
#[tokio::test(start_paused = true)]
async fn a_wait_for_credits_that_runs_out_is_a_timeout() {
    let (mock, conn) = fake_smb::connection(None).await;
    conn.set_credit_wait_timeout(std::time::Duration::from_secs(10));
    // Holds all but one credit and stays unanswered, so its response could
    // still grant more for the whole wait.
    let holder = conn.clone();
    let held =
        tokio::spawn(async move { fake_smb::send_echo(&holder, fake_smb::GRANTED - 1).await });
    fake_smb::yield_until(|| mock.sent_count() >= 2, "the holder was never sent").await;
    let started = tokio::time::Instant::now();
    let result = plenora_storage_core::ExecutionControl::default()
        .run(
            async {
                fake_smb::send_echo(&conn, 2)
                    .await
                    .map_err(|error| smb_error(&error, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await;
    held.abort();
    assert_eq!(axes(result.expect_err("starved")), MUTATING_TIMEOUT);
    let elapsed = started.elapsed();
    assert!(elapsed >= std::time::Duration::from_secs(10), "{elapsed:?}");
    assert!(elapsed <= std::time::Duration::from_secs(11), "{elapsed:?}");
}

/// The address loop of `connect`, with every failure it may meet. The
/// outcome depends on all of them, never on their order: `timeout` only
/// when every address ran out of time, `io` as soon as one refused.
#[tokio::test]
async fn smb_connection_failures_on_several_addresses_keep_their_cause() {
    use smb2::transport::ConnectAttempt;
    use std::{io::ErrorKind, net::SocketAddr};
    #[derive(Clone, Copy, Debug)]
    enum Dial {
        TimedOut,
        Refused,
        Connects,
    }
    let address = |n: u8| SocketAddr::from(([192, 0, 2, n], 445));
    let failed = |address: SocketAddr, error_kind: Option<ErrorKind>| smb2::Error::ConnectFailed {
        host: "fixture".to_owned(),
        attempts: vec![ConnectAttempt {
            addr: address,
            error_kind,
        }],
    };
    let outcome = |plan: Vec<Dial>| async move {
        let addresses: Vec<SocketAddr> = (1..=u8::try_from(plan.len()).expect("few"))
            .map(address)
            .collect();
        let mut dialled = Vec::new();
        let result = first_connection(&addresses, |address| {
            dialled.push(address);
            let dial = plan[dialled.len() - 1];
            async move {
                match dial {
                    Dial::TimedOut => Err(failed(address, None)),
                    Dial::Refused => Err(failed(address, Some(ErrorKind::ConnectionRefused))),
                    Dial::Connects => Ok(address),
                }
            }
        })
        .await;
        (
            result.map_err(|error| {
                (
                    error.category,
                    error.phase,
                    error.remote_effect,
                    error.retry,
                )
            }),
            dialled.len(),
        )
    };
    let timeout = Err((
        ErrorCategory::Timeout,
        ErrorPhase::Connect,
        RemoteEffect::None,
        RetryDisposition::Safe,
    ));
    let io = (ErrorCategory::Io, ErrorPhase::Connect);
    assert_eq!(
        outcome(vec![Dial::TimedOut, Dial::TimedOut]).await,
        (timeout, 2)
    );
    for plan in [
        vec![Dial::Refused, Dial::TimedOut],
        vec![Dial::TimedOut, Dial::Refused],
        vec![Dial::Refused],
    ] {
        let (result, dialled) = outcome(plan.clone()).await;
        let error = result.expect_err("no connection");
        assert_eq!((error.0, error.1), io, "{plan:?}");
        assert_eq!(dialled, plan.len());
    }
    assert_eq!(
        outcome(vec![Dial::Refused, Dial::Connects, Dial::TimedOut]).await,
        (Ok(address(2)), 2)
    );
    let error = first_connection::<(), _, _>(&[], |_| async { Err(smb2::Error::Disconnected) })
        .await
        .expect_err("no address");
    assert_eq!(error.category, ErrorCategory::Io);
}

/// The same loop with the real dialler: two loopback ports held by sockets
/// that are bound but not listening, so they stay reserved for the whole test
/// and refuse every connection. The connection fails as `io`, not `timeout`.
#[tokio::test]
async fn refused_smb_connections_are_io() {
    let mut held = Vec::new();
    for _ in 0..2 {
        let socket = tokio::net::TcpSocket::new_v4().expect("socket");
        socket
            .bind(std::net::SocketAddr::from(([127, 0, 0, 1], 0)))
            .expect("bind");
        held.push(socket);
    }
    let addresses: Vec<_> = held
        .iter()
        .map(|socket| socket.local_addr().expect("address"))
        .collect();
    let error = first_connection(&addresses, |address| async move {
        smb2::client::connection::Connection::connect(
            &address.to_string(),
            std::time::Duration::from_secs(5),
        )
        .await
    })
    .await
    .map(drop)
    .expect_err("refused");
    assert_eq!(
        (error.category, error.phase),
        (ErrorCategory::Io, ErrorPhase::Connect)
    );
    drop(held);
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
