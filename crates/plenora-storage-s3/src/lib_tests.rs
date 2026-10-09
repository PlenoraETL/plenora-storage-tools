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
    //! multipart upload whose part is answered late or never, and a download
    //! that is silent or trickles.
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

    /// When the server answers a part upload and a download.
    #[derive(Clone, Copy, Debug)]
    pub struct Script {
        /// Delay before answering a part upload; `None` never answers.
        pub part: Option<Duration>,
        /// A download sends `count` bytes, one every `every`; `None` is silent.
        pub download: Option<(Duration, usize)>,
    }

    pub struct Server {
        pub address: SocketAddr,
        /// Requests received in full, on any connection.
        pub requests: Arc<AtomicUsize>,
        /// Bytes of trickled downloads the client has read; the test counts them.
        pub received: Arc<AtomicUsize>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for Server {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    pub async fn start(script: Script) -> Server {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let requests = Arc::new(AtomicUsize::new(0));
        let received = Arc::new(AtomicUsize::new(0));
        let counter = requests.clone();
        let delivered = received.clone();
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
        while let Some((method, target)) = read_request(&mut socket).await {
            requests.fetch_add(1, Ordering::SeqCst);
            let answer = match (method.as_str(), target.contains("?uploads")) {
                ("POST", true) => xml(
                    "<InitiateMultipartUploadResult><Bucket>bucket</Bucket><Key>object</Key>\
                     <UploadId>upload</UploadId></InitiateMultipartUploadResult>",
                ),
                ("POST", false) => xml(
                    "<CompleteMultipartUploadResult><ETag>\"e\"</ETag></CompleteMultipartUploadResult>",
                ),
                ("PUT", _) if target.contains("partNumber=") => {
                    let Some(delay) = script.part else {
                        std::future::pending::<()>().await;
                        return;
                    };
                    tokio::time::sleep(delay).await;
                    let seen = requests.load(Ordering::SeqCst);
                    socket
                        .write_all(b"HTTP/1.1 200 OK\r\nETag: \"p\"\r\nContent-Length: 0\r\n\r\n")
                        .await
                        .expect("answer");
                    // No virtual time passes until the completion request
                    // arrives, on whatever connection the pool picks; it may
                    // be this one, so another task holds the clock.
                    let requests = requests.clone();
                    tokio::spawn(async move {
                        while requests.load(Ordering::SeqCst) == seen {
                            tokio::task::yield_now().await;
                        }
                    });
                    continue;
                }
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
                    for sent in 1..=count {
                        tokio::time::sleep(every).await;
                        socket.write_all(b"x").await.expect("byte");
                        // Paused time jumps to the next timer as soon as no
                        // task can run, even with a byte still on its way
                        // through the socket: the read timeout could then
                        // fire first. Staying runnable until the client has
                        // read it keeps the clock still meanwhile.
                        while received.load(Ordering::SeqCst) < sent {
                            tokio::task::yield_now().await;
                        }
                    }
                    continue;
                }
            };
            socket.write_all(answer.as_bytes()).await.expect("answer");
        }
    }

    fn xml(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
    }

    /// Reads one request, body included; `None` when the client closed the
    /// connection.
    async fn read_request(socket: &mut TcpStream) -> Option<(String, String)> {
        let mut head = Vec::new();
        let mut byte = [0_u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            if socket.read(&mut byte).await.expect("request") == 0 {
                return None;
            }
            head.push(byte[0]);
        }
        let head = String::from_utf8(head).expect("ASCII head");
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
        let mut body = vec![0; length];
        socket.read_exact(&mut body).await.expect("body");
        Some((method, target))
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

/// The store the S3 provider builds for an operation under `control`, against
/// the fake endpoint. Its two pooled connections (the read client and the
/// upload client) are opened in real time by a HEAD and a PUT, then tokio
/// time is paused.
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
    let path = object_store::path::Path::from("object");
    store.head(&path).await.expect("warm-up read");
    store
        .put(&path, object_store::PutPayload::from_static(b"x"))
        .await
        .expect("warm-up upload");
    tokio::time::pause();
    store
}

/// Keeps virtual time still until the server has received `count` requests
/// in total, warm-up included. The pool decides when a request reuses a
/// connection and when it opens one: either way that happens before any
/// virtual time passes, so no connection timeout can fire while a loopback
/// connection is being set up, and the waits under test start only once the
/// request is at the server.
fn hold_time_until(server: &fake_s3::Server, count: usize) {
    let requests = server.requests.clone();
    tokio::spawn(async move {
        while requests.load(std::sync::atomic::Ordering::SeqCst) < count {
            tokio::task::yield_now().await;
        }
    });
}

/// A multipart upload of one part, through the operation control as the
/// provider runs it, with the (virtual) time it took.
async fn multipart_upload(
    store: &object_store::aws::AmazonS3,
    control: &plenora_storage_core::ExecutionControl,
) -> (plenora_storage_core::StorageResult<()>, std::time::Duration) {
    use object_store::ObjectStore;
    use plenora_storage_core::ErrorPhase;
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
                    .put_part(object_store::PutPayload::from_static(b"part"))
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

/// Without a deadline a multipart upload whose part is answered after 400 s
/// completes: requests with a body go through the upload client, which has
/// no client limit and leaves only the caller's deadline. The read limit
/// would have cut it at 300 s.
#[tokio::test]
async fn without_deadline_an_s3_multipart_upload_is_not_cut_by_the_read_limit() {
    let server = fake_s3::start(fake_s3::Script {
        part: Some(std::time::Duration::from_secs(400)),
        download: None,
    })
    .await;
    let control = plenora_storage_core::ExecutionControl::default();
    let store = warm_store(&server, &control).await;
    hold_time_until(&server, 4);
    let (result, elapsed) = multipart_upload(&store, &control).await;
    result.expect("upload");
    assert!(
        elapsed >= std::time::Duration::from_secs(400),
        "{elapsed:?}"
    );
}

/// With a ten-minute deadline a multipart upload whose part is never
/// answered waits until the deadline and ends as a `timeout` of unknown
/// effect. Before 3.0.0 the client gave up after a fixed 30 s.
#[tokio::test]
async fn an_s3_multipart_upload_may_wait_until_the_deadline() {
    use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};
    let server = fake_s3::start(fake_s3::Script {
        part: None,
        download: None,
    })
    .await;
    let control = plenora_storage_core::ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(600));
    let store = warm_store(&server, &control).await;
    hold_time_until(&server, 4);
    let (result, elapsed) = multipart_upload(&store, &control).await;
    let error = result.expect_err("timeout");
    assert_eq!(
        (error.category, error.remote_effect, error.retry),
        (
            ErrorCategory::Timeout,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery
        )
    );
    assert!(
        elapsed >= std::time::Duration::from_secs(590),
        "{elapsed:?}"
    );
    assert!(
        elapsed <= std::time::Duration::from_secs(601),
        "{elapsed:?}"
    );
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
                while let Some(chunk) = futures_util::StreamExt::next(&mut body).await {
                    let chunk = chunk
                        .map_err(|error| super::map_store_error(error, ErrorPhase::Read, false))?;
                    received.fetch_add(chunk.len(), std::sync::atomic::Ordering::SeqCst);
                }
                Ok(received.load(std::sync::atomic::Ordering::SeqCst))
            },
            ErrorPhase::Read,
            false,
        )
        .await;
    (result, started.elapsed())
}

