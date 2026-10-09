use super::{Connector, HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_WITHOUT_DEADLINE, HttpTimeouts};
use crate::common::transport_failure;
use plenora_storage_core::{
    EngineConfig, ErrorCategory, ErrorPhase, ExecutionControl, InactivityTimeout, OperationContext,
    RemoteEffect, RetryDisposition, StorageError,
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

const LIMIT: Duration = HTTP_READ_TIMEOUT_WITHOUT_DEADLINE;

/// Yields until `ready` holds. A condition that never comes true fails the
/// test after 10 s of real time instead of hanging it.
async fn yield_until(mut ready: impl FnMut() -> bool, what: &str) {
    let started = std::time::Instant::now();
    while !ready() {
        assert!(started.elapsed() < Duration::from_secs(10), "{what}");
        tokio::task::yield_now().await;
    }
}

/// What the fake server does with one request, in arrival order.
#[derive(Clone, Copy, Debug)]
enum Reply {
    /// Reads the request and answers at once with an empty body.
    Now,
    /// Reads the request and answers with an empty body after this long.
    After(Duration),
    /// Reads the request and never answers.
    Never,
    /// Never reads the request body and never answers.
    NeverRead,
    /// Answers at once with a body of `count` bytes, sending one every `every`.
    Trickle { every: Duration, count: usize },
}

/// An HTTP/1.1 server with keep-alive that answers requests, on whatever
/// connection they arrive, as its script says; past the end of the script it
/// never answers.
struct Server {
    address: SocketAddr,
    /// Request heads received, on any connection.
    requests: Arc<AtomicUsize>,
    /// Answers whose head the client has received; the tests count them.
    answers: Arc<AtomicUsize>,
    /// Trickled body bytes the client has received; the tests count them.
    bytes: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

#[derive(Clone)]
struct Shared {
    script: Arc<Mutex<VecDeque<Reply>>>,
    requests: Arc<AtomicUsize>,
    answers: Arc<AtomicUsize>,
    bytes: Arc<AtomicUsize>,
}

async fn server(script: Vec<Reply>) -> Server {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let shared = Shared {
        script: Arc::new(Mutex::new(VecDeque::from(script))),
        requests: Arc::new(AtomicUsize::new(0)),
        answers: Arc::new(AtomicUsize::new(0)),
        bytes: Arc::new(AtomicUsize::new(0)),
    };
    let (requests, answers, bytes) = (
        shared.requests.clone(),
        shared.answers.clone(),
        shared.bytes.clone(),
    );
    let task = tokio::spawn(async move {
        let mut connections = tokio::task::JoinSet::new();
        loop {
            let (socket, _) = listener.accept().await.expect("accept");
            connections.spawn(serve(socket, shared.clone()));
        }
    });
    Server {
        address,
        requests,
        answers,
        bytes,
        task,
    }
}

async fn serve(mut socket: TcpStream, shared: Shared) {
    while let Some(body) = read_head(&mut socket).await {
        shared.requests.fetch_add(1, Ordering::SeqCst);
        let reply = shared
            .script
            .lock()
            .expect("script")
            .pop_front()
            .unwrap_or(Reply::Never);
        if matches!(reply, Reply::NeverRead) {
            std::future::pending::<()>().await;
        }
        read_body(&mut socket, body).await;
        match reply {
            Reply::Now => answer(&mut socket, &shared.answers).await,
            Reply::After(delay) => {
                tokio::time::sleep(delay).await;
                answer(&mut socket, &shared.answers).await;
            }
            Reply::Never | Reply::NeverRead => std::future::pending::<()>().await,
            Reply::Trickle { every, count } => {
                let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {count}\r\n\r\n");
                socket.write_all(head.as_bytes()).await.expect("head");
                let base = shared.bytes.load(Ordering::SeqCst);
                for sent in 1..=count {
                    tokio::time::sleep(every).await;
                    socket.write_all(b"x").await.expect("byte");
                    // Paused time jumps to the next timer as soon as no task
                    // can run, even with the byte still in the socket. Staying
                    // runnable until the client has it keeps the clock still.
                    yield_until(
                        || shared.bytes.load(Ordering::SeqCst) >= base + sent,
                        "the client never read a trickled byte",
                    )
                    .await;
                }
            }
        }
    }
}

/// Answers with an empty body and, like a trickled byte, keeps the clock
/// still until the client has the answer.
async fn answer(socket: &mut TcpStream, answers: &AtomicUsize) {
    let seen = answers.load(Ordering::SeqCst);
    socket
        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        .await
        .expect("answer");
    yield_until(
        || answers.load(Ordering::SeqCst) > seen,
        "the client never read an answer",
    )
    .await;
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

/// How a request body is delimited.
enum Framing {
    Length(usize),
    Chunked,
}

/// Reads one request head; `None` when the client closed the connection.
async fn read_head(socket: &mut TcpStream) -> Option<Framing> {
    let mut framing = Framing::Length(0);
    loop {
        let line = read_line(socket).await?;
        if line.is_empty() {
            return Some(framing);
        }
        let line = line.to_ascii_lowercase();
        if let Some(value) = line.strip_prefix("content-length:") {
            framing = Framing::Length(value.trim().parse().expect("length"));
        }
        if line == "transfer-encoding: chunked" {
            framing = Framing::Chunked;
        }
    }
}

async fn read_body(socket: &mut TcpStream, framing: Framing) {
    match framing {
        Framing::Length(length) => {
            let mut body = vec![0; length];
            socket.read_exact(&mut body).await.expect("body");
        }
        Framing::Chunked => loop {
            let size = read_line(socket).await.expect("chunk size");
            let size = usize::from_str_radix(&size, 16).expect("hex size");
            let mut chunk = vec![0; size + 2];
            socket.read_exact(&mut chunk).await.expect("chunk");
            if size == 0 {
                break;
            }
        },
    }
}

fn deadline_in(seconds: u64) -> ExecutionControl {
    ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + Duration::from_secs(seconds))
}

/// The client of an operation under `control`, its pooled connection opened
/// by one answered request in real time, after which tokio time is paused.
async fn warm_client(
    server: &Server,
    control: &ExecutionControl,
) -> (reqwest::Client, Option<Duration>, url::Url) {
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
    let client = connector.client().expect("client");
    let warm = client.get(url.clone()).send().await.expect("warm-up");
    server.answers.fetch_add(1, Ordering::SeqCst);
    drop(warm);
    tokio::time::pause();
    (client, connector.idle(), url)
}

/// Sends `request` as the providers do, through the operation control and
/// the inactivity limit, and reads the whole answer. Virtual time stands
/// still until the request has reached the server, so setting up a
/// connection, if the pool needs one, never races a client timer.
async fn exchange(
    server: &Server,
    control: &ExecutionControl,
    idle: Option<Duration>,
    request: reqwest::RequestBuilder,
    mutating: bool,
) -> (Result<usize, StorageError>, Duration) {
    let phase = if mutating {
        ErrorPhase::Commit
    } else {
        ErrorPhase::Read
    };
    let target = server.requests.load(Ordering::SeqCst) + 1;
    let requests = server.requests.clone();
    let hold = tokio::spawn(async move {
        yield_until(
            || requests.load(Ordering::SeqCst) >= target,
            "the request never reached the server",
        )
        .await;
    });
    let started = Instant::now();
    let result = control
        .run(
            async {
                let response = crate::watched::send(request, idle)
                    .await
                    .map_err(|error| transport_failure(&*error, phase, mutating))?;
                server.answers.fetch_add(1, Ordering::SeqCst);
                let mut response = response;
                let mut length = 0;
                while let Some(chunk) = response
                    .chunk()
                    .await
                    .map_err(|error| transport_failure(&error, phase, mutating))?
                {
                    length += chunk.len();
                    server.bytes.fetch_add(chunk.len(), Ordering::SeqCst);
                }
                Ok(length)
            },
            phase,
            mutating,
        )
        .await;
    let elapsed = started.elapsed();
    hold.await.expect("request reached the server");
    (result, elapsed)
}

/// A body that sends one byte every `every`, `count` times; then, if `stall`,
/// produces nothing more without ending.
fn slow_body(every: Duration, count: usize, stall: bool) -> reqwest::Body {
    reqwest::Body::wrap_stream(futures_util::stream::unfold(0, move |sent| async move {
        if sent == count {
            if stall {
                std::future::pending::<()>().await;
            }
            return None;
        }
        tokio::time::sleep(every).await;
        Some((
            Ok::<_, std::io::Error>(bytes::Bytes::from_static(b"x")),
            sent + 1,
        ))
    }))
}

fn assert_axes(error: StorageError, mutating: bool) {
    let expected = if mutating {
        (RemoteEffect::Unknown, RetryDisposition::RequiresRecovery)
    } else {
        (RemoteEffect::None, RetryDisposition::Safe)
    };
    assert_eq!(
        (error.category, (error.remote_effect, error.retry)),
        (ErrorCategory::Timeout, expected)
    );
}

fn assert_near(elapsed: Duration, expected: Duration) {
    assert!(elapsed >= expected, "{elapsed:?} < {expected:?}");
    assert!(
        elapsed <= expected + Duration::from_secs(1),
        "{elapsed:?} > {expected:?}"
    );
}

/// With a ten-minute deadline a request may wait until the deadline. Before
/// 3.0.0 the client gave up after a fixed 60 s, whatever the deadline.
#[tokio::test]
async fn a_request_may_wait_until_the_deadline() {
    let control = deadline_in(600);
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let (result, elapsed) = exchange(&server, &control, idle, client.get(url), false).await;
    assert_axes(result.expect_err("timeout"), false);
    assert!(elapsed >= Duration::from_secs(590), "{elapsed:?}");
    assert!(elapsed <= Duration::from_secs(601), "{elapsed:?}");
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
    let (client, idle, url) = warm_client(&server, &control).await;
    let started = Instant::now();
    let (result, _) = exchange(&server, &control, idle, client.get(url.clone()), false).await;
    result.expect("slow answer");
    assert!(started.elapsed() >= Duration::from_secs(200));
    let (result, _) = exchange(&server, &control, idle, client.get(url), false).await;
    assert_axes(result.expect_err("timeout"), false);
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_secs(590), "{elapsed:?}");
    assert!(elapsed <= Duration::from_secs(601), "{elapsed:?}");
}

/// Without a deadline a silent response is given up on after the declared
/// limit, not a hidden 60 s, and reported as `timeout`.
#[tokio::test]
async fn without_deadline_a_silent_response_ends_at_the_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let (result, elapsed) = exchange(&server, &control, idle, client.get(url), false).await;
    assert_axes(result.expect_err("timeout"), false);
    assert_near(elapsed, LIMIT);
}

