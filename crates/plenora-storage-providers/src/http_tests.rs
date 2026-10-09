use super::{Connector, HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, HttpTimeouts};
use crate::common::transport_failure;
use plenora_storage_core::{
    EngineConfig, ErrorCategory, ErrorPhase, ExecutionControl, OperationContext, RemoteEffect,
    RetryDisposition, StorageError,
};
use std::{
    collections::VecDeque,
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::Instant,
};

/// What the fake server does with one request, in arrival order.
#[derive(Clone, Copy, Debug)]
enum Reply {
    /// Answers at once with an empty body.
    Now,
    /// Answers with an empty body after this long.
    After(Duration),
    /// Never answers.
    Never,
    /// Answers at once with a body of `count` bytes, sending one every `every`.
    Trickle { every: Duration, count: usize },
}

/// An HTTP/1.1 server with keep-alive that answers requests, on whatever
/// connection they arrive, as its script says; past the end of the script it
/// never answers.
struct Server {
    address: SocketAddr,
    accepted: Arc<AtomicUsize>,
    /// Late answers and trickled bytes the client has received; the test
    /// counts them.
    received: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn server(script: Vec<Reply>) -> Server {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let accepted = Arc::new(AtomicUsize::new(0));
    let script = Arc::new(Mutex::new(VecDeque::from(script)));
    let received = Arc::new(AtomicUsize::new(0));
    let counter = accepted.clone();
    let delivered = received.clone();
    let task = tokio::spawn(async move {
        let mut connections = tokio::task::JoinSet::new();
        loop {
            let (socket, _) = listener.accept().await.expect("accept");
            counter.fetch_add(1, Ordering::SeqCst);
            connections.spawn(serve(socket, script.clone(), delivered.clone()));
        }
    });
    Server {
        address,
        accepted,
        received,
        task,
    }
}

async fn serve(
    mut socket: TcpStream,
    script: Arc<Mutex<VecDeque<Reply>>>,
    received: Arc<AtomicUsize>,
) {
    while read_request(&mut socket).await {
        let reply = script
            .lock()
            .expect("script")
            .pop_front()
            .unwrap_or(Reply::Never);
        match reply {
            Reply::Now => answer(&mut socket).await,
            Reply::After(delay) => {
                tokio::time::sleep(delay).await;
                let seen = received.load(Ordering::SeqCst);
                answer(&mut socket).await;
                // As for a trickled byte below: no virtual time passes until
                // the client has the answer.
                while received.load(Ordering::SeqCst) == seen {
                    tokio::task::yield_now().await;
                }
            }
            Reply::Never => std::future::pending::<()>().await,
            Reply::Trickle { every, count } => {
                let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {count}\r\n\r\n");
                socket.write_all(head.as_bytes()).await.expect("head");
                for sent in 1..=count {
                    tokio::time::sleep(every).await;
                    socket.write_all(b"x").await.expect("byte");
                    // Paused time jumps to the next timer as soon as no task
                    // can run, even if a byte is still on its way through the
                    // socket: the client's read timeout could then fire
                    // before the byte is seen. Staying runnable until the
                    // client has read it keeps the clock still meanwhile.
                    while received.load(Ordering::SeqCst) < sent {
                        tokio::task::yield_now().await;
                    }
                }
            }
        }
    }
}

async fn answer(socket: &mut TcpStream) {
    socket
        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        .await
        .expect("answer");
}

/// Reads one CRLF-terminated line; `None` when the peer closed first.
async fn read_line(socket: &mut TcpStream) -> Option<String> {
    let mut line = Vec::new();
    let mut byte = [0_u8; 1];
    while !line.ends_with(b"\r\n") {
        if socket.read(&mut byte).await.expect("read") == 0 {
            return None;
        }
        line.push(byte[0]);
    }
    line.truncate(line.len() - 2);
    Some(String::from_utf8(line).expect("ASCII line"))
}

/// Reads one request, body included (by length or chunked); `false` when the
/// client closed the connection.
async fn read_request(socket: &mut TcpStream) -> bool {
    let mut length = 0;
    let mut chunked = false;
    loop {
        let Some(line) = read_line(socket).await else {
            return false;
        };
        if line.is_empty() {
            break;
        }
        let line = line.to_ascii_lowercase();
        if let Some(value) = line.strip_prefix("content-length:") {
            length = value.trim().parse().expect("length");
        }
        chunked |= line == "transfer-encoding: chunked";
    }
    if chunked {
        loop {
            let size = read_line(socket).await.expect("chunk size");
            let size = usize::from_str_radix(&size, 16).expect("hex size");
            let mut chunk = vec![0; size + 2];
            socket.read_exact(&mut chunk).await.expect("chunk");
            if size == 0 {
                break;
            }
        }
    } else {
        let mut body = vec![0; length];
        socket.read_exact(&mut body).await.expect("body");
    }
    true
}

fn deadline_in(seconds: u64) -> ExecutionControl {
    ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + Duration::from_secs(seconds))
}

