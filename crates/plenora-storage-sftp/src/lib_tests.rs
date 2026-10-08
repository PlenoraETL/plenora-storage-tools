use super::{
    atomic_replace, discard_staged_object, qualify_atomic_session, remote_path, scan_directory,
    validate_key,
};
use plenora_storage_core::{
    EngineConfig, ErrorCategory, ErrorPhase, ExecutionControl, OperationContext, RemoteEffect,
    RetryDisposition,
};
use russh_sftp::{
    client::{RawSftpSession, SftpSession},
    protocol::{File, Handle, Name, Packet, Status, StatusCode, Version},
    server::Handler,
};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

#[test]
fn private_key_errors_and_ambiguous_credentials_are_redacted() {
    use super::{CredentialMaterial, SftpAuthentication};
    for fields in [
        vec![
            ("private_key", "private-material-never-echo"),
            ("passphrase", "secret-phrase"),
        ],
        vec![
            ("private_key", "private-material-never-echo"),
            ("password", "secret-password"),
        ],
        vec![
            ("password", "secret-password"),
            ("passphrase", "secret-phrase"),
        ],
        vec![],
    ] {
        let material = CredentialMaterial::new(
            fields
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
        );
        let Err(error) = SftpAuthentication::from_material(&material) else {
            panic!("invalid credentials accepted");
        };
        assert_eq!(error.remote_effect, RemoteEffect::None);
        assert_eq!(error.phase, ErrorPhase::Validate);
        let public = serde_json::to_string(&error).expect("public error");
        assert!(!public.contains("private-material-never-echo"));
        assert!(!public.contains("secret-phrase"));
        assert!(!public.contains("secret-password"));
    }
}

#[tokio::test]
async fn host_key_pin_is_required_unless_explicitly_disabled() {
    use russh::{
        client::Handler as _,
        keys::{HashAlg, PublicKey, PublicKeyOrCertificate},
    };

    let key = PublicKey::from_openssh(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILM+rvN+ot98qgEN796jTiQfZfG1KaT0PtFDJ/XFSqti",
    )
    .expect("public test key");
    let fingerprint = key.fingerprint(HashAlg::Sha256).to_string();
    let server_key = PublicKeyOrCertificate::from(key);
    for (expected_fingerprint, allow_unverified, accepted) in [
        (Some(fingerprint), false, true),
        (Some("SHA256:wrong-key".to_owned()), false, false),
        (None, false, false),
        (None, true, true),
    ] {
        let mut handler = super::SshClient {
            expected_fingerprint,
            allow_unverified,
        };
        assert_eq!(
            handler
                .check_server_key(&server_key)
                .await
                .expect("host key check"),
            accepted
        );
    }
}

#[test]
fn keys_cannot_escape_the_remote_root() {
    assert!(validate_key("folder/object.bin").is_ok());
    assert!(validate_key("../secret").is_err());
    assert!(validate_key("/absolute").is_err());
    assert_eq!(remote_path("upload", "a/b"), "upload/a/b");
}

struct EndlessDirectory(Arc<AtomicUsize>);

struct ParentCreationRace {
    mode: Option<u32>,
    created: bool,
}

#[allow(
    clippy::unused_async_trait_impl,
    reason = "russh-sftp declares the handler methods as async; the test servers keep that shape"
)]
impl Handler for ParentCreationRace {
    type Error = StatusCode;

    fn unimplemented(&self) -> Self::Error {
        StatusCode::OpUnsupported
    }

    async fn init(
        &mut self,
        _version: u32,
        _extensions: std::collections::HashMap<String, String>,
    ) -> Result<Version, Self::Error> {
        Ok(Version::new())
    }

    async fn stat(
        &mut self,
        id: u32,
        path: String,
    ) -> Result<russh_sftp::protocol::Attrs, Self::Error> {
        assert_eq!(path, "parent");
        if !self.created {
            return Err(StatusCode::NoSuchFile);
        }
        let permissions = self.mode.ok_or(StatusCode::NoSuchFile)?;
        Ok(russh_sftp::protocol::Attrs {
            id,
            attrs: russh_sftp::protocol::FileAttributes {
                permissions: Some(permissions),
                ..Default::default()
            },
        })
    }

    async fn mkdir(
        &mut self,
        _id: u32,
        path: String,
        _attrs: russh_sftp::protocol::FileAttributes,
    ) -> Result<Status, Self::Error> {
        assert_eq!(path, "parent");
        self.created = true;
        Err(StatusCode::Failure)
    }
}