/// Without a deadline a silent S3 download is given up on after the declared
/// read limit, not a hidden 30 s, and reported as a safe `timeout`.
#[tokio::test]
async fn without_deadline_a_silent_s3_download_ends_at_the_read_limit() {
    use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};
    let server = fake_s3::start(fake_s3::Script {
        part: None,
        download: None,
    })
    .await;
    let store = warm_store(&server, &plenora_storage_core::ExecutionControl::default()).await;
    hold_time_until(&server, 3);
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
    assert!(
        elapsed >= super::READ_TIMEOUT_WITHOUT_DEADLINE,
        "{elapsed:?}"
    );
    assert!(
        elapsed <= super::READ_TIMEOUT_WITHOUT_DEADLINE + std::time::Duration::from_secs(1),
        "{elapsed:?}"
    );
}

/// Without a deadline an S3 download that keeps moving is never cut, however
/// long it lasts: the read limit counts silence, not the whole transfer.
#[tokio::test]
async fn without_deadline_an_s3_download_that_keeps_moving_is_not_cut() {
    let server = fake_s3::start(fake_s3::Script {
        part: None,
        download: Some((std::time::Duration::from_secs(100), 6)),
    })
    .await;
    let store = warm_store(&server, &plenora_storage_core::ExecutionControl::default()).await;
    hold_time_until(&server, 3);
    let (result, elapsed) = download(&store, &server.received).await;
    assert_eq!(result.expect("download"), 6);
    assert!(
        elapsed >= std::time::Duration::from_secs(600),
        "{elapsed:?}"
    );
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
    assert_eq!(open.read, Some(super::READ_TIMEOUT_WITHOUT_DEADLINE));
    assert_eq!(open.connect, super::CLIENT_CONNECT_TIMEOUT);
}