async fn connector(server: &Server, control: &ExecutionControl) -> (Connector, url::Url) {
    let policy = EngineConfig {
        allow_insecure_http: true,
        allow_private_network: true,
        ..EngineConfig::default()
    };
    let url = url::Url::parse(&format!("http://{}/", server.address)).expect("url");
    let connector = Connector::new(
        &url,
        &OperationContext {
            policy: &policy,
            control,
        },
    )
    .await
    .expect("connector");
    (connector, url)
}

/// Opens the client's pooled connection with one answered request in real
/// time, then pauses tokio time. Every later request reuses that connection
/// (each test checks that only one was accepted), so the waits under test
/// involve no connection setup and advance only in virtual time: no outcome
/// depends on how fast the machine is.
async fn warm_up(client: &reqwest::Client, url: &url::Url) {
    client.get(url.clone()).send().await.expect("warm-up");
    tokio::time::pause();
}

/// A GET through the operation control, as the providers issue it; counts
/// the answer as received by `server`.
async fn get(
    server: &Server,
    control: &ExecutionControl,
    client: &reqwest::Client,
    url: &url::Url,
) -> Result<bytes::Bytes, StorageError> {
    control
        .run(
            async {
                let response = client
                    .get(url.clone())
                    .send()
                    .await
                    .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
                server.received.fetch_add(1, Ordering::SeqCst);
                response
                    .bytes()
                    .await
                    .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await
}

fn assert_safe_timeout(error: StorageError) {
    assert_eq!(
        (error.category, error.remote_effect, error.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
}

/// With a ten-minute deadline a request may wait until the deadline. Before
/// 3.0.0 the client gave up after a fixed 60 s, whatever the deadline.
#[tokio::test]
async fn a_request_may_wait_until_the_deadline() {
    let control = deadline_in(600);
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (connector, url) = connector(&server, &control).await;
    let client = connector.client().expect("client");
    warm_up(&client, &url).await;
    let started = Instant::now();
    let error = get(&server, &control, &client, &url)
        .await
        .expect_err("timeout");
    let elapsed = started.elapsed();
    assert_safe_timeout(error);
    assert!(elapsed >= Duration::from_secs(590), "{elapsed:?}");
    assert!(elapsed <= Duration::from_secs(601), "{elapsed:?}");
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
}

/// A later request of the same operation ends at the deadline too, not one
/// full budget after it started: the connector's limits are computed once,
/// and the operation control is what keeps the deadline.
#[tokio::test]
async fn a_later_request_still_ends_at_the_deadline() {
    let control = deadline_in(600);
    let server = server(vec![
        Reply::Now,
        Reply::After(Duration::from_secs(200)),
        Reply::Never,
    ])
    .await;
    let (connector, url) = connector(&server, &control).await;
    let client = connector.client().expect("client");
    warm_up(&client, &url).await;
    let started = Instant::now();
    get(&server, &control, &client, &url)
        .await
        .expect("slow answer");
    assert!(started.elapsed() >= Duration::from_secs(200));
    let error = get(&server, &control, &client, &url)
        .await
        .expect_err("timeout");
    let elapsed = started.elapsed();
    assert_safe_timeout(error);
    assert!(elapsed >= Duration::from_secs(590), "{elapsed:?}");
    assert!(elapsed <= Duration::from_secs(601), "{elapsed:?}");
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
}

/// Without a deadline a silent response is given up on after the declared
/// read limit, not a hidden 60 s, and reported as `timeout`.
#[tokio::test]
async fn without_deadline_a_silent_response_ends_at_the_read_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (connector, url) = connector(&server, &control).await;
    let client = connector.client().expect("client");
    warm_up(&client, &url).await;
    let started = Instant::now();
    let error = get(&server, &control, &client, &url)
        .await
        .expect_err("timeout");
    let elapsed = started.elapsed();
    assert_safe_timeout(error);
    assert!(elapsed >= HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, "{elapsed:?}");
    assert!(
        elapsed <= HTTP_READ_TIMEOUT_WITHOUT_DEADLINE + Duration::from_secs(1),
        "{elapsed:?}"
    );
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
}

/// Without a deadline a download that keeps moving is never cut, however
/// long it lasts: the read limit counts silence, not the whole transfer.
#[tokio::test]
async fn without_deadline_a_download_that_keeps_moving_is_not_cut() {
    let control = ExecutionControl::default();
    let server = server(vec![
        Reply::Now,
        Reply::Trickle {
            every: Duration::from_secs(100),
            count: 6,
        },
    ])
    .await;
    let (connector, url) = connector(&server, &control).await;
    let client = connector.client().expect("client");
    warm_up(&client, &url).await;
    let started = Instant::now();
    let length = control
        .run(
            async {
                let response = client
                    .get(url.clone())
                    .send()
                    .await
                    .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
                let mut body = response.bytes_stream();
                while let Some(chunk) = futures_util::StreamExt::next(&mut body).await {
                    let chunk = chunk
                        .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
                    server.received.fetch_add(chunk.len(), Ordering::SeqCst);
                }
                Ok(server.received.load(Ordering::SeqCst))
            },
            ErrorPhase::Read,
            false,
        )
        .await
        .expect("download");
    assert_eq!(length, 6);
    assert!(started.elapsed() >= Duration::from_secs(600));
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
}

/// Uploads a body that sends one byte every 100 s for 400 s, through the
/// operation control without a deadline, and returns the outcome with the
/// (virtual) time it took.
async fn slow_upload(
    client: &reqwest::Client,
    url: &url::Url,
) -> (Result<(), StorageError>, Duration) {
    let control = ExecutionControl::default();
    let body = futures_util::stream::unfold(0, |sent| async move {
        if sent == 4 {
            return None;
        }
        tokio::time::sleep(Duration::from_secs(100)).await;
        Some((
            Ok::<_, std::io::Error>(bytes::Bytes::from_static(b"x")),
            sent + 1,
        ))
    });
    let started = Instant::now();
    let result = control
        .run(
            async {
                client
                    .put(url.clone())
                    .body(reqwest::Body::wrap_stream(body))
                    .send()
                    .await
                    .map(drop)
                    .map_err(|error| transport_failure(&error, ErrorPhase::Commit, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await;
    (result, started.elapsed())
}

/// Without a deadline an upload that keeps sending is not cut by the read
/// limit: requests with a body go through the upload client, which has no
/// client limit and leaves only the caller's deadline.
#[tokio::test]
async fn without_deadline_an_upload_is_not_cut_by_the_read_limit() {
    let server = server(vec![Reply::Now, Reply::Now]).await;
    let (connector, url) = connector(&server, &ExecutionControl::default()).await;
    let client = connector.upload_client().expect("client");
    warm_up(&client, &url).await;
    let (result, elapsed) = slow_upload(&client, &url).await;
    result.expect("upload");
    assert!(elapsed >= Duration::from_secs(400), "{elapsed:?}");
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
}

/// Why uploads need their own client: reqwest starts the read timeout with
/// the request and bytes sent do not re-arm it, so the read client cuts the
/// same upload at the read limit while it is still moving.
#[tokio::test]
async fn the_read_client_would_cut_a_moving_upload() {
    let server = server(vec![Reply::Now, Reply::Now]).await;
    let (connector, url) = connector(&server, &ExecutionControl::default()).await;
    let client = connector.client().expect("client");
    warm_up(&client, &url).await;
    let (result, elapsed) = slow_upload(&client, &url).await;
    let error = result.expect_err("cut");
    assert_eq!(
        (error.category, error.remote_effect, error.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery
        )
    );
    assert!(elapsed >= HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(400), "{elapsed:?}");
}

/// Sends one request through the client `object_store` gets for Azure, twice:
/// once in real time to open the pooled connection, then with paused time to
/// a server that answers after 400 s. Returns the outcome of the second.
#[cfg(feature = "azure")]
async fn azure_request_answered_after_400_s(
    method: reqwest::Method,
    body: fn() -> object_store::client::HttpRequestBody,
) -> Result<Duration, object_store::client::HttpError> {
    use object_store::client::{HttpConnector, HttpRequest};
    let server = server(vec![Reply::Now, Reply::After(Duration::from_secs(400))]).await;
    let (connector, url) = connector(&server, &ExecutionControl::default()).await;
    let client = connector
        .connect(&object_store::ClientOptions::new())
        .expect("client");
    let request = || {
        let mut request = HttpRequest::new(body());
        *request.method_mut() = method.clone();
        *request.uri_mut() = url.as_str().parse().expect("uri");
        request
    };
    client.execute(request()).await.expect("warm-up");
    tokio::time::pause();
    let started = Instant::now();
    let result = client.execute(request()).await.map(|_| started.elapsed());
    server.received.fetch_add(1, Ordering::SeqCst);
    assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
    result
}

/// Azure requests go through `object_store`, which sees a single client: a
/// request with a body is routed to the upload client, so waiting past the
/// read limit for the answer to an upload does not cut it.
#[cfg(feature = "azure")]
#[tokio::test]
async fn azure_requests_with_a_body_use_the_upload_client() {
    let elapsed = azure_request_answered_after_400_s(reqwest::Method::PUT, || {
        // The body every upload carries.
        object_store::PutPayload::from_static(b"x").into()
    })
    .await
    .expect("upload");
    assert!(elapsed >= Duration::from_secs(400), "{elapsed:?}");
}

/// The same wait on an Azure request without a body ends at the read limit.
#[cfg(feature = "azure")]
#[tokio::test]
async fn azure_requests_without_a_body_keep_the_read_limit() {
    let error = azure_request_answered_after_400_s(reqwest::Method::GET, || {
        object_store::client::HttpRequestBody::empty()
    })
    .await
    .expect_err("read limit");
    assert_eq!(error.kind(), object_store::client::HttpErrorKind::Timeout);
}

/// A transport error is `timeout` only when the client gave up waiting, with
/// the effect of the operation; before 3.0.0 it was `io`.
#[test]
fn only_a_client_timeout_is_reported_as_timeout() {
    let timed_out = std::io::Error::from(std::io::ErrorKind::TimedOut);
    let write = transport_failure(&timed_out, ErrorPhase::Commit, true);
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