/// Without a deadline a download that keeps moving is never cut, however
/// long it lasts: the limit counts inactivity, not the whole transfer.
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
    let (client, idle, url) = warm_client(&server, &control).await;
    let (result, elapsed) = exchange(&server, &control, idle, client.get(url), false).await;
    assert_eq!(result.expect("download"), 6);
    assert!(elapsed >= Duration::from_secs(600), "{elapsed:?}");
}

/// Without a deadline an upload that keeps sending for ten minutes, twice
/// the limit, is not cut: each frame the transport takes re-arms the clock.
/// reqwest's own read timeout would have cut it at 300 s.
#[tokio::test]
async fn without_deadline_an_upload_that_keeps_moving_is_not_cut() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Now]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let request = client
        .put(url)
        .body(slow_body(Duration::from_secs(100), 6, false));
    let (result, elapsed) = exchange(&server, &control, idle, request, true).await;
    result.expect("upload");
    assert!(elapsed >= Duration::from_secs(600), "{elapsed:?}");
}

/// An upload whose body stops being taken fails at the limit after the last
/// frame taken, while it is still being sent.
#[tokio::test]
async fn an_upload_that_stops_moving_ends_at_the_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let request = client
        .put(url)
        .body(slow_body(Duration::from_secs(100), 2, true));
    let (result, elapsed) = exchange(&server, &control, idle, request, true).await;
    assert_axes(result.expect_err("timeout"), true);
    assert_near(elapsed, Duration::from_secs(200) + LIMIT);
}