#[tokio::test]
async fn concurrent_parent_creation_requires_proof_of_a_directory() {
    for (mode, succeeds) in [
        (Some(0o040_755), true),
        (Some(0o100_644), false),
        (None, false),
    ] {
        let (client, server) = tokio::io::duplex(4096);
        let server = tokio::spawn(russh_sftp::server::run(
            server,
            ParentCreationRace {
                mode,
                created: false,
            },
        ));
        let sftp = SftpSession::new(client).await.expect("session");
        let policy = plenora_storage_core::EngineConfig::default();
        let control = ExecutionControl::default()
            .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
        let mut prepared = false;
        let result = super::ensure_parent_directories(
            &sftp,
            "parent/object",
            &plenora_storage_core::OperationContext {
                policy: &policy,
                control: &control,
            },
            &mut prepared,
        )
        .await;
        assert_eq!(result.is_ok(), succeeds);
        assert!(prepared);
        sftp.close().await.expect("close");
        server.abort();
    }
}

struct InterruptedCommit {
    state: Arc<AtomicUsize>,
    commit: bool,
    reached: Arc<tokio::sync::Notify>,
}

#[allow(
    clippy::unused_async_trait_impl,
    reason = "russh-sftp declares the handler methods as async; the test servers keep that shape"
)]
impl Handler for InterruptedCommit {
    type Error = StatusCode;

    fn unimplemented(&self) -> Self::Error {
        StatusCode::OpUnsupported
    }

    async fn init(
        &mut self,
        _version: u32,
        _extensions: std::collections::HashMap<String, String>,
    ) -> Result<Version, Self::Error> {
        let mut version = Version::new();
        version
            .extensions
            .insert("posix-rename@openssh.com".to_owned(), "1".to_owned());
        Ok(version)
    }

    async fn extended(
        &mut self,
        _id: u32,
        request: String,
        data: Vec<u8>,
    ) -> Result<Packet, Self::Error> {
        assert_eq!(request, "posix-rename@openssh.com");
        assert_eq!(data, b"\0\0\0\x07staging\0\0\0\x05final");
        if self.commit {
            self.state.store(1, Ordering::SeqCst);
        }
        self.reached.notify_one();
        std::future::pending().await // Simulate a lost response, not an explicit rejection.
    }

    async fn remove(&mut self, id: u32, path: String) -> Result<Status, Self::Error> {
        assert_eq!(
            path, "staging",
            "cleanup must never remove the final object"
        );
        if self
            .state
            .compare_exchange(0, 2, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err(StatusCode::NoSuchFile);
        }
        Ok(Status {
            id,
            status_code: StatusCode::Ok,
            error_message: String::new(),
            language_tag: String::new(),
        })
    }
}

#[tokio::test]
async fn interrupted_atomic_commit_preserves_final_object_and_reports_provable_effects() {
    for committed in [false, true] {
        for cancellation in [false, true] {
            let state = Arc::new(AtomicUsize::new(0));
            let reached = Arc::new(tokio::sync::Notify::new());
            let (client, server) = tokio::io::duplex(4096);
            let commit_server = tokio::spawn(russh_sftp::server::run(
                server,
                InterruptedCommit {
                    state: state.clone(),
                    commit: committed,
                    reached: reached.clone(),
                },
            ));
            let raw = RawSftpSession::new(client);
            qualify_atomic_session(&raw).await.unwrap();
            let (client, server) = tokio::io::duplex(4096);
            let cleanup_server = tokio::spawn(russh_sftp::server::run(
                server,
                InterruptedCommit {
                    state: state.clone(),
                    commit: committed,
                    reached: reached.clone(),
                },
            ));
            let cleanup = SftpSession::new(client).await.unwrap();
            let control = ExecutionControl::default()
                .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
            let token = control.cancellation.clone();
            let (outcome, ()) = tokio::join!(
                control.run(
                    atomic_replace(&raw, "staging", "final"),
                    ErrorPhase::Commit,
                    true
                ),
                async {
                    reached.notified().await;
                    if cancellation {
                        token.cancel();
                    }
                }
            );
            let error =
                discard_staged_object(&cleanup, true, "staging", outcome.unwrap_err()).await;
            assert_eq!(
                error.category,
                if cancellation {
                    ErrorCategory::Cancelled
                } else {
                    ErrorCategory::Timeout
                }
            );
            assert_eq!(
                error.remote_effect,
                if committed {
                    RemoteEffect::Unknown
                } else {
                    RemoteEffect::RolledBack
                }
            );
            assert_eq!(
                error.retry,
                if committed {
                    RetryDisposition::RequiresRecovery
                } else {
                    RetryDisposition::Safe
                }
            );
            assert_eq!(state.load(Ordering::SeqCst), if committed { 1 } else { 2 });
            commit_server.abort();
            cleanup_server.abort();
        }
    }
}

