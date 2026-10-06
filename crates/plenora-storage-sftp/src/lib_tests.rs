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