/// A server that stops reading: once the socket buffers are full the
/// transport takes no more frames, and the upload fails at the limit. A frame
/// counts once the transport has taken it, not once the server has it.
#[tokio::test]
async fn an_upload_the_server_stops_reading_ends_at_the_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::NeverRead]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let chunk = bytes::Bytes::from(vec![0_u8; 64 * 1024]);
    let body = futures_util::stream::iter(
        std::iter::repeat_n(chunk, 4 * 1024).map(Ok::<_, std::io::Error>),
    );
    let request = client.put(url).body(reqwest::Body::wrap_stream(body));
    let (result, elapsed) = exchange(&server, &control, idle, request, true).await;
    assert_axes(result.expect_err("timeout"), true);
    assert!(elapsed >= LIMIT, "{elapsed:?}");
    assert!(elapsed <= LIMIT + Duration::from_secs(1), "{elapsed:?}");
}

/// An answer that never comes after the whole body was sent fails at the
/// limit after the last frame: the clock keeps running once the body ends.
#[tokio::test]
async fn an_answer_that_never_comes_after_the_body_ends_at_the_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let request = client
        .put(url)
        .body(slow_body(Duration::from_secs(100), 2, false));
    let (result, elapsed) = exchange(&server, &control, idle, request, true).await;
    assert_axes(result.expect_err("timeout"), true);
    assert_near(elapsed, Duration::from_secs(200) + LIMIT);
}

/// A mutation without a body (here DELETE, as for MKCOL) follows the same
/// rule as an upload: the error comes from the inactivity clock.
#[tokio::test]
async fn a_mutation_without_a_body_follows_the_same_rule() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let started = Instant::now();
    let error = crate::watched::send(client.delete(url), idle)
        .await
        .expect_err("timeout");
    assert_near(started.elapsed(), LIMIT);
    assert!(inactivity_in_chain(&*error), "{error:?}");
    assert_axes(transport_failure(&*error, ErrorPhase::Commit, true), true);
}

