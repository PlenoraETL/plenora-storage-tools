use super::{optional_prefix_path, required_path};

#[test]
fn object_keys_are_relative_and_normalized() {
    assert!(required_path("folder/object.bin").is_ok());
    assert!(required_path("").is_err());
    assert!(required_path(&"x".repeat(4_097)).is_err());
    assert!(required_path("../secret").is_err());
    assert!(required_path("folder//object.bin").is_err());
    assert!(required_path("folder/./object.bin").is_err());
    assert!(required_path("folder/").is_err());
}

#[test]
fn an_empty_prefix_means_the_whole_namespace() {
    assert_eq!(optional_prefix_path(Some("")).expect("empty prefix"), None);
    assert!(
        optional_prefix_path(Some("incoming/"))
            .expect("prefix")
            .is_some()
    );
    assert!(optional_prefix_path(Some("/absolute")).is_err());
    assert!(optional_prefix_path(Some("../secret")).is_err());
}

/// Case 9e of the common runtime matrix: a proved publication whose metadata
/// cannot be read back is `committed` in `cleanup` and is never retried,
/// because repeating the request would publish again.
#[test]
fn unavailable_metadata_after_publication_is_committed_and_never_retried() {
    let error = super::committed_verification_error();
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::Committed
    );
    assert_eq!(error.phase, plenora_storage_core::ErrorPhase::Cleanup);
    assert_eq!(error.retry, plenora_storage_core::RetryDisposition::Never);
}

/// Reads one HTTP request head from `socket`.
async fn read_request(socket: &mut tokio::net::TcpStream) {
    use tokio::io::AsyncReadExt;
    let mut head = Vec::new();
    let mut byte = [0_u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        let read = socket.read(&mut byte).await.expect("request");
        assert_eq!(read, 1, "connection closed before the request ended");
        head.push(byte[0]);
    }
}

/// Sends a request that is never answered through the S3 client of an
/// operation whose time remaining is `remaining`, and returns the error with
/// the (virtual) time it took. The first request is answered in real time and
/// leaves an idle pooled connection, so the second one only waits for an
/// answer: with paused time that wait is deterministic.
async fn unanswered_request(
    remaining: Option<std::time::Duration>,
) -> (reqwest::Error, std::time::Duration) {
    use tokio::io::AsyncWriteExt;
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
    let connector = super::PinnedDnsConnector {
        pinned: Vec::new(),
        timeouts: super::ClientTimeouts::for_remaining(remaining),
    };
    let client = connector.client(true).expect("client");
    let url = format!("http://{address}/");
    client.get(&url).send().await.expect("first request");
    tokio::time::pause();
    let started = tokio::time::Instant::now();
    let error = client.get(&url).send().await.expect_err("unanswered");
    let elapsed = started.elapsed();
    server.abort();
    (error, elapsed)
}

/// With a ten-minute deadline an S3 request may wait until the deadline.
/// Before 3.0.0 the client gave up after a fixed 30 s, whatever the deadline.
#[tokio::test]
async fn an_s3_request_may_wait_until_the_deadline() {
    let (error, elapsed) = unanswered_request(Some(std::time::Duration::from_secs(600))).await;
    assert!(error.is_timeout());
    assert!(
        elapsed >= std::time::Duration::from_secs(599),
        "{elapsed:?}"
    );
    assert!(
        elapsed <= std::time::Duration::from_secs(601),
        "{elapsed:?}"
    );
}

/// Without a deadline the declared read limit applies, not a hidden 30 s.
#[tokio::test]
async fn without_deadline_the_declared_s3_read_limit_applies() {
    let (error, elapsed) = unanswered_request(None).await;
    assert!(error.is_timeout());
    assert!(
        elapsed >= super::READ_TIMEOUT_WITHOUT_DEADLINE,
        "{elapsed:?}"
    );
    assert!(
        elapsed <= super::READ_TIMEOUT_WITHOUT_DEADLINE + std::time::Duration::from_secs(1),
        "{elapsed:?}"
    );
}

/// A request the client gave up on is a `timeout` with the effect of the
/// operation, through the error wrapper of `object_store`; other failures keep their
/// mapping.
#[tokio::test]
async fn an_s3_client_timeout_is_reported_as_timeout() {
    use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};
    let (error, _) = unanswered_request(None).await;
    let mapped = super::map_store_error(
        object_store::Error::Generic {
            store: "S3",
            source: Box::new(error),
        },
        ErrorPhase::Commit,
        true,
    );
    assert_eq!(
        (mapped.category, mapped.remote_effect, mapped.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery
        )
    );
    let not_found = super::map_store_error(
        object_store::Error::NotFound {
            path: "object".to_owned(),
            source: "absent".into(),
        },
        ErrorPhase::Read,
        false,
    );
    assert_eq!(not_found.category, ErrorCategory::NotFound);
}

/// The connection timeout stays short and fixed unless less time remains.
#[test]
fn s3_connection_timeout_never_exceeds_the_deadline() {
    let short = super::ClientTimeouts::for_remaining(Some(std::time::Duration::from_secs(2)));
    assert_eq!(short.connect, std::time::Duration::from_secs(2));
    assert_eq!(short.total, Some(std::time::Duration::from_secs(2)));
    let open = super::ClientTimeouts::for_remaining(None);
    assert_eq!(open.total, None);
    assert_eq!(open.read, Some(super::READ_TIMEOUT_WITHOUT_DEADLINE));
    assert_eq!(open.connect, super::CLIENT_CONNECT_TIMEOUT);
}
