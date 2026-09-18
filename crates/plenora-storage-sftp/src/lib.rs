//! SFTP adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime},
};

use async_trait::async_trait;
use plenora_storage_core::{
    ArtifactMetadata, CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory,
    ErrorPhase, GetRequest, IntegrityMetadata, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StatRequest, StorageError,
    StorageProvider, StorageResult, TestResult, TransferResult, directory_may_contain,
    key_matches_prefix, resolve_network_target, validate_object_key, validate_object_prefix,
};
use russh::{
    client,
    keys::{HashAlg, PublicKey},
};
use russh_sftp::{
    client::{RawSftpSession, SftpSession, error::Error as SftpError, fs::Metadata},
    protocol::{OpenFlags, Packet, StatusCode},
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const PROVIDER_ID: &str = "sftp";
pub const CONFIG_CONTRACT: &str = "plenora-storage-sftp-connection-v1";
static TEMPORARY_NAME_NONCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SftpConnectionConfig {
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_root")]
    pub root: String,
    #[serde(default)]
    pub host_key_sha256: Option<String>,
    #[serde(default)]
    pub atomic_rename: bool,
}

const fn default_port() -> u16 {
    22
}

fn default_root() -> String {
    ".".to_owned()
}

pub struct SftpProvider {
    credentials: Arc<dyn CredentialResolver>,
}

impl SftpProvider {
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self { credentials }
    }

    async fn connect_ssh(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<(client::Handle<SshClient>, String)> {
        let config = parse_config(connection)?;
        // The transport dials these addresses, never the host name again: a
        // second resolution could reach an address the policy just rejected.
        let addresses = resolve_network_target(
            &config.host,
            config.port,
            context.policy.allow_private_network,
        )
        .await
        .map_err(|error| error.with_provider(PROVIDER_ID))?;
        if config.host_key_sha256.is_none() && !context.policy.allow_unverified_ssh {
            return Err(StorageError::invalid_configuration(
                "SFTP_HOST_KEY_REQUIRED",
                "SFTP requires a pinned SHA-256 host key fingerprint",
            )
            .with_provider(PROVIDER_ID));
        }
        let credential = self
            .credentials
            .resolve(&connection.credential_ref)
            .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let username = credential.required("username")?.to_owned();
        let password = credential.required("password")?.to_owned();
        let handler = SshClient {
            expected_fingerprint: config.host_key_sha256.clone(),
            allow_unverified: context.policy.allow_unverified_ssh,
        };
        let mut ssh = client::connect(
            Arc::new(client::Config::default()),
            addresses.as_slice(),
            handler,
        )
        .await
        .map_err(map_ssh_connect_error)?;
        let authenticated = ssh
            .authenticate_password(username, password)
            .await
            .map_err(map_ssh_connect_error)?;
        if !authenticated.success() {
            return Err(StorageError::new(
                ErrorCategory::Authentication,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Never,
                "SFTP_AUTHENTICATION_FAILED",
                "SFTP server rejected the credentials",
            )
            .with_provider(PROVIDER_ID));
        }
        Ok((ssh, config.root))
    }

    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<SftpConnection> {
        let (ssh, root) = self.connect_ssh(connection, context).await?;
        let channel = ssh
            .channel_open_session()
            .await
            .map_err(map_ssh_connect_error)?;
        channel
            .request_subsystem(true, "sftp")
            .await
            .map_err(map_ssh_connect_error)?;
        let sftp = SftpSession::new(channel.into_stream())
            .await
            .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
        Ok(SftpConnection { ssh, sftp, root })
    }
}

struct SshClient {
    expected_fingerprint: Option<String>,
    allow_unverified: bool,
}

impl client::Handler for SshClient {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKey,
    ) -> Result<bool, Self::Error> {
        if self.allow_unverified {
            return Ok(true);
        }
        let actual = server_public_key.fingerprint(HashAlg::Sha256).to_string();
        Ok(self
            .expected_fingerprint
            .as_ref()
            .is_some_and(|expected| expected == &actual))
    }
}

struct SftpConnection {
    ssh: client::Handle<SshClient>,
    sftp: SftpSession,
    root: String,
}