/// Whether the inactivity clock is what failed `error`.
fn inactivity_in_chain(error: &(dyn std::error::Error + 'static)) -> bool {
    let mut cause = Some(error);
    while let Some(current) = cause {
        // `io::Error` hides the error it wraps from `source`.
        if let Some(io) = current.downcast_ref::<std::io::Error>()
            && let Some(inner) = io.get_ref()
            && inner.is::<InactivityTimeout>()
        {
            return true;
        }
        if current.is::<InactivityTimeout>() {
            return true;
        }
        cause = current.source();
    }
    false
}

/// Sends one request through the client `object_store` gets for Azure, after
/// a first one in real time opened the pooled connection, and returns the
/// outcome with the (virtual) time it took.
#[cfg(feature = "azure")]
async fn azure_request(
    script: Vec<Reply>,
    body: object_store::client::HttpRequestBody,
) -> (
    Result<object_store::client::HttpResponse, object_store::client::HttpError>,
    Duration,
) {
    use object_store::client::{HttpConnector, HttpRequest};
    let server = server(script).await;
    let control = ExecutionControl::default();
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
            control: &control,
        },
    )
    .await
    .expect("connector");
    let client = connector
        .connect(&object_store::ClientOptions::new())
        .expect("client");
    let request = |body| {
        let mut request = HttpRequest::new(body);
        *request.method_mut() = reqwest::Method::PUT;
        *request.uri_mut() = url.as_str().parse().expect("uri");
        request
    };
    client
        .execute(request(object_store::client::HttpRequestBody::empty()))
        .await
        .expect("warm-up");
    server.answers.fetch_add(1, Ordering::SeqCst);
    tokio::time::pause();
    let requests = server.requests.clone();
    let hold = tokio::spawn(async move {
        yield_until(
            || requests.load(Ordering::SeqCst) >= 2,
            "the request never reached the server",
        )
        .await;
    });
    let started = Instant::now();
    let result = client.execute(request(body)).await;
    if result.is_ok() {
        server.answers.fetch_add(1, Ordering::SeqCst);
    }
    hold.await.expect("request reached the server");
    (result, started.elapsed())
}

/// Azure requests go through `object_store`, which sees a single client:
/// every request it sends, with or without a body, is under the inactivity
/// limit. An upload answered within the limit after its body succeeds.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_azure_upload_answered_within_the_limit_succeeds() {
    let (result, elapsed) = azure_request(
        vec![Reply::Now, Reply::After(Duration::from_secs(200))],
        object_store::PutPayload::from_static(b"x").into(),
    )
    .await;
    result.expect("upload");
    assert!(elapsed >= Duration::from_secs(200), "{elapsed:?}");
}

/// Checks that an unanswered Azure request with `body` fails at the limit as
/// an `object_store` timeout from the inactivity clock.
#[cfg(feature = "azure")]
async fn unanswered_azure_request(body: object_store::client::HttpRequestBody) {
    let (result, elapsed) = azure_request(vec![Reply::Now, Reply::Never], body).await;
    let error = result.expect_err("timeout");
    assert_eq!(error.kind(), object_store::client::HttpErrorKind::Timeout);
    assert!(inactivity_in_chain(&error), "{error:?}");
    assert_near(elapsed, LIMIT);
}

/// An Azure upload the server never answers fails at the limit.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_unanswered_azure_upload_ends_at_the_limit() {
    unanswered_azure_request(object_store::PutPayload::from_static(b"x").into()).await;
}

/// An Azure request without a body, read or mutation, follows the same rule.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_unanswered_azure_request_without_a_body_ends_at_the_limit() {
    unanswered_azure_request(object_store::client::HttpRequestBody::empty()).await;
}

/// A transport error is `timeout` only when the client gave up waiting, with
/// the effect of the operation; before 3.0.0 it was `io`.
#[test]
fn only_a_client_timeout_is_reported_as_timeout() {
    let timed_out = std::io::Error::from(std::io::ErrorKind::TimedOut);
    assert_axes(
        transport_failure(&timed_out, ErrorPhase::Commit, true),
        true,
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
            idle: Some(LIMIT),
            connect: HTTP_CONNECT_TIMEOUT,
        }
    );
    assert_eq!(
        HttpTimeouts::for_remaining(Some(Duration::from_secs(600))),
        HttpTimeouts {
            total: Some(Duration::from_secs(600)),
            idle: None,
            connect: HTTP_CONNECT_TIMEOUT,
        }
    );
    assert_eq!(
        HttpTimeouts::for_remaining(Some(Duration::from_secs(3))).connect,
        Duration::from_secs(3)
    );
}
