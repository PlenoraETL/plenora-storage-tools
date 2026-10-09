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

mod fake_s3 {
    //! A minimal S3 endpoint for the timeout tests: path-style requests, a
    //! multipart upload whose part and completion are answered as scripted,
    //! and a download that is silent or trickles.
    use std::{
        net::SocketAddr,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
        time::Duration,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpStream,
    };

    /// Yields until `ready` holds. A condition that never comes true fails
    /// the test after 10 s of real time instead of hanging it.
    pub async fn yield_until(mut ready: impl FnMut() -> bool, what: &str) {
        let started = std::time::Instant::now();
        while !ready() {
            assert!(started.elapsed() < Duration::from_secs(10), "{what}");
            tokio::task::yield_now().await;
        }
    }

    /// How the server treats a part upload.
    #[derive(Clone, Copy, Debug)]
    pub enum Part {
        /// Reads it and answers at once.
        Now,
        /// Reads it and never answers.
        Never,
        /// Never reads it.
        NeverRead,
        /// Reads `chunk` bytes every `every`, then answers.
        Slowly { chunk: usize, every: Duration },
    }

    /// When the server answers a part, the completion and a download.
    #[derive(Clone, Copy, Debug)]
    pub struct Script {
        pub part: Part,
        /// Whether the completion is answered.
        pub complete: bool,
        /// A download sends `count` bytes, one every `every`; `None` is silent.
        pub download: Option<(Duration, usize)>,
    }

    pub const IDLE: Script = Script {
        part: Part::Now,
        complete: true,
        download: None,
    };