impl SftpConnection {
    /// Negotiate the atomic replacement primitive before creating any directories
    /// or files. The high-level client only exposes the non-replacing v3 rename.
    async fn atomic_session(&self) -> StorageResult<RawSftpSession> {
        let channel = self
            .ssh
            .channel_open_session()
            .await
            .map_err(map_ssh_connect_error)?;
        channel
            .request_subsystem(true, "sftp")
            .await
            .map_err(map_ssh_connect_error)?;
        let session = RawSftpSession::new(channel.into_stream());
        qualify_atomic_session(&session).await?;
        Ok(session)
    }
}

async fn qualify_atomic_session(session: &RawSftpSession) -> StorageResult<()> {
    let version = session
        .init()
        .await
        .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
    if version
        .extensions
        .get("posix-rename@openssh.com")
        .map(String::as_str)
        != Some("1")
    {
        return Err(StorageError::unsupported(
            "SFTP atomic replacement requires posix-rename@openssh.com version 1",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

async fn atomic_replace(
    session: &RawSftpSession,
    source: &str,
    destination: &str,
) -> StorageResult<()> {
    let mut payload = Vec::new();
    for path in [source, destination] {
        let length = u32::try_from(path.len()).map_err(|_| configuration_error())?;
        payload.extend_from_slice(&length.to_be_bytes());
        payload.extend_from_slice(path.as_bytes());
    }
    let response = session
        .extended("posix-rename@openssh.com", payload)
        .await
        .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))?;
    match response {
        Packet::Status(status) if status.status_code == StatusCode::Ok => Ok(()),
        Packet::Status(status) => Err(map_sftp_error(status.into(), ErrorPhase::Commit, true)),
        _ => Err(map_sftp_error(
            SftpError::UnexpectedPacket,
            ErrorPhase::Commit,
            true,
        )),
    }
}

#[async_trait]
impl StorageProvider for SftpProvider {
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        policy: &plenora_storage_core::EngineConfig,
    ) -> StorageResult<()> {
        let config = parse_config(connection)?;
        if config.host_key_sha256.is_none() && !policy.allow_unverified_ssh {
            return Err(StorageError::invalid_configuration(
                "SFTP_HOST_KEY_REQUIRED",
                "SFTP requires a pinned SHA-256 host key fingerprint",
            )
            .with_provider(PROVIDER_ID));
        }
        Ok(())
    }

    fn id(&self) -> &'static str {
        PROVIDER_ID
    }

    fn config_contract(&self) -> &'static str {
        CONFIG_CONTRACT
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: PROVIDER_ID.to_owned(),
            config_contract: CONFIG_CONTRACT.to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .into_iter()
                .map(str::to_owned)
                .collect(),
            attributes: BTreeMap::from([
                ("api".to_owned(), "sftp-v3".to_owned()),
                ("authentication".to_owned(), "password".to_owned()),
                ("host_key_verification".to_owned(), "sha256-pin".to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "true".to_owned()),
                ("put_create_if_absent_atomic".to_owned(), "true".to_owned()),
                ("copy_create_if_absent_atomic".to_owned(), "true".to_owned()),
                (
                    "atomic_publication".to_owned(),
                    "qualified_by_connection".to_owned(),
                ),
                (
                    "atomic_required".to_owned(),
                    "overwrite_true_only".to_owned(),
                ),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .canonicalize(&remote.root)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Probe, false))?;
                    Ok(TestResult {
                        provider: PROVIDER_ID.to_owned(),
                        reachable: true,
                    })
                },
                ErrorPhase::Probe,
                false,
            )
            .await
    }

    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
        let limit = list_limit(request, context)?;
        validate_prefix(request.prefix.as_deref().unwrap_or_default())?;
        if let Some(start_after) = request.start_after.as_deref() {
            validate_key(start_after)?;
        }
        let prefix = request.prefix.as_deref().unwrap_or_default();
        // The high-level read_dir collects every batch before returning. A
        // raw session lets us enforce the scan budget between READDIR responses.
        let (_ssh, listing, root) = context
            .control
            .run(
                async {
                    let (ssh, root) = self.connect_ssh(connection, context).await?;
                    let channel = ssh
                        .channel_open_session()
                        .await
                        .map_err(map_ssh_connect_error)?;
                    channel
                        .request_subsystem(true, "sftp")
                        .await
                        .map_err(map_ssh_connect_error)?;
                    let listing = RawSftpSession::new(channel.into_stream());
                    listing
                        .init()
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
                    Ok((ssh, listing, root))
                },
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let mut stack = vec![root.clone()];
        // Only the smallest `limit + 1` matching keys are retained, so page size
        // bounds memory instead of the whole matching namespace.
        let mut selected = BTreeMap::new();
        let mut scanned = 0_usize;
        while let Some(directory) = stack.pop() {
            scan_directory(&listing, &directory, context, &mut scanned, |entry| {
                let metadata = entry.attrs;
                validate_key(&entry.filename)?;
                let path = remote_path(&directory, &entry.filename);
                let key = relative_key(&root, &path)?;
                if metadata.file_type().is_dir() {
                    // Directories that cannot hold a matching key are not
                    // entered at all.
                    if directory_may_contain(&key, prefix) {
                        stack.push(path);
                    }
                } else if metadata.file_type().is_file()
                    && key_matches_prefix(&key, prefix)
                    && request
                        .start_after
                        .as_ref()
                        .is_none_or(|offset| key > *offset)
                {
                    selected.insert(key.clone(), public_metadata(key, &metadata));
                    if selected.len() > limit.saturating_add(1)
                        && let Some(highest) = selected.keys().next_back().cloned()
                    {
                        selected.remove(&highest);
                    }
                }
                Ok(())
            })
            .await?;
        }
        let mut objects = selected.into_values().collect::<Vec<_>>();
        let truncated = objects.len() > limit;
        if truncated {
            objects.truncate(limit);
        }
        let next_start_after = truncated
            .then(|| objects.last().map(|object| object.key.clone()))
            .flatten();
        Ok(ProviderListResult {
            objects,
            truncated,
            next_start_after,
        })
    }

    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let metadata = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .metadata(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        Ok(public_metadata(request.key.clone(), &metadata))
    }

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let mut file = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .open(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        // Once bytes are offered to the caller-owned sink, failures can leave
        // an externally visible partial artifact.
        let (bytes_transferred, digest) = copy_with_control(&mut file, sink, context, true).await?;
        Ok(transfer_result(
            request.key.clone(),
            bytes_transferred,
            digest,
        ))
    }

    async fn put(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let mut parent_effect = false;
        let result = async {
            if request
                .content_length
                .is_some_and(|length| length > context.policy.max_transfer_bytes)
            {
                return Err(transfer_limit_error()
                    .with_outcome(RemoteEffect::None, RetryDisposition::Never));
            }
            let atomic_publish = validate_sftp_publication(
                connection,
                request.overwrite,
                request.publication_policy,
            )?;
            validate_key(&request.key)?;
            validate_file_metadata(request)?;
            let remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let atomic = if atomic_publish {
                Some(
                    context
                        .control
                        .run(remote.atomic_session(), ErrorPhase::Connect, false)
                        .await?,
                )
            } else {
                None
            };
            let destination_path = remote_path(&remote.root, &request.key);
            ensure_parent_directories(&remote.sftp, &destination_path, context, &mut parent_effect)
                .await?;
            let write_path = if atomic_publish {
                temporary_path(&destination_path)
            } else {
                destination_path.clone()
            };
            let flags = if atomic_publish || !request.overwrite {
                OpenFlags::CREATE | OpenFlags::EXCLUDE | OpenFlags::WRITE
            } else {
                OpenFlags::CREATE | OpenFlags::TRUNCATE | OpenFlags::WRITE
            };
            let opened = context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .open_with_flags(write_path.clone(), flags)
                            .await
                            .map_err(|error| {
                                if request.overwrite {
                                    map_sftp_error(error, ErrorPhase::Prepare, true)
                                } else {
                                    map_exclusive_open_error(error, "SFTP_CREATE_CONFLICT")
                                }
                            })
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await;
            // Deliberately no cleanup on this path. An exclusive create can fail
            // precisely because the name is already held, and this operation cannot
            // prove it owns a file it did not open. Deleting it would be a
            // destructive guess; the error already reports an ambiguous outcome.
            let mut file = opened?;
            let transfer = copy_with_control(source, &mut file, context, true).await;
            let (bytes_transferred, digest) = match transfer {
                Ok(result) => result,
                Err(error) => {
                    return Err(discard_staged_object(
                        &remote.sftp,
                        atomic_publish,
                        &write_path,
                        error,
                    )
                    .await);
                }
            };
            if request
                .content_length
                .is_some_and(|expected| expected != bytes_transferred)
            {
                let error = StorageError::new(
                    ErrorCategory::InvalidConfiguration,
                    ErrorPhase::Commit,
                    RemoteEffect::Partial,
                    RetryDisposition::RequiresRecovery,
                    "CONTENT_LENGTH_MISMATCH",
                    "artifact length differs from declared content_length",
                )
                .with_provider(PROVIDER_ID);
                return Err(discard_staged_object(
                    &remote.sftp,
                    atomic_publish,
                    &write_path,
                    error,
                )
                .await);
            }
            if let Err(error) = context
                .control
                .run(
                    async {
                        file.sync_all()
                            .await
                            .map_err(|_| mutation_io_error(ErrorPhase::Commit))?;
                        file.shutdown()
                            .await
                            .map_err(|_| mutation_io_error(ErrorPhase::Commit))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await
            {
                return Err(discard_staged_object(
                    &remote.sftp,
                    atomic_publish,
                    &write_path,
                    error,
                )
                .await);
            }
            if let Some(session) = atomic.as_ref()
                && let Err(error) = context
                    .control
                    .run(
                        atomic_replace(session, &write_path, &destination_path),
                        ErrorPhase::Commit,
                        true,
                    )
                    .await
            {
                return Err(discard_staged_object(&remote.sftp, true, &write_path, error).await);
            }
            Ok(transfer_result(
                request.key.clone(),
                bytes_transferred,
                digest,
            ))
        }
        .await;
        result.map_err(|error: StorageError| error.with_preparation_effect(parent_effect))
    }

    async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let result = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .remove_file(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await;
        match result {
            Ok(()) => Ok(DeleteResult {
                key: request.key.clone(),
                deleted: true,
            }),
            Err(error) if request.ignore_missing && error.category == ErrorCategory::NotFound => {
                Ok(DeleteResult {
                    key: request.key.clone(),
                    deleted: false,
                })
            }
            Err(error) => Err(error),
        }
    }

    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let mut parent_effect = false;
        let result = async {
            let atomic_publish = validate_sftp_publication(
                connection,
                request.overwrite,
                request.publication_policy,
            )?;
            validate_key(&request.source_key)?;
            validate_key(&request.destination_key)?;
            // A self-copy would open the destination for truncation while the
            // source is still open, destroying the object being copied.
            if request.source_key == request.destination_key {
                return Err(StorageError::invalid_configuration(
                    "COPY_TARGET_EQUALS_SOURCE",
                    "copy source and destination must differ",
                )
                .with_provider(PROVIDER_ID));
            }
            let remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let atomic = if atomic_publish {
                Some(
                    context
                        .control
                        .run(remote.atomic_session(), ErrorPhase::Connect, false)
                        .await?,
                )
            } else {
                None
            };
            let source_path = remote_path(&remote.root, &request.source_key);
            let destination_path = remote_path(&remote.root, &request.destination_key);
            ensure_parent_directories(&remote.sftp, &destination_path, context, &mut parent_effect)
                .await?;
            let mut source = context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .open(source_path)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            let write_path = if atomic_publish {
                temporary_path(&destination_path)
            } else {
                destination_path.clone()
            };
            let flags = if atomic_publish || !request.overwrite {
                OpenFlags::CREATE | OpenFlags::EXCLUDE | OpenFlags::WRITE
            } else {
                OpenFlags::CREATE | OpenFlags::TRUNCATE | OpenFlags::WRITE
            };
            let opened = context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .open_with_flags(write_path.clone(), flags)
                            .await
                            .map_err(|error| {
                                if request.overwrite {
                                    map_sftp_error(error, ErrorPhase::Prepare, true)
                                } else {
                                    map_exclusive_open_error(error, "SFTP_COPY_CONFLICT")
                                }
                            })
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await;
            // Deliberately no cleanup on this path, for the same reason as `put`:
            // the operation cannot prove it owns a file it did not open.
            let mut destination = opened?;
            if let Err(error) =
                copy_with_control(&mut source, &mut destination, context, true).await
            {
                return Err(discard_staged_object(
                    &remote.sftp,
                    atomic_publish,
                    &write_path,
                    error,
                )
                .await);
            }
            if let Err(error) = context
                .control
                .run(
                    async {
                        destination
                            .sync_all()
                            .await
                            .map_err(|_| mutation_io_error(ErrorPhase::Commit))?;
                        destination
                            .shutdown()
                            .await
                            .map_err(|_| mutation_io_error(ErrorPhase::Commit))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await
            {
                return Err(discard_staged_object(
                    &remote.sftp,
                    atomic_publish,
                    &write_path,
                    error,
                )
                .await);
            }
            if let Some(session) = atomic.as_ref()
                && let Err(error) = context
                    .control
                    .run(
                        atomic_replace(session, &write_path, &destination_path),
                        ErrorPhase::Commit,
                        true,
                    )
                    .await
            {
                return Err(discard_staged_object(&remote.sftp, true, &write_path, error).await);
            }
            // The destination is published from here on: a failed read-back must
            // not be reported as an operation without a remote effect.
            let metadata = context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .metadata(destination_path)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Cleanup,
                    true,
                )
                .await
                .map_err(|_| committed_verification_error())?;
            Ok(public_metadata(request.destination_key.clone(), &metadata))
        }
        .await;
        result.map_err(|error: StorageError| error.with_preparation_effect(parent_effect))
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<SftpConnectionConfig> {
    let config: SftpConnectionConfig =
        serde_json::from_value(connection.config.clone()).map_err(|_| configuration_error())?;
    // Serde alone accepts values that `plenora-storage-sftp-connection-v1`
    // rejects, so the schema bounds are enforced here as well.
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    let valid = (1..=253).contains(&config.host.chars().count())
        && !config.host.contains(char::is_whitespace)
        && !config.host.contains('\0')
        && config.port > 0
        && is_valid_root(&config.root)
        && config
            .host_key_sha256
            .as_ref()
            .is_none_or(|value| is_ssh_sha256_fingerprint(value));
    if valid {
        Ok(config)
    } else {
        Err(configuration_error())
    }
}

fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "SFTP_CONFIGURATION_INVALID",
        "SFTP configuration does not match its public contract",
    )
    .with_provider(PROVIDER_ID)
}

fn is_valid_root(root: &str) -> bool {
    (1..=4_096).contains(&root.chars().count())
        && !root.contains('\\')
        && !root.contains('\0')
        && !root.split('/').any(|part| part == "..")
}

/// Matches the `host_key_sha256` pattern of the SFTP connection contract. It is
/// checked even when unverified host keys are authorized, so that relaxing the
/// policy cannot also relax the configuration contract.
fn is_ssh_sha256_fingerprint(value: &str) -> bool {
    let Some(encoded) = value.strip_prefix("SHA256:") else {
        return false;
    };
    let body = encoded.strip_suffix('=').unwrap_or(encoded);
    body.len() == 43
        && body
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/'))
}

fn validate_key(key: &str) -> StorageResult<()> {
    validate_object_key(key).map_err(|error| error.with_provider(PROVIDER_ID))
}

fn validate_prefix(prefix: &str) -> StorageResult<()> {
    validate_object_prefix(prefix).map_err(|error| error.with_provider(PROVIDER_ID))
}

fn validate_file_metadata(request: &PutRequest) -> StorageResult<()> {
    if request.content_type.is_some() || !request.metadata.is_empty() {
        return Err(StorageError::unsupported(
            "SFTP does not preserve object content type or custom metadata",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

fn validate_sftp_publication(
    connection: &ProviderConnection,
    overwrite: bool,
    publication_policy: PublicationPolicy,
) -> StorageResult<bool> {
    if publication_policy != PublicationPolicy::AtomicRequired {
        return Ok(false);
    }
    let config = parse_config(connection)?;
    if !config.atomic_rename || !overwrite {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_ATOMIC_PUBLICATION_UNAVAILABLE",
            "SFTP atomic publication requires a qualified atomic rename connection and overwrite=true",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(true)
}

/// Builds a staging name that no other client can collide with.
///
/// A process id and a local counter are not enough: two clients on different
/// machines can produce the same pair, and an exclusive create that loses that
/// race would report a conflict against a file it does not own.
fn temporary_path(destination: &str) -> String {
    let nonce = TEMPORARY_NAME_NONCE.fetch_add(1, Ordering::Relaxed);
    let elapsed = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |value| value.as_nanos());
    let mut digest = Sha256::new();
    digest.update(destination.as_bytes());
    digest.update(std::process::id().to_le_bytes());
    digest.update(nonce.to_le_bytes());
    digest.update(elapsed.to_le_bytes());
    let unique = format!("{:x}", digest.finalize());
    format!("{destination}.plenora-tmp-{}", &unique[..32])
}

fn remote_path(root: &str, key: &str) -> String {
    if root == "." {
        format!("./{key}")
    } else if root.ends_with('/') {
        format!("{root}{key}")
    } else {
        format!("{root}/{key}")
    }
}

fn relative_key(root: &str, path: &str) -> StorageResult<String> {
    let normalized_root = root.trim_end_matches('/');
    let key = path
        .strip_prefix(normalized_root)
        .unwrap_or(path)
        .trim_start_matches('/')
        .to_owned();
    validate_key(&key)?;
    Ok(key)
}

async fn scan_directory<F>(
    listing: &RawSftpSession,
    directory: &str,
    context: &OperationContext<'_>,
    scanned: &mut usize,
    mut visit: F,
) -> StorageResult<()>
where
    F: FnMut(russh_sftp::protocol::File) -> StorageResult<()> + Send,
{
    context
        .control
        .run(
            async {
                let handle = listing
                    .opendir(directory)
                    .await
                    .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?
                    .handle;
                loop {
                    let batch = match listing.readdir(&handle).await {
                        Ok(batch) => batch,
                        Err(SftpError::Status(status)) if status.status_code == StatusCode::Eof => {
                            break;
                        }
                        Err(error) => return Err(map_sftp_error(error, ErrorPhase::Read, false)),
                    };
                    for entry in batch.files {
                        if matches!(entry.filename.as_str(), "." | "..") {
                            continue;
                        }
                        if *scanned >= context.policy.max_list_items {
                            return Err(list_scan_limit_error());
                        }
                        *scanned += 1;
                        visit(entry)?;
                    }
                }
                listing
                    .close(handle)
                    .await
                    .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?;
                Ok(())
            },
            ErrorPhase::Read,
            false,
        )
        .await
}

async fn ensure_parent_directories(
    sftp: &SftpSession,
    path: &str,
    context: &OperationContext<'_>,
    parent_effect: &mut bool,
) -> StorageResult<()> {
    let Some((parent, _)) = path.rsplit_once('/') else {
        return Ok(());
    };
    let absolute = parent.starts_with('/');
    let mut current = if absolute {
        "/".to_owned()
    } else {
        String::new()
    };
    for part in parent
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
    {
        if !current.is_empty() && current != "/" {
            current.push('/');
        }
        current.push_str(part);
        let exists = context
            .control
            .run(
                async {
                    sftp.try_exists(&current)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Prepare, false))
                },
                ErrorPhase::Prepare,
                false,
            )
            .await?;
        if !exists {
            *parent_effect = true;
            context
                .control
                .run(
                    async {
                        sftp.create_dir(&current)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Prepare, true))
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await?;
        }
    }
    Ok(())
}

async fn copy_with_control<R, W>(
    source: &mut R,
    destination: &mut W,
    context: &OperationContext<'_>,
    mutating: bool,
) -> StorageResult<(u64, Sha256)>
where
    R: AsyncRead + Send + Unpin + ?Sized,
    W: AsyncWrite + Send + Unpin + ?Sized,
{
    let mut buffer = vec![0_u8; 64 * 1_024];
    let mut transferred = 0_u64;
    let mut digest = Sha256::new();
    loop {
        let read = context
            .control
            .run(
                async {
                    source
                        .read(&mut buffer)
                        .await
                        .map_err(|_| transfer_io_error(ErrorPhase::Read, mutating))
                },
                ErrorPhase::Read,
                mutating,
            )
            .await?;
        if read == 0 {
            break;
        }
        transferred = transferred
            .checked_add(read as u64)
            .filter(|total| *total <= context.policy.max_transfer_bytes)
            .ok_or_else(transfer_limit_error)?;
        digest.update(&buffer[..read]);
        context
            .control
            .run(
                async {
                    destination
                        .write_all(&buffer[..read])
                        .await
                        .map_err(|_| transfer_io_error(ErrorPhase::Write, mutating))
                },
                ErrorPhase::Write,
                mutating,
            )
            .await?;
    }
    Ok((transferred, digest))
}

fn list_limit(
    request: &ProviderListRequest,
    context: &OperationContext<'_>,
) -> StorageResult<usize> {
    let limit = request.max_items.unwrap_or(1_000);
    if limit == 0 || limit > context.policy.max_list_items {
        return Err(StorageError::new(
            ErrorCategory::ResourceLimit,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "LIST_LIMIT_INVALID",
            "list max_items is zero or exceeds the engine policy",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(limit)
}

fn public_metadata(key: String, metadata: &Metadata) -> ObjectMetadata {
    ObjectMetadata {
        key,
        size: metadata.len(),
        last_modified: metadata.modified().ok().and_then(format_system_time),
        etag: None,
        version: None,
    }
}

fn format_system_time(value: SystemTime) -> Option<String> {
    OffsetDateTime::from(value).format(&Rfc3339).ok()
}

fn transfer_result(key: String, bytes_transferred: u64, digest: Sha256) -> TransferResult {
    let checksum = IntegrityMetadata {
        algorithm: "sha256".to_owned(),
        value: format!("{:x}", digest.finalize()),
    };
    TransferResult {
        key,
        bytes_transferred,
        artifact: ArtifactMetadata {
            content_type: None,
            size: Some(bytes_transferred),
            sha256: Some(checksum.value.clone()),
        },
        checksum,
        etag: None,
        version: None,
    }
}

fn map_sftp_error(error: SftpError, phase: ErrorPhase, mutating: bool) -> StorageError {
    let (category, effect, retry, code, message) = match error {
        SftpError::Status(status) if status.status_code == StatusCode::NoSuchFile => (
            ErrorCategory::NotFound,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_OBJECT_NOT_FOUND",
            "SFTP object was not found",
        ),
        SftpError::Status(status) if status.status_code == StatusCode::PermissionDenied => (
            ErrorCategory::Authorization,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_AUTHORIZATION_FAILED",
            "SFTP server denied the operation",
        ),
        SftpError::Status(status) if status.status_code == StatusCode::OpUnsupported => (
            ErrorCategory::Unsupported,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_OPERATION_UNSUPPORTED",
            "SFTP server does not support the operation",
        ),
        SftpError::Timeout => (
            ErrorCategory::Timeout,
            if mutating {
                RemoteEffect::Unknown
            } else {
                RemoteEffect::None
            },
            if mutating {
                RetryDisposition::RequiresRecovery
            } else {
                RetryDisposition::Safe
            },
            "SFTP_TIMEOUT",
            "SFTP operation timed out",
        ),
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "SFTP_MUTATION_FAILED",
            "SFTP mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "SFTP_REQUEST_FAILED",
            "SFTP request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
}

fn map_ssh_connect_error(error: russh::Error) -> StorageError {
    let (category, retry, code, message) = match error {
        russh::Error::UnknownKey | russh::Error::KeyChanged { .. } => (
            ErrorCategory::Authentication,
            RetryDisposition::Never,
            "SFTP_HOST_KEY_REJECTED",
            "SFTP server host key was rejected",
        ),
        russh::Error::ConnectionTimeout
        | russh::Error::KeepaliveTimeout
        | russh::Error::InactivityTimeout => (
            ErrorCategory::Timeout,
            RetryDisposition::Safe,
            "SFTP_CONNECT_TIMEOUT",
            "SFTP connection timed out",
        ),
        _ => (
            ErrorCategory::Transient,
            RetryDisposition::Safe,
            "SFTP_CONNECT_FAILED",
            "SFTP connection failed",
        ),
    };
    StorageError::new(
        category,
        ErrorPhase::Connect,
        RemoteEffect::None,
        retry,
        code,
        message,
    )
    .with_provider(PROVIDER_ID)
}

fn transfer_io_error(phase: ErrorPhase, mutating: bool) -> StorageError {
    StorageError::new(
        ErrorCategory::Io,
        phase,
        if mutating {
            RemoteEffect::Unknown
        } else {
            RemoteEffect::None
        },
        if mutating {
            RetryDisposition::RequiresRecovery
        } else {
            RetryDisposition::Safe
        },
        "SFTP_TRANSFER_IO_FAILED",
        "SFTP transfer stream failed",
    )
    .with_provider(PROVIDER_ID)
}

fn mutation_io_error(phase: ErrorPhase) -> StorageError {
    transfer_io_error(phase, true)
}

/// Independent budget for cleanup after a failure.
///
/// The caller's deadline may already have expired, and a cleanup that hangs must
/// not extend the operation without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

/// Removes an unpublished staging object and restates the outcome.
///
/// A confirmed removal turns an ambiguous failure into a rolled back one; an
/// unconfirmed removal is reported rather than leaving a `.plenora-tmp-*` object
/// behind silently. The original cause is preserved either way.
async fn discard_staged_object(
    sftp: &SftpSession,
    staged: bool,
    write_path: &str,
    error: StorageError,
) -> StorageError {
    if !staged {
        return error;
    }
    match tokio::time::timeout(CLEANUP_BUDGET, sftp.remove_file(write_path)).await {
        Ok(Ok(())) => error.rolled_back(),
        _ => error.cleanup_unconfirmed("staging_remove_failed"),
    }
}

fn committed_verification_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::RequiresRecovery,
        "SFTP_COMMITTED_METADATA_UNAVAILABLE",
        "SFTP destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

/// Maps a failed exclusive create.
///
/// Only the generic failure status proves the destination already exists, which
/// is how SFTP v3 reports a rejected exclusive create. Permission, unsupported
/// and transport failures are reported as themselves, so a timeout raised after
/// the server created the file is never announced as a conflict without effect.
fn map_exclusive_open_error(error: SftpError, code: &'static str) -> StorageError {
    match &error {
        SftpError::Status(status) if status.status_code == StatusCode::Failure => {
            conflict_error(code)
        }
        _ => map_sftp_error(error, ErrorPhase::Prepare, true),
    }
}

fn conflict_error(code: &'static str) -> StorageError {
    StorageError::new(
        ErrorCategory::Conflict,
        ErrorPhase::Prepare,
        RemoteEffect::None,
        RetryDisposition::Never,
        code,
        "SFTP destination already exists or could not be created exclusively",
    )
    .with_provider(PROVIDER_ID)
}

fn transfer_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeds the engine byte limit",
    )
    .with_provider(PROVIDER_ID)
}

fn list_scan_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "LIST_SCAN_LIMIT_EXCEEDED",
        "SFTP listing visited more directory entries than the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}

#[cfg(test)]
mod tests {
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
    fn keys_cannot_escape_the_remote_root() {
        assert!(validate_key("folder/object.bin").is_ok());
        assert!(validate_key("../secret").is_err());
        assert!(validate_key("/absolute").is_err());
        assert_eq!(remote_path("upload", "a/b"), "upload/a/b");
    }

    struct EndlessDirectory(Arc<AtomicUsize>);

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
}
