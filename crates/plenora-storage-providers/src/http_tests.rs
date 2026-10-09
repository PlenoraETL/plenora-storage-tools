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
    /// Reads the request and answers with exactly this head and body; the
    /// client acknowledges it once it has read the whole body.
    Fixed {
        head: &'static str,
        body: &'static [u8],
    },
}

/// One request as the server received it.
#[derive(Clone, Debug)]
struct Recorded {
    method: String,
    /// Header names in lower case, with their values.
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Recorded {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(header, _)| header == name)
            .map(|(_, value)| value.as_str())
    }
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
    /// Every request received, in order.
    log: Arc<Mutex<Vec<Recorded>>>,
    task: tokio::task::JoinHandle<()>,
}

impl Server {
    /// The last request received.
    fn last(&self) -> Recorded {
        self.log
            .lock()
            .expect("log")
            .last()
            .cloned()
            .expect("a request")
    }
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
    log: Arc<Mutex<Vec<Recorded>>>,
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
        log: Arc::new(Mutex::new(Vec::new())),
    };
    let (requests, answers, bytes, log) = (
        shared.requests.clone(),
        shared.answers.clone(),
        shared.bytes.clone(),
        shared.log.clone(),
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
        log,
        task,
    }
}

async fn serve(mut socket: TcpStream, shared: Shared) {
    while let Some((method, headers, framing)) = read_head(&mut socket).await {
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
        let body = read_body(&mut socket, framing).await;
        shared.log.lock().expect("log").push(Recorded {
            method,
            headers,
            body,
        });
        match reply {
            Reply::Now => answer(&mut socket, &shared.answers).await,
            Reply::Fixed { head, body } => {
                let seen = shared.answers.load(Ordering::SeqCst);
                socket.write_all(head.as_bytes()).await.expect("head");
                socket.write_all(body).await.expect("body");
                yield_until(
                    || shared.answers.load(Ordering::SeqCst) > seen,
                    "the client never read an answer",
                )
                .await;
            }
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

/// A request head: method, headers (names in lower case) and framing.
type Head = (String, Vec<(String, String)>, Framing);

/// Reads one request head; `None` when the client closed the connection.
async fn read_head(socket: &mut TcpStream) -> Option<Head> {
    let request_line = read_line(socket).await?;
    let method = request_line.split(' ').next().expect("method").to_owned();
    let mut headers = Vec::new();
    let mut framing = Framing::Length(0);
    loop {
        let line = read_line(socket).await?;
        if line.is_empty() {
            return Some((method, headers, framing));
        }
        let (name, value) = line.split_once(':').expect("header");
        let (name, value) = (name.to_ascii_lowercase(), value.trim().to_owned());
        if name == "content-length" {
            framing = Framing::Length(value.parse().expect("length"));
        }
        if name == "transfer-encoding" && value.eq_ignore_ascii_case("chunked") {
            framing = Framing::Chunked;
        }
        headers.push((name, value));
    }
}

async fn read_body(socket: &mut TcpStream, framing: Framing) -> Vec<u8> {
    match framing {
        Framing::Length(length) => {
            let mut body = vec![0; length];
            socket.read_exact(&mut body).await.expect("body");
            body
        }
        Framing::Chunked => {
            let mut body = Vec::new();
            loop {
                let size = read_line(socket).await.expect("chunk size");
                let size = usize::from_str_radix(&size, 16).expect("hex size");
                let mut chunk = vec![0; size + 2];
                socket.read_exact(&mut chunk).await.expect("chunk");
                if size == 0 {
                    return body;
                }
                body.extend_from_slice(&chunk[..size]);
            }
        }
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

/// Empty frames are not progress: an upload whose body sends only empty
/// frames, every 10 s, ends at the limit.
#[tokio::test]
async fn an_upload_of_empty_frames_ends_at_the_limit() {
    let control = ExecutionControl::default();
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (client, idle, url) = warm_client(&server, &control).await;
    let body = futures_util::stream::unfold((), |()| async {
        tokio::time::sleep(Duration::from_secs(10)).await;
        Some((Ok::<_, std::io::Error>(bytes::Bytes::new()), ()))
    });
    let request = client.put(url).body(reqwest::Body::wrap_stream(body));
    let (result, elapsed) = exchange(&server, &control, idle, request, true).await;
    assert_axes(result.expect_err("timeout"), true);
    assert_near(elapsed, LIMIT);
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
    let mut cause = None;
    let result = control
        .run(
            async {
                crate::watched::send(client.delete(url), idle)
                    .await
                    .map(drop)
                    .map_err(|error| {
                        let mapped = transport_failure(&*error, ErrorPhase::Commit, true);
                        cause = Some(error);
                        mapped
                    })
            },
            ErrorPhase::Commit,
            true,
        )
        .await;
    assert_near(started.elapsed(), LIMIT);
    assert_axes(result.expect_err("timeout"), true);
    let cause = cause.expect("a transport failure");
    assert!(inactivity_in_chain(&*cause), "{cause:?}");
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

/// The answer to one request: status, headers and the whole body.
type Answer = (
    reqwest::StatusCode,
    reqwest::header::HeaderMap,
    bytes::Bytes,
);

/// Sends one request through the client `object_store` gets for Azure, after
/// a first one in real time opened the pooled connection, through the
/// operation control, and returns the answer or the `object_store` error with
/// the (virtual) time it took.
#[cfg(feature = "azure")]
async fn azure_request(
    server: &Server,
    method: reqwest::Method,
    headers: &[(&'static str, &'static str)],
    body: object_store::client::HttpRequestBody,
) -> (Result<Answer, object_store::client::HttpError>, Duration) {
    use object_store::client::{HttpConnector, HttpRequest};
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
    let mut warm = HttpRequest::new(object_store::client::HttpRequestBody::empty());
    *warm.method_mut() = reqwest::Method::PUT;
    *warm.uri_mut() = url.as_str().parse().expect("uri");
    client.execute(warm).await.expect("warm-up");
    server.answers.fetch_add(1, Ordering::SeqCst);
    tokio::time::pause();
    let mut request = HttpRequest::new(body);
    *request.method_mut() = method;
    *request.uri_mut() = url.as_str().parse().expect("uri");
    for (name, value) in headers {
        request.headers_mut().insert(
            reqwest::header::HeaderName::from_static(name),
            reqwest::header::HeaderValue::from_static(value),
        );
    }
    let requests = server.requests.clone();
    let hold = tokio::spawn(async move {
        yield_until(
            || requests.load(Ordering::SeqCst) >= 2,
            "the request never reached the server",
        )
        .await;
    });
    let started = Instant::now();
    let mut failure = None;
    let result = control
        .run(
            async {
                let outcome = async {
                    let response = client.execute(request).await?;
                    let (parts, body) = response.into_parts();
                    let body = body.bytes().await?;
                    // Acknowledged once the whole answer is in: the server
                    // keeps the clock still until then.
                    server.answers.fetch_add(1, Ordering::SeqCst);
                    Ok::<_, object_store::client::HttpError>((parts.status, parts.headers, body))
                }
                .await;
                outcome.map_err(|error| {
                    let mapped = transport_failure(&error, ErrorPhase::Commit, true);
                    failure = Some(error);
                    mapped
                })
            },
            ErrorPhase::Commit,
            true,
        )
        .await;
    let elapsed = started.elapsed();
    hold.await.expect("request reached the server");
    match (result, failure) {
        (Ok(answer), _) => (Ok(answer), elapsed),
        (Err(_), Some(error)) => (Err(error), elapsed),
        (Err(error), None) => panic!("the operation control failed first: {error:?}"),
    }
}

/// Azure requests go through `object_store`, which sees a single client:
/// every request it sends, with or without a body, is under the inactivity
/// limit. An upload answered within the limit after its body succeeds.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_azure_upload_answered_within_the_limit_succeeds() {
    let server = server(vec![Reply::Now, Reply::After(Duration::from_secs(200))]).await;
    let (result, elapsed) = azure_request(
        &server,
        reqwest::Method::PUT,
        &[],
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
    let server = server(vec![Reply::Now, Reply::Never]).await;
    let (result, elapsed) = azure_request(&server, reqwest::Method::PUT, &[], body).await;
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

/// Sends `request` through `watched::send` and the operation control, as
/// `WebDAV`, GCS and Azure file uploads do, and returns the whole answer.
async fn send_and_read(server: &Server, request: reqwest::RequestBuilder) -> Answer {
    let control = ExecutionControl::default();
    let target = server.requests.load(Ordering::SeqCst) + 1;
    let requests = server.requests.clone();
    let hold = tokio::spawn(async move {
        yield_until(
            || requests.load(Ordering::SeqCst) >= target,
            "the request never reached the server",
        )
        .await;
    });
    let answer = control
        .run(
            async {
                let response = crate::watched::send(request, Some(LIMIT))
                    .await
                    .map_err(|error| transport_failure(&*error, ErrorPhase::Read, false))?;
                let status = response.status();
                let headers = response.headers().clone();
                let body = response
                    .bytes()
                    .await
                    .map_err(|error| transport_failure(&error, ErrorPhase::Read, false))?;
                // Acknowledged once the whole answer is in: the server keeps
                // the clock still until then.
                server.answers.fetch_add(1, Ordering::SeqCst);
                Ok((status, headers, body))
            },
            ErrorPhase::Read,
            false,
        )
        .await
        .expect("answer");
    hold.await.expect("request reached the server");
    answer
}

const PROPFIND_BODY: &str = "<?xml version=\"1.0\"?><d:propfind xmlns:d=\"DAV:\"/>";
const MULTI_STATUS: Reply = Reply::Fixed {
    head: "HTTP/1.1 207 Multi-Status\r\nContent-Type: application/xml\r\nContent-Length: 17\r\n\r\n",
    body: b"<d:multistatus/>\n",
};
const HEAD_ANSWER: Reply = Reply::Fixed {
    head: "HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n",
    body: b"",
};
const PARTIAL: Reply = Reply::Fixed {
    head: "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 2-5/10\r\nContent-Length: 4\r\n\r\n",
    body: b"2345",
};

fn assert_propfind(server: &Server, answer: &Answer) {
    let request = server.last();
    assert_eq!(request.method, "PROPFIND");
    assert_eq!(request.header("depth"), Some("1"));
    assert_eq!(request.header("content-type"), Some("application/xml"));
    assert_eq!(request.body, PROPFIND_BODY.as_bytes());
    assert_eq!(answer.0.as_u16(), 207);
    assert_eq!(&answer.2[..], b"<d:multistatus/>\n");
}

fn assert_head(server: &Server, answer: &Answer) {
    assert_eq!(server.last().method, "HEAD");
    assert_eq!(answer.0.as_u16(), 200);
    assert_eq!(
        answer
            .1
            .get(reqwest::header::CONTENT_LENGTH)
            .map(reqwest::header::HeaderValue::as_bytes),
        Some(&b"100"[..])
    );
    assert!(answer.2.is_empty());
}

fn assert_range(server: &Server, answer: &Answer) {
    let request = server.last();
    assert_eq!(request.method, "GET");
    assert_eq!(request.header("range"), Some("bytes=2-5"));
    assert_eq!(answer.0.as_u16(), 206);
    assert_eq!(&answer.2[..], b"2345");
}

/// A PROPFIND keeps its method, headers and XML body through the adapter, and
/// its answer arrives whole.
#[tokio::test]
async fn a_propfind_goes_through_the_adapter_unchanged() {
    let server = server(vec![Reply::Now, MULTI_STATUS]).await;
    let (client, _, url) = warm_client(&server, &ExecutionControl::default()).await;
    let method = reqwest::Method::from_bytes(b"PROPFIND").expect("method");
    let request = client
        .request(method, url)
        .header("Depth", "1")
        .header("Content-Type", "application/xml")
        .body(PROPFIND_BODY);
    let answer = send_and_read(&server, request).await;
    assert_propfind(&server, &answer);
}

/// A HEAD answer declaring a length has no body, and reading it does not wait
/// for one.
#[tokio::test]
async fn a_head_answer_with_a_length_has_no_body() {
    let server = server(vec![Reply::Now, HEAD_ANSWER]).await;
    let (client, _, url) = warm_client(&server, &ExecutionControl::default()).await;
    let started = Instant::now();
    let answer = send_and_read(&server, client.head(url)).await;
    assert_head(&server, &answer);
    assert_eq!(started.elapsed(), Duration::ZERO);
}

/// A ranged GET keeps its `Range` header and gets the partial body.
#[tokio::test]
async fn a_ranged_get_keeps_its_range() {
    let server = server(vec![Reply::Now, PARTIAL]).await;
    let (client, _, url) = warm_client(&server, &ExecutionControl::default()).await;
    let request = client.get(url).header("Range", "bytes=2-5");
    let answer = send_and_read(&server, request).await;
    assert_range(&server, &answer);
}

/// The same three requests through the conversion `object_store` uses.
#[cfg(feature = "azure")]
#[tokio::test]
async fn object_store_requests_go_through_the_conversion_unchanged() {
    let server = server(vec![Reply::Now, MULTI_STATUS]).await;
    let method = reqwest::Method::from_bytes(b"PROPFIND").expect("method");
    let (answer, _) = azure_request(
        &server,
        method,
        &[("depth", "1"), ("content-type", "application/xml")],
        PROPFIND_BODY.to_owned().into(),
    )
    .await;
    assert_propfind(&server, &answer.expect("propfind"));
}

/// A HEAD answer with a length through the `object_store` conversion.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_object_store_head_with_a_length_has_no_body() {
    let server = server(vec![Reply::Now, HEAD_ANSWER]).await;
    let (answer, elapsed) = azure_request(
        &server,
        reqwest::Method::HEAD,
        &[],
        object_store::client::HttpRequestBody::empty(),
    )
    .await;
    assert_head(&server, &answer.expect("head"));
    assert_eq!(elapsed, Duration::ZERO);
}

/// A ranged GET through the `object_store` conversion.
#[cfg(feature = "azure")]
#[tokio::test]
async fn an_object_store_ranged_get_keeps_its_range() {
    let server = server(vec![Reply::Now, PARTIAL]).await;
    let (answer, _) = azure_request(
        &server,
        reqwest::Method::GET,
        &[("range", "bytes=2-5")],
        object_store::client::HttpRequestBody::empty(),
    )
    .await;
    assert_range(&server, &answer.expect("range"));
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
