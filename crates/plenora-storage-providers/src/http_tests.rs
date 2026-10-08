use super::{Connector, HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, HttpTimeouts};
use crate::common::transport_failure;
use plenora_storage_core::{
    EngineConfig, ErrorCategory, ErrorPhase, ExecutionControl, OperationContext, RemoteEffect,
    RetryDisposition,
};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

/// Reads one HTTP request head from `socket`.
async fn read_request(socket: &mut tokio::net::TcpStream) {
    let mut head = Vec::new();
    let mut byte = [0_u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        let read = socket.read(&mut byte).await.expect("request");
        assert_eq!(read, 1, "connection closed before the request ended");
        head.push(byte[0]);
    }
}

/// A server that answers the first request on a kept-alive connection and
/// never answers the second. The first exchange happens in real time and
/// leaves an idle pooled connection, so the second request needs no new
/// connection and its only wait is for an answer that never comes: with
/// paused time that wait is deterministic.
async fn answers_once() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.expect("accept");
        read_request(&mut socket).await;
        socket
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            .await
            .expect("answer");
        read_request(&mut socket).await;
        std::future::pending::<()>().await;
    });
    (address, server)
}

/// Sends a request that is never answered through a connector built under
/// `control`, and returns the error with the (virtual) time it took.
async fn unanswered_request(control: ExecutionControl) -> (reqwest::Error, Duration) {
    let (address, server) = answers_once().await;
    let policy = EngineConfig {
        allow_insecure_http: true,
        allow_private_network: true,
        ..EngineConfig::default()
    };
    let url = url::Url::parse(&format!("http://{address}/")).expect("url");
    let connector = Connector::new(
        &url,
        &OperationContext {
            policy: &policy,
            control: &control,
        },
    )
    .await
    .expect("connector");
    let client = connector.client().expect("client");
    client.get(url.clone()).send().await.expect("first request");
    tokio::time::pause();
    let started = tokio::time::Instant::now();
    let error = client
        .get(url)
        .send()
        .await
        .expect_err("unanswered request");
    let elapsed = started.elapsed();
    server.abort();
    (error, elapsed)
}

fn deadline_in(seconds: u64) -> ExecutionControl {
    ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + Duration::from_secs(seconds))
}

/// With a ten-minute deadline a request may wait until the deadline. Before
/// 3.0.0 the client gave up after a fixed 60 s, whatever the deadline.
#[tokio::test]
async fn a_request_may_wait_until_the_deadline() {
    let (error, elapsed) = unanswered_request(deadline_in(600)).await;
    assert!(error.is_timeout());
    assert!(elapsed >= Duration::from_secs(590), "{elapsed:?}");
    assert!(elapsed <= Duration::from_secs(600), "{elapsed:?}");
}

/// Without a deadline the declared read limit applies, not a hidden 60 s.
#[tokio::test]
async fn without_deadline_the_declared_read_limit_applies() {
    let (error, elapsed) = unanswered_request(ExecutionControl::default()).await;
    assert!(error.is_timeout());
    assert!(elapsed >= HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, "{elapsed:?}");
    assert!(
        elapsed <= HTTP_READ_TIMEOUT_WITHOUT_DEADLINE + Duration::from_secs(1),
        "{elapsed:?}"
    );
}

/// A request the client gave up on is a `timeout`, with the effect of the
/// operation; before 3.0.0 it was `io`.
#[tokio::test]
async fn a_client_timeout_is_reported_as_timeout() {
    let (error, _) = unanswered_request(ExecutionControl::default()).await;
    let read = transport_failure(&error, ErrorPhase::Read, false);
    assert_eq!(
        (read.category, read.remote_effect, read.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
    let write = transport_failure(&error, ErrorPhase::Commit, true);
    assert_eq!(
        (write.category, write.remote_effect, write.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery
        )
    );
    let reset = transport_failure(
        &std::io::Error::from(std::io::ErrorKind::ConnectionReset),
        ErrorPhase::Read,
        false,
    );
    assert_eq!(reset.category, ErrorCategory::Io);
}

/// The connection timeout stays short and fixed unless less time remains.
#[test]
fn connection_timeout_never_exceeds_the_deadline() {
    assert_eq!(
        HttpTimeouts::for_remaining(None),
        HttpTimeouts {
            total: None,
            read: Some(HTTP_READ_TIMEOUT_WITHOUT_DEADLINE),
            connect: HTTP_CONNECT_TIMEOUT,
        }
    );
    assert_eq!(
        HttpTimeouts::for_remaining(Some(Duration::from_secs(600))),
        HttpTimeouts {
            total: Some(Duration::from_secs(600)),
            read: None,
            connect: HTTP_CONNECT_TIMEOUT,
        }
    );
    assert_eq!(
        HttpTimeouts::for_remaining(Some(Duration::from_secs(3))).connect,
        Duration::from_secs(3)
    );
}