    pub struct Server {
        pub address: SocketAddr,
        /// Request heads received, on any connection.
        pub requests: Arc<AtomicUsize>,
        /// Answers and download bytes the client has received; the tests
        /// count them.
        pub received: Arc<AtomicUsize>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for Server {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    pub fn start(script: Script) -> Server {
        // A small receive buffer, inherited by accepted sockets, so that the
        // client cannot park a whole part in socket buffers and must keep
        // sending while the server reads.
        let socket = tokio::net::TcpSocket::new_v4().expect("socket");
        socket.set_recv_buffer_size(64 * 1024).expect("buffer");
        socket
            .bind(SocketAddr::from(([127, 0, 0, 1], 0)))
            .expect("bind");
        let listener = socket.listen(16).expect("listen");
        let address = listener.local_addr().expect("address");
        let requests = Arc::new(AtomicUsize::new(0));
        let received = Arc::new(AtomicUsize::new(0));
        let (counter, delivered) = (requests.clone(), received.clone());
        let task = tokio::spawn(async move {
            let mut connections = tokio::task::JoinSet::new();
            loop {
                let (socket, _) = listener.accept().await.expect("accept");
                connections.spawn(serve(socket, script, counter.clone(), delivered.clone()));
            }
        });
        Server {
            address,
            requests,
            received,
            task,
        }
    }

    const OBJECT_HEADERS: &str = "Last-Modified: Thu, 01 Jan 2026 00:00:00 GMT\r\nETag: \"e\"\r\n";

    async fn serve(
        mut socket: TcpStream,
        script: Script,
        requests: Arc<AtomicUsize>,
        received: Arc<AtomicUsize>,
    ) {
        while let Some((method, target, length)) = read_head(&mut socket).await {
            requests.fetch_add(1, Ordering::SeqCst);
            let part = method == "PUT" && target.contains("partNumber=");
            if part && matches!(script.part, Part::NeverRead) {
                std::future::pending::<()>().await;
            }
            if let (true, Part::Slowly { chunk, every }) = (part, script.part) {
                let mut left = length;
                while left > 0 {
                    tokio::time::sleep(every).await;
                    let take = left.min(chunk);
                    spin_read(&socket, take).await;
                    left -= take;
                }
            } else {
                let mut body = vec![0; length];
                socket.read_exact(&mut body).await.expect("body");
            }
            let answer = match (method.as_str(), target.contains("?uploads")) {
                ("POST", true) => xml(
                    "<InitiateMultipartUploadResult><Bucket>bucket</Bucket><Key>object</Key>\
                     <UploadId>upload</UploadId></InitiateMultipartUploadResult>",
                ),
                ("POST", false) if !script.complete => {
                    std::future::pending::<()>().await;
                    return;
                }
                ("POST", false) => xml(
                    "<CompleteMultipartUploadResult><ETag>\"e\"</ETag></CompleteMultipartUploadResult>",
                ),
                ("PUT", _) if part => match script.part {
                    Part::Never | Part::NeverRead => {
                        std::future::pending::<()>().await;
                        return;
                    }
                    Part::Now | Part::Slowly { .. } => {
                        "HTTP/1.1 200 OK\r\nETag: \"p\"\r\nContent-Length: 0\r\n\r\n".to_owned()
                    }
                },
                ("PUT", _) => {
                    "HTTP/1.1 200 OK\r\nETag: \"e\"\r\nContent-Length: 0\r\n\r\n".to_owned()
                }
                ("HEAD", _) => {
                    format!("HTTP/1.1 200 OK\r\n{OBJECT_HEADERS}Content-Length: 0\r\n\r\n")
                }
                _ => {
                    let Some((every, count)) = script.download else {
                        std::future::pending::<()>().await;
                        return;
                    };
                    let head = format!(
                        "HTTP/1.1 200 OK\r\n{OBJECT_HEADERS}Content-Length: {count}\r\n\r\n"
                    );
                    socket.write_all(head.as_bytes()).await.expect("head");
                    let base = received.load(Ordering::SeqCst);
                    for sent in 1..=count {
                        tokio::time::sleep(every).await;
                        socket.write_all(b"x").await.expect("byte");
                        // Paused time jumps to the next timer as soon as no
                        // task can run, even with the byte still in the
                        // socket. Staying runnable until the client has it
                        // keeps the clock still meanwhile.
                        yield_until(
                            || received.load(Ordering::SeqCst) >= base + sent,
                            "the client never read a trickled byte",
                        )
                        .await;
                    }
                    continue;
                }
            };
            socket.write_all(answer.as_bytes()).await.expect("answer");
        }
    }

    /// Reads `length` bytes without ever leaving the runtime idle, so virtual
    /// time stands still while the client refills the socket: each chunk the
    /// server reads makes the client's transport take new pieces of the body
    /// before the clock can move. Fails the test after 10 s of real time.
    async fn spin_read(socket: &TcpStream, mut length: usize) {
        let started = std::time::Instant::now();
        let mut buffer = vec![0; 64 * 1024];
        while length > 0 {
            let want = length.min(buffer.len());
            match socket.try_read(&mut buffer[..want]) {
                Ok(0) => panic!("the client closed the connection"),
                Ok(read) => length -= read,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        started.elapsed() < Duration::from_secs(10),
                        "the client stopped sending"
                    );
                    tokio::task::yield_now().await;
                }
                Err(error) => panic!("read failed: {error}"),
            }
        }
    }

    fn xml(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
    }

    /// Reads one request head: method, target and body length. `None` when
    /// the client closed the connection.
    async fn read_head(socket: &mut TcpStream) -> Option<(String, String, usize)> {
        let mut head = Vec::new();
        let mut byte = [0_u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            if socket.read(&mut byte).await.expect("request") == 0 {
                return None;
            }
            head.push(byte[0]);
        }
        let head = String::from_utf8(head).expect("ASCII head");
        assert!(
            !head.to_ascii_lowercase().contains("transfer-encoding"),
            "S3 requests carry a length"
        );
        let mut lines = head.split("\r\n");
        let mut request_line = lines.next().expect("request line").split(' ');
        let method = request_line.next().expect("method").to_owned();
        let target = request_line.next().expect("target").to_owned();
        let length = lines
            .find_map(|line| {
                line.to_ascii_lowercase()
                    .strip_prefix("content-length:")
                    .map(|value| value.trim().parse::<usize>().expect("length"))
            })
            .unwrap_or(0);
        Some((method, target, length))
    }
}

struct TestCredentials;