#[tokio::test]
async fn atomic_publication_rejects_a_server_without_posix_rename() {
    let (client, server) = tokio::io::duplex(4096);
    let server = tokio::spawn(russh_sftp::server::run(
        server,
        EndlessDirectory(Arc::new(AtomicUsize::new(0))),
    ));
    let session = RawSftpSession::new(client);
    let error = qualify_atomic_session(&session)
        .await
        .expect_err("extension is mandatory");
    server.abort();
    assert_eq!(
        error.category,
        plenora_storage_core::ErrorCategory::Unsupported
    );
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::None
    );
}

#[allow(
    clippy::unused_async_trait_impl,
    reason = "russh-sftp declares the handler methods as async; the test servers keep that shape"
)]
impl Handler for EndlessDirectory {
    type Error = StatusCode;
    fn unimplemented(&self) -> Self::Error {
        StatusCode::OpUnsupported
    }
    async fn opendir(&mut self, id: u32, _path: String) -> Result<Handle, Self::Error> {
        Ok(Handle {
            id,
            handle: "directory".to_owned(),
        })
    }
    async fn readdir(&mut self, id: u32, _handle: String) -> Result<Name, Self::Error> {
        let count = self.0.fetch_add(1, Ordering::SeqCst);
        Ok(Name {
            id,
            files: vec![File::dummy(format!("file-{count}"))],
        })
    }
}

#[tokio::test]
async fn listing_stops_between_batches_without_waiting_for_directory_eof() {
    let requests = Arc::new(AtomicUsize::new(0));
    let (client, server) = tokio::io::duplex(4096);
    let server = tokio::spawn(russh_sftp::server::run(
        server,
        EndlessDirectory(requests.clone()),
    ));
    let listing = RawSftpSession::new(client);
    listing.init().await.expect("init");
    let policy = EngineConfig {
        max_list_items: 2,
        ..EngineConfig::default()
    };
    let control = ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
    let mut visited = Vec::new();
    let error = scan_directory(
        &listing,
        ".",
        &OperationContext {
            policy: &policy,
            control: &control,
        },
        &mut 0,
        |file| {
            visited.push(file.filename);
            Ok(())
        },
    )
    .await
    .expect_err("scan bound before EOF");
    server.abort();
    assert_eq!(error.code, "LIST_SCAN_LIMIT_EXCEEDED");
    assert_eq!(visited, ["file-0", "file-1"]);
    assert_eq!(requests.load(Ordering::SeqCst), 3);
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

/// An SFTP server that announces `fsync@openssh.com` and answers fsync only
/// after `fsync_delay` of (paused, deterministic) time: a slow but correct
/// flush of a large upload on a loaded server.
struct SlowFsync {
    fsync_delay: std::time::Duration,
}

#[allow(
    clippy::unused_async_trait_impl,
    reason = "russh-sftp declares the handler methods as async; the test servers keep that shape"
)]
impl Handler for SlowFsync {
    type Error = StatusCode;

    fn unimplemented(&self) -> Self::Error {
        StatusCode::OpUnsupported
    }

    async fn init(
        &mut self,
        _version: u32,
        _extensions: std::collections::HashMap<String, String>,
    ) -> Result<Version, Self::Error> {
        let mut version = Version::new();
        version
            .extensions
            .insert("fsync@openssh.com".to_owned(), "1".to_owned());
        Ok(version)
    }

    async fn open(
        &mut self,
        id: u32,
        _filename: String,
        _pflags: russh_sftp::protocol::OpenFlags,
        _attrs: russh_sftp::protocol::FileAttributes,
    ) -> Result<Handle, Self::Error> {
        Ok(Handle {
            id,
            handle: "slow".to_owned(),
        })
    }

    async fn extended(
        &mut self,
        id: u32,
        request: String,
        _data: Vec<u8>,
    ) -> Result<Packet, Self::Error> {
        assert_eq!(request, "fsync@openssh.com");
        tokio::time::sleep(self.fsync_delay).await;
        Ok(Packet::Status(ok_status(id)))
    }

    async fn close(&mut self, id: u32, _handle: String) -> Result<Status, Self::Error> {
        Ok(ok_status(id))
    }
}

fn ok_status(id: u32) -> Status {
    Status {
        id,
        status_code: StatusCode::Ok,
        error_message: String::new(),
        language_tag: String::new(),
    }
}

/// Demonstrates the cause of the 3.0.0 campaign failure: russh-sftp gives
/// every request a fixed 10 s timeout, so an fsync that takes 30 s fails
/// although the operation deadline is a minute away.
#[tokio::test(start_paused = true)]
async fn library_default_request_timeout_fails_a_slow_fsync() {
    let (client, server) = tokio::io::duplex(4096);
    let server = tokio::spawn(russh_sftp::server::run(
        server,
        SlowFsync {
            fsync_delay: std::time::Duration::from_secs(30),
        },
    ));
    let sftp = SftpSession::new(client).await.expect("session");
    let file = sftp.create("object").await.expect("create");
    let result = file.sync_all().await;
    assert!(
        matches!(result, Err(russh_sftp::client::error::Error::Timeout)),
        "{result:?}"
    );
    server.abort();
}

