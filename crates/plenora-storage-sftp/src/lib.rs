//! SFTP adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::SystemTime,
};

use async_trait::async_trait;
use plenora_storage_core::{
    ArtifactMetadata, CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory,
    ErrorPhase, GetRequest, IntegrityMetadata, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StatRequest, StorageError,
    StorageProvider, StorageResult, TestResult, TransferResult, validate_network_target,
};
use russh::{
    client,
    keys::{HashAlg, PublicKey},
};
use russh_sftp::{
    client::{SftpSession, error::Error as SftpError, fs::Metadata},
    protocol::{OpenFlags, StatusCode},
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

    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<SftpConnection> {
        let config = parse_config(connection)?;
        validate_root(&config.root)?;
        validate_network_target(
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
            (config.host.as_str(), config.port),
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
        Ok(SftpConnection {
            _ssh: ssh,
            sftp,
            root: config.root,
        })
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
    _ssh: client::Handle<SshClient>,
    sftp: SftpSession,
    root: String,
}

#[async_trait]
impl StorageProvider for SftpProvider {
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
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let mut stack = vec![remote.root.clone()];
        let mut objects = Vec::new();
        while let Some(directory) = stack.pop() {
            let entries = context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .read_dir(&directory)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            for entry in entries {
                let metadata = entry.metadata();
                if metadata.file_type().is_dir() {
                    stack.push(entry.path());
                } else if metadata.file_type().is_file() {
                    let key = relative_key(&remote.root, &entry.path())?;
                    if request
                        .prefix
                        .as_ref()
                        .is_none_or(|prefix| key.starts_with(prefix))
                        && request
                            .start_after
                            .as_ref()
                            .is_none_or(|offset| key > *offset)
                    {
                        objects.push(public_metadata(key, &metadata));
                        if objects.len() > context.policy.max_list_items {
                            return Err(list_scan_limit_error());
                        }
                    }
                }
            }
        }
        objects.sort_by(|left, right| left.key.cmp(&right.key));
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
        let atomic_publish =
            validate_sftp_publication(connection, request.overwrite, request.publication_policy)?;
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
        let destination_path = remote_path(&remote.root, &request.key);
        ensure_parent_directories(&remote.sftp, &destination_path, context).await?;
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
        let mut file = context
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
                                conflict_error("SFTP_CREATE_CONFLICT")
                            }
                        })
                },
                ErrorPhase::Prepare,
                true,
            )
            .await?;
        let transfer = copy_with_control(source, &mut file, context, true).await;
        let (bytes_transferred, digest) = match transfer {
            Ok(result) => result,
            Err(error) => {
                if atomic_publish {
                    let _ = remote.sftp.remove_file(&write_path).await;
                }
                return Err(error);
            }
        };
        if request
            .content_length
            .is_some_and(|expected| expected != bytes_transferred)
        {
            if atomic_publish {
                let _ = remote.sftp.remove_file(&write_path).await;
            }
            return Err(StorageError::new(
                ErrorCategory::InvalidConfiguration,
                ErrorPhase::Commit,
                RemoteEffect::Partial,
                RetryDisposition::RequiresRecovery,
                "CONTENT_LENGTH_MISMATCH",
                "artifact length differs from declared content_length",
            )
            .with_provider(PROVIDER_ID));
        }
        context
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
            .await?;
        if atomic_publish {
            context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .rename(&write_path, &destination_path)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await?;
        }
        Ok(transfer_result(
            request.key.clone(),
            bytes_transferred,
            digest,
        ))
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
        let atomic_publish =
            validate_sftp_publication(connection, request.overwrite, request.publication_policy)?;
        validate_key(&request.source_key)?;
        validate_key(&request.destination_key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let source_path = remote_path(&remote.root, &request.source_key);
        let destination_path = remote_path(&remote.root, &request.destination_key);
        ensure_parent_directories(&remote.sftp, &destination_path, context).await?;
        let mut source = remote
            .sftp
            .open(source_path)
            .await
            .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?;
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
        let mut destination = remote
            .sftp
            .open_with_flags(write_path.clone(), flags)
            .await
            .map_err(|error| {
                if request.overwrite {
                    map_sftp_error(error, ErrorPhase::Prepare, true)
                } else {
                    conflict_error("SFTP_COPY_CONFLICT")
                }
            })?;
        copy_with_control(&mut source, &mut destination, context, true).await?;
        context
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
            .await?;
        if atomic_publish {
            context
                .control
                .run(
                    async {
                        remote
                            .sftp
                            .rename(&write_path, &destination_path)
                            .await
                            .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await?;
        }
        let metadata = remote
            .sftp
            .metadata(destination_path)
            .await
            .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))?;
        Ok(public_metadata(request.destination_key.clone(), &metadata))
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<SftpConnectionConfig> {
    serde_json::from_value(connection.config.clone()).map_err(|_| {
        StorageError::invalid_configuration(
            "SFTP_CONFIGURATION_INVALID",
            "SFTP configuration does not match its public contract",
        )
        .with_provider(PROVIDER_ID)
    })
}

fn validate_root(root: &str) -> StorageResult<()> {
    if root.is_empty()
        || root.len() > 4_096
        || root.contains('\\')
        || root.contains('\0')
        || root.split('/').any(|part| part == "..")
    {
        return Err(StorageError::invalid_configuration(
            "SFTP_ROOT_INVALID",
            "SFTP root path is invalid",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

fn validate_key(key: &str) -> StorageResult<()> {
    if key.is_empty()
        || key.len() > 4_096
        || key.starts_with('/')
        || key.contains('\\')
        || key.contains('\0')
        || key
            .split('/')
            .any(|part| part.is_empty() || matches!(part, "." | ".."))
    {
        return Err(StorageError::invalid_configuration(
            "OBJECT_KEY_INVALID",
            "storage object key must be a normalized relative path",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

fn validate_prefix(prefix: &str) -> StorageResult<()> {
    if prefix.len() > 4_096
        || prefix.starts_with('/')
        || prefix.contains('\\')
        || prefix.contains('\0')
        || prefix.split('/').any(|part| part == "..")
    {
        return Err(StorageError::invalid_configuration(
            "OBJECT_PREFIX_INVALID",
            "storage object prefix is invalid",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
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

fn temporary_path(destination: &str) -> String {
    let nonce = TEMPORARY_NAME_NONCE.fetch_add(1, Ordering::Relaxed);
    format!("{destination}.plenora-tmp-{}-{nonce}", std::process::id())
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

async fn ensure_parent_directories(
    sftp: &SftpSession,
    path: &str,
    context: &OperationContext<'_>,
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
        "SFTP listing exceeds the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}

#[cfg(test)]
mod tests {
    use super::{remote_path, validate_key};

    #[test]
    fn keys_cannot_escape_the_remote_root() {
        assert!(validate_key("folder/object.bin").is_ok());
        assert!(validate_key("../secret").is_err());
        assert!(validate_key("/absolute").is_err());
        assert_eq!(remote_path("upload", "a/b"), "upload/a/b");
    }
}