impl plenora_storage_core::CredentialResolver for TestCredentials {
    fn resolve(
        &self,
        _reference: &str,
    ) -> plenora_storage_core::StorageResult<plenora_storage_core::CredentialMaterial> {
        Ok(plenora_storage_core::CredentialMaterial::new(
            std::collections::BTreeMap::from([
                ("access_key_id".to_owned(), "key".to_owned()),
                ("secret_access_key".to_owned(), "secret".to_owned()),
            ]),
        ))
    }
}

const LIMIT: std::time::Duration = super::READ_TIMEOUT_WITHOUT_DEADLINE;

/// The store the S3 provider builds for an operation under `control`, against
/// the fake endpoint. Its pooled connection is opened in real time by a HEAD,
/// then tokio time is paused.
async fn warm_store(
    server: &fake_s3::Server,
    control: &plenora_storage_core::ExecutionControl,
) -> object_store::aws::AmazonS3 {
    use object_store::ObjectStoreExt;
    let policy = plenora_storage_core::EngineConfig {
        allow_insecure_http: true,
        allow_private_network: true,
        ..plenora_storage_core::EngineConfig::default()
    };
    let connection = plenora_storage_core::ProviderConnection {
        provider: "s3".to_owned(),
        config_contract: super::CONFIG_CONTRACT.to_owned(),
        config: serde_json::json!({
            "endpoint": format!("http://{}", server.address),
            "bucket": "bucket",
            "region": "us-east-1",
        }),
        credential_ref: "test".to_owned(),
    };
    let store = super::S3Provider::new(std::sync::Arc::new(TestCredentials))
        .store(
            &connection,
            &plenora_storage_core::OperationContext {
                policy: &policy,
                control,
            },
        )
        .await
        .expect("store");
    store
        .head(&object_store::path::Path::from("object"))
        .await
        .expect("warm-up");
    tokio::time::pause();
    store
}

/// Keeps virtual time still until the server has received `count` request
/// heads in total, warm-up included. The pool decides when a request reuses
/// a connection and when it opens one: either way that happens before any
/// virtual time passes, so no client timer fires while a loopback connection
/// is set up. Await the handle at the end of the test.
fn hold_time_until(server: &fake_s3::Server, count: usize) -> tokio::task::JoinHandle<()> {
    let requests = server.requests.clone();
    tokio::spawn(async move {
        fake_s3::yield_until(
            || requests.load(std::sync::atomic::Ordering::SeqCst) >= count,
            "a request never reached the server",
        )
        .await;
    })
}

/// A multipart upload of one part of `size` bytes in 64 KiB chunks, through
/// the operation control as the provider runs it, with the (virtual) time it
/// took.
async fn multipart_upload(
    store: &object_store::aws::AmazonS3,
    control: &plenora_storage_core::ExecutionControl,
    size: usize,
) -> (plenora_storage_core::StorageResult<()>, std::time::Duration) {
    use object_store::ObjectStore;
    use plenora_storage_core::ErrorPhase;
    let mut part = object_store::PutPayloadMut::new().with_block_size(64 * 1024);
    part.extend_from_slice(&vec![0_u8; size]);
    let part = part.freeze();
    let started = tokio::time::Instant::now();
    let result = control
        .run(
            async {
                let path = object_store::path::Path::from("object");
                let mut upload = store
                    .put_multipart_opts(&path, object_store::PutMultipartOptions::default())
                    .await
                    .map_err(|error| super::map_store_error(error, ErrorPhase::Commit, true))?;
                upload
                    .put_part(part)
                    .await
                    .map_err(|error| super::map_store_error(error, ErrorPhase::Commit, true))?;
                upload
                    .complete()
                    .await
                    .map(drop)
                    .map_err(|error| super::map_store_error(error, ErrorPhase::Commit, true))
            },
            ErrorPhase::Commit,
            true,
        )
        .await;
    (result, started.elapsed())
}

fn assert_mutating_timeout(error: &plenora_storage_core::StorageError) {
    assert_eq!(error.category, plenora_storage_core::ErrorCategory::Timeout);
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::Unknown
    );
    assert_eq!(
        error.retry,
        plenora_storage_core::RetryDisposition::RequiresRecovery
    );
}