/// Opens a product session against `SlowFsync`, creates a file and completes
/// it with the product's commit step under `control`.
async fn finish_after_slow_fsync(
    fsync_delay: std::time::Duration,
    control: ExecutionControl,
) -> plenora_storage_core::StorageResult<()> {
    let (client, server) = tokio::io::duplex(4096);
    let server = tokio::spawn(russh_sftp::server::run(server, SlowFsync { fsync_delay }));
    let sftp = super::open_session(client, &control)
        .await
        .expect("session");
    let mut file = sftp.create("object").await.expect("create");
    let policy = EngineConfig::default();
    let result = super::publication::finish_written_file(
        &mut file,
        &OperationContext {
            policy: &policy,
            control: &control,
        },
    )
    .await;
    server.abort();
    result
}

fn after(seconds: u64) -> ExecutionControl {
    ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(seconds))
}

/// The commit `fsync` may take as long as the operation deadline allows.
/// Before 3.0.0 the library's fixed 10 s request timeout failed a 30 s fsync
/// with a minute left, as `SFTP_TRANSFER_IO_FAILED`.
#[tokio::test(start_paused = true)]
async fn a_slow_fsync_within_the_deadline_completes() {
    let result = finish_after_slow_fsync(std::time::Duration::from_secs(30), after(60)).await;
    assert!(result.is_ok(), "{result:?}");
}

/// Without a deadline the request limit is the declared
/// `REQUEST_TIMEOUT_WITHOUT_DEADLINE`, not a hidden 10 s.
#[tokio::test(start_paused = true)]
async fn a_slow_fsync_without_deadline_completes_within_the_declared_limit() {
    let result = finish_after_slow_fsync(
        std::time::Duration::from_secs(30),
        ExecutionControl::default(),
    )
    .await;
    assert!(result.is_ok(), "{result:?}");
}

/// An fsync that is never answered within the limit is a `timeout` with the
/// effect of a write: unknown, requires recovery. Before 3.0.0 it was `io`.
#[tokio::test(start_paused = true)]
async fn an_unanswered_fsync_is_a_timeout_with_unknown_effect() {
    let delay = super::REQUEST_TIMEOUT_WITHOUT_DEADLINE + std::time::Duration::from_secs(60);
    for control in [ExecutionControl::default(), after(20)] {
        let error = finish_after_slow_fsync(delay, control)
            .await
            .expect_err("unanswered fsync");
        assert_eq!(
            (
                error.category,
                error.phase,
                error.remote_effect,
                error.retry.clone()
            ),
            (
                ErrorCategory::Timeout,
                ErrorPhase::Commit,
                RemoteEffect::Unknown,
                RetryDisposition::RequiresRecovery
            )
        );
    }
}

/// The request timeout follows the time remaining: rounded up, never below one
/// second, and the declared limit without a deadline.
#[test]
fn request_timeout_follows_the_remaining_deadline() {
    assert_eq!(
        super::request_timeout_secs(&ExecutionControl::default()),
        super::REQUEST_TIMEOUT_WITHOUT_DEADLINE.as_secs()
    );
    let control = ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_millis(42_500));
    assert!((42..=43).contains(&super::request_timeout_secs(&control)));
    let expired = ExecutionControl::default().with_deadline(std::time::Instant::now());
    assert_eq!(super::request_timeout_secs(&expired), 1);
}

/// A rejected fsync or a broken stream stays `io`; only an unanswered request
/// is a `timeout`.
#[test]
fn real_io_failures_stay_io() {
    let rejected = super::flush_error(russh_sftp::client::error::Error::Status(Status {
        id: 1,
        status_code: StatusCode::Failure,
        error_message: String::new(),
        language_tag: String::new(),
    }));
    assert_eq!(rejected.category, ErrorCategory::Io);
    assert_eq!(rejected.remote_effect, RemoteEffect::Unknown);
    let broken = super::stream_error(
        &std::io::Error::from(std::io::ErrorKind::BrokenPipe),
        ErrorPhase::Write,
        true,
    );
    assert_eq!(broken.category, ErrorCategory::Io);
    let timed_out = super::stream_error(
        &std::io::Error::from(std::io::ErrorKind::TimedOut),
        ErrorPhase::Write,
        true,
    );
    assert_eq!(timed_out.category, ErrorCategory::Timeout);
    assert_eq!(timed_out.retry, RetryDisposition::RequiresRecovery);
}