/// Without a deadline a 32 MiB part, held in memory as one piece, that the
/// server reads 4 MiB every 100 s keeps moving for 800 s and is not cut: the
/// adapter hands it to the transport in 64 KiB pieces and each one re-arms
/// the clock. The fixed 300 s read timeout of reqwest cut it.
#[tokio::test]
async fn without_deadline_an_s3_part_that_keeps_moving_is_not_cut() {
    let server = fake_s3::start(fake_s3::Script {
        part: fake_s3::Part::Slowly {
            chunk: 4 * 1024 * 1024,
            every: std::time::Duration::from_secs(100),
        },
        ..fake_s3::IDLE
    });
    let control = plenora_storage_core::ExecutionControl::default();
    let store = warm_store(&server, &control).await;
    let hold = hold_time_until(&server, 3);
    let (result, elapsed) = multipart_upload(&store, &control, 32 * 1024 * 1024).await;
    result.expect("upload");
    assert!(
        elapsed >= std::time::Duration::from_secs(800),
        "{elapsed:?}"
    );
    hold.await.expect("requests reached the server");
}

/// A part the server stops reading fails once the transport has taken
/// nothing for the whole limit, with the effect of a write.
#[tokio::test]
async fn without_deadline_an_s3_part_the_server_stops_reading_ends_at_the_limit() {
    let server = fake_s3::start(fake_s3::Script {
        part: fake_s3::Part::NeverRead,
        ..fake_s3::IDLE
    });
    let control = plenora_storage_core::ExecutionControl::default();
    let store = warm_store(&server, &control).await;
    let hold = hold_time_until(&server, 3);
    let (result, elapsed) = multipart_upload(&store, &control, 32 * 1024 * 1024).await;
    assert_mutating_timeout(&result.expect_err("timeout"));
    assert!(elapsed >= LIMIT, "{elapsed:?}");
    assert!(
        elapsed <= LIMIT + std::time::Duration::from_secs(1),
        "{elapsed:?}"
    );
    hold.await.expect("requests reached the server");
}

/// A completion the server never answers fails at the limit, and the
/// failure comes from the inactivity clock: `CompleteMultipartUpload` goes
/// through the same discipline as the part, whatever its body.
#[tokio::test]
async fn an_unanswered_s3_completion_ends_at_the_limit() {
    let server = fake_s3::start(fake_s3::Script {
        complete: false,
        ..fake_s3::IDLE
    });
    let control = plenora_storage_core::ExecutionControl::default();
    let store = warm_store(&server, &control).await;
    let hold = hold_time_until(&server, 4);
    let (result, elapsed) = multipart_upload(&store, &control, 1024).await;
    assert_mutating_timeout(&result.expect_err("timeout"));
    assert!(elapsed >= LIMIT, "{elapsed:?}");
    assert!(
        elapsed <= LIMIT + std::time::Duration::from_secs(1),
        "{elapsed:?}"
    );
    hold.await.expect("requests reached the server");
}

/// With a ten-minute deadline a part the server never answers waits until
/// the deadline, not the inactivity limit, and ends as a `timeout` of
/// unknown effect. Before 3.0.0 the client gave up after a fixed 30 s.
#[tokio::test]
async fn an_s3_multipart_upload_may_wait_until_the_deadline() {
    let server = fake_s3::start(fake_s3::Script {
        part: fake_s3::Part::Never,
        ..fake_s3::IDLE
    });
    let control = plenora_storage_core::ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(600));
    let store = warm_store(&server, &control).await;
    let hold = hold_time_until(&server, 3);
    let (result, elapsed) = multipart_upload(&store, &control, 1024).await;
    assert_mutating_timeout(&result.expect_err("timeout"));
    assert!(
        elapsed >= std::time::Duration::from_secs(590),
        "{elapsed:?}"
    );
    assert!(
        elapsed <= std::time::Duration::from_secs(601),
        "{elapsed:?}"
    );
    hold.await.expect("requests reached the server");
}

/// Downloads the object through the operation control without a deadline.
async fn download(
    store: &object_store::aws::AmazonS3,
    received: &std::sync::atomic::AtomicUsize,
) -> (
    plenora_storage_core::StorageResult<usize>,
    std::time::Duration,
) {
    use object_store::ObjectStoreExt;
    use plenora_storage_core::ErrorPhase;
    let started = tokio::time::Instant::now();
    let result = plenora_storage_core::ExecutionControl::default()
        .run(
            async {
                let path = object_store::path::Path::from("object");
                let object = store
                    .get(&path)
                    .await
                    .map_err(|error| super::map_store_error(error, ErrorPhase::Read, false))?;
                let mut body = object.into_stream();
                let mut length = 0;
                while let Some(chunk) = futures_util::StreamExt::next(&mut body).await {
                    let chunk = chunk
                        .map_err(|error| super::map_store_error(error, ErrorPhase::Read, false))?;
                    length += chunk.len();
                    received.fetch_add(chunk.len(), std::sync::atomic::Ordering::SeqCst);
                }
                Ok(length)
            },
            ErrorPhase::Read,
            false,
        )
        .await;
    (result, started.elapsed())
}

/// Without a deadline a silent S3 download is given up on after the declared
/// limit, not a hidden 30 s, and reported as a safe `timeout`.
#[tokio::test]
async fn without_deadline_a_silent_s3_download_ends_at_the_limit() {
    use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};
    let server = fake_s3::start(fake_s3::IDLE);
    let store = warm_store(&server, &plenora_storage_core::ExecutionControl::default()).await;
    let hold = hold_time_until(&server, 2);
    let (result, elapsed) = download(&store, &server.received).await;
    let error = result.expect_err("timeout");
    assert_eq!(
        (error.category, error.remote_effect, error.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::None,
            RetryDisposition::Safe
        )
    );
    assert!(elapsed >= LIMIT, "{elapsed:?}");
    assert!(
        elapsed <= LIMIT + std::time::Duration::from_secs(1),
        "{elapsed:?}"
    );
    hold.await.expect("request reached the server");
}

/// Without a deadline an S3 download that keeps moving is never cut, however
/// long it lasts: the limit counts inactivity, not the whole transfer.
#[tokio::test]
async fn without_deadline_an_s3_download_that_keeps_moving_is_not_cut() {
    let server = fake_s3::start(fake_s3::Script {
        download: Some((std::time::Duration::from_secs(100), 6)),
        ..fake_s3::IDLE
    });
    let store = warm_store(&server, &plenora_storage_core::ExecutionControl::default()).await;
    let hold = hold_time_until(&server, 2);
    let (result, elapsed) = download(&store, &server.received).await;
    assert_eq!(result.expect("download"), 6);
    assert!(
        elapsed >= std::time::Duration::from_secs(600),
        "{elapsed:?}"
    );
    hold.await.expect("request reached the server");
}

/// Other store failures keep their mapping.
#[test]
fn an_s3_failure_that_is_not_a_timeout_keeps_its_category() {
    let not_found = super::map_store_error(
        object_store::Error::NotFound {
            path: "object".to_owned(),
            source: "absent".into(),
        },
        plenora_storage_core::ErrorPhase::Read,
        false,
    );
    assert_eq!(
        not_found.category,
        plenora_storage_core::ErrorCategory::NotFound
    );
}

/// The connection timeout stays short and fixed unless less time remains.
#[test]
fn s3_connection_timeout_never_exceeds_the_deadline() {
    let short = super::ClientTimeouts::for_remaining(Some(std::time::Duration::from_secs(2)));
    assert_eq!(short.connect, std::time::Duration::from_secs(2));
    assert_eq!(short.total, Some(std::time::Duration::from_secs(2)));
    let open = super::ClientTimeouts::for_remaining(None);
    assert_eq!(open.total, None);
    assert_eq!(open.idle, Some(super::READ_TIMEOUT_WITHOUT_DEADLINE));
    assert_eq!(open.connect, super::CLIENT_CONNECT_TIMEOUT);
}
