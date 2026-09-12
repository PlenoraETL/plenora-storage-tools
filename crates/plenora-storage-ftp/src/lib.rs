//! FTP adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

use std::{
    collections::BTreeMap,
    sync::Arc,
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
use serde::Deserialize;
use sha2::{Digest, Sha256};
use suppaftp::{
    FtpError, Mode, Status,
    list::{File, ListParser},
    tokio::AsyncFtpStream,
    types::FileType,
};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{
    AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader,
};

pub const PROVIDER_ID: &str = "ftp";
pub const CONFIG_CONTRACT: &str = "plenora-storage-ftp-connection-v1";

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FtpMode {
    #[default]
    Passive,
    ExtendedPassive,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FtpConnectionConfig {
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_root")]
    pub root: String,
    #[serde(default)]
    pub mode: FtpMode,
}

const fn default_port() -> u16 {
    21
}

fn default_root() -> String {
    ".".to_owned()
}

pub struct FtpProvider {
    credentials: Arc<dyn CredentialResolver>,
}

impl FtpProvider {
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self { credentials }
    }

    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<FtpConnection> {
        let config = parse_config(connection)?;
        if !context.policy.allow_insecure_ftp {
            return Err(StorageError::invalid_configuration(
                "INSECURE_FTP_FORBIDDEN",
                "plain FTP requires explicit engine authorization",
            )
            .with_provider(PROVIDER_ID));
        }
        // The control connection dials these addresses, never the host name
        // again: a second resolution could reach an address the policy just
        // rejected.
        let addresses = resolve_network_target(
            &config.host,
            config.port,
            context.policy.allow_private_network,
        )
        .await
        .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let credential = self
            .credentials
            .resolve(&connection.credential_ref)
            .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let username = credential.required("username")?.to_owned();
        let password = credential.required("password")?.to_owned();
        let mut ftp = AsyncFtpStream::connect(addresses.as_slice())
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        ftp.login(username, password)
            .await
            .map_err(map_ftp_auth_error)?;
        ftp.set_mode(match config.mode {
            FtpMode::Passive => Mode::Passive,
            FtpMode::ExtendedPassive => Mode::ExtendedPassive,
        });
        // A PASV reply carries a server-chosen address. Without this, the data
        // channel would be dialled at an address the network policy never saw,
        // which reopens on the data connection the bypass the control
        // connection just closed. Only the port from the reply is used.
        ftp.set_passive_nat_workaround(true);
        ftp.transfer_type(FileType::Binary)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, false))?;
        ftp.cwd(&config.root)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        Ok(FtpConnection { ftp })
    }
}

struct FtpConnection {
    ftp: AsyncFtpStream,
}

#[async_trait]
impl StorageProvider for FtpProvider {
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
                ("api".to_owned(), "ftp".to_owned()),
                ("transport_security".to_owned(), "none-opt-in".to_owned()),
                ("authentication".to_owned(), "password".to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "true".to_owned()),
                ("put_create_if_absent_atomic".to_owned(), "false".to_owned()),
                (
                    "copy_create_if_absent_atomic".to_owned(),
                    "false".to_owned(),
                ),
                ("overwrite_false".to_owned(), "rejected".to_owned()),
                ("atomic_publication".to_owned(), "false".to_owned()),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let mut remote = context
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
                        .ftp
                        .noop()
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Probe, false))?;
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
        let mut remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let prefix = request.prefix.as_deref().unwrap_or_default();
        let mut stack = vec![".".to_owned()];
        // Only the smallest `limit + 1` matching keys are retained, so page size
        // bounds memory instead of the whole matching namespace.
        let mut selected = BTreeMap::new();
        let mut scanned = 0_usize;
        while let Some(directory) = stack.pop() {
            scan_directory(&mut remote.ftp, &directory, context, &mut scanned, |file| {
                if matches!(file.name(), "." | "..") {
                    return Ok(());
                }
                let key = if directory == "." {
                    file.name().to_owned()
                } else {
                    format!("{directory}/{}", file.name())
                };
                // A server-supplied name that cannot be a public key must not be
                // published: it would break the output contract and produce a
                // cursor the next page rejects.
                if validate_key(&key).is_err() {
                    return Err(list_name_error());
                }
                if file.is_directory() {
                    if directory_may_contain(&key, prefix) {
                        stack.push(key);
                    }
                } else if file.is_file()
                    && key_matches_prefix(&key, prefix)
                    && request
                        .start_after
                        .as_ref()
                        .is_none_or(|offset| key > *offset)
                {
                    selected.insert(key.clone(), public_metadata(key, &file));
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
        let mut remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        stat_file(&mut remote.ftp, &request.key, context).await
    }

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        validate_key(&request.key)?;
        let mut remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let mut stream = context
            .control
            .run(
                async {
                    remote
                        .ftp
                        .retr_as_stream(&request.key)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        // Once bytes are offered to the caller-owned sink, failures can leave
        // an externally visible partial artifact.
        let (bytes_transferred, digest) =
            copy_with_control(&mut stream, sink, context, true).await?;
        context
            .control
            .run(
                async {
                    remote
                        .ftp
                        .finalize_retr_stream(stream)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
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
        validate_ftp_publication(request.overwrite, request.publication_policy)?;
        validate_key(&request.key)?;
        validate_file_metadata(request)?;
        let mut remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        ensure_parent_directories(&mut remote.ftp, &request.key, context).await?;
        let mut stream = context
            .control
            .run(
                async {
                    remote
                        .ftp
                        .put_with_stream(&request.key)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))
                },
                ErrorPhase::Prepare,
                true,
            )
            .await?;
        let transfer = copy_with_control(source, &mut stream, context, true).await;
        let (bytes_transferred, digest) = match transfer {
            Ok(result) => result,
            Err(error) => {
                abort_transfer(&mut remote.ftp, stream).await;
                return Err(error);
            }
        };
        if request
            .content_length
            .is_some_and(|expected| expected != bytes_transferred)
        {
            abort_transfer(&mut remote.ftp, stream).await;
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
                    remote
                        .ftp
                        .finalize_put_stream(stream)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
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
        let mut remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        // Absence is proved before the mutation. FTP reports "not found" and
        // several other unavailability conditions with the same 550 status, so
        // a failed `rm` cannot be read as "the object was already missing".
        let exists = context
            .control
            .run(
                ftp_object_exists(&mut remote.ftp, &request.key, context),
                ErrorPhase::Probe,
                false,
            )
            .await?;
        if !exists {
            return if request.ignore_missing {
                Ok(DeleteResult {
                    key: request.key.clone(),
                    deleted: false,
                })
            } else {
                Err(StorageError::new(
                    ErrorCategory::NotFound,
                    ErrorPhase::Probe,
                    RemoteEffect::None,
                    RetryDisposition::Never,
                    "FTP_OBJECT_NOT_FOUND",
                    "FTP object was not found",
                )
                .with_provider(PROVIDER_ID))
            };
        }
        context
            .control
            .run(
                async {
                    remote
                        .ftp
                        .rm(&request.key)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
        Ok(DeleteResult {
            key: request.key.clone(),
            deleted: true,
        })
    }

    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        validate_ftp_publication(request.overwrite, request.publication_policy)?;
        validate_key(&request.source_key)?;
        validate_key(&request.destination_key)?;
        // A self-copy would open the destination for writing while the source is
        // still being read, destroying the object being copied.
        if request.source_key == request.destination_key {
            return Err(StorageError::invalid_configuration(
                "COPY_TARGET_EQUALS_SOURCE",
                "copy source and destination must differ",
            )
            .with_provider(PROVIDER_ID));
        }
        let mut source_remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let mut destination_remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        ensure_parent_directories(
            &mut destination_remote.ftp,
            &request.destination_key,
            context,
        )
        .await?;
        let mut source = context
            .control
            .run(
                async {
                    source_remote
                        .ftp
                        .retr_as_stream(&request.source_key)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        let opened = context
            .control
            .run(
                async {
                    destination_remote
                        .ftp
                        .put_with_stream(&request.destination_key)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))
                },
                ErrorPhase::Prepare,
                true,
            )
            .await;
        let mut destination = match opened {
            Ok(destination) => destination,
            Err(error) => {
                abort_transfer(&mut source_remote.ftp, source).await;
                return Err(error);
            }
        };
        if let Err(error) = copy_with_control(&mut source, &mut destination, context, true).await {
            abort_transfer(&mut destination_remote.ftp, destination).await;
            abort_transfer(&mut source_remote.ftp, source).await;
            return Err(error);
        }
        if let Err(error) = context
            .control
            .run(
                async {
                    destination_remote
                        .ftp
                        .finalize_put_stream(destination)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await
        {
            abort_transfer(&mut source_remote.ftp, source).await;
            return Err(error);
        }
        // The destination is published from here on. Draining the source
        // transfer and reading the published metadata can still fail, but never
        // without a remote effect.
        context
            .control
            .run(
                async {
                    source_remote
                        .ftp
                        .finalize_retr_stream(source)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Cleanup, false))
                },
                ErrorPhase::Cleanup,
                true,
            )
            .await
            .map_err(|_| committed_verification_error())?;
        stat_file(
            &mut destination_remote.ftp,
            &request.destination_key,
            context,
        )
        .await
        .map_err(|_| committed_verification_error())
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<FtpConnectionConfig> {
    let config: FtpConnectionConfig =
        serde_json::from_value(connection.config.clone()).map_err(|_| configuration_error())?;
    // Serde alone accepts values that `plenora-storage-ftp-connection-v1`
    // rejects, so the schema bounds are enforced here as well.
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    let valid = (1..=253).contains(&config.host.chars().count())
        && !config.host.contains(char::is_whitespace)
        && !config.host.contains('\0')
        && config.port > 0
        && is_valid_root(&config.root);
    if valid {
        Ok(config)
    } else {
        Err(configuration_error())
    }
}

fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "FTP_CONFIGURATION_INVALID",
        "FTP configuration does not match its public contract",
    )
    .with_provider(PROVIDER_ID)
}

fn is_valid_root(root: &str) -> bool {
    (1..=4_096).contains(&root.chars().count())
        && !root.contains('\\')
        && !root.contains('\0')
        && !root.split('/').any(|part| part == "..")
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
            "FTP does not preserve object content type or custom metadata",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

fn validate_ftp_publication(
    overwrite: bool,
    publication_policy: PublicationPolicy,
) -> StorageResult<()> {
    if !overwrite {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_CREATE_IF_ABSENT_UNSUPPORTED",
            "FTP cannot guarantee atomic create-if-absent and rejects overwrite=false",
        )
        .with_provider(PROVIDER_ID));
    }
    if publication_policy == PublicationPolicy::AtomicRequired {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_ATOMIC_PUBLICATION_UNSUPPORTED",
            "FTP cannot guarantee atomic publication",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

async fn ensure_parent_directories(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
) -> StorageResult<()> {
    let Some((parent, _)) = key.rsplit_once('/') else {
        return Ok(());
    };
    let mut current = String::new();
    for part in parent.split('/') {
        if !current.is_empty() {
            current.push('/');
        }
        current.push_str(part);
        if !ftp_directory_exists(ftp, &current).await? {
            context
                .control
                .run(
                    async {
                        ftp.mkdir(&current)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))
                    },
                    ErrorPhase::Prepare,
                    true,
                )
                .await?;
        }
    }
    Ok(())
}

/// Weak existence probe used only to decide whether a parent directory still
/// has to be created. A wrong answer here is self-correcting: the following
/// `MKD` fails and the error is reported.
async fn ftp_directory_exists(ftp: &mut AsyncFtpStream, path: &str) -> StorageResult<bool> {
    match ftp.mlst(Some(path)).await {
        Ok(_) => Ok(true),
        Err(error) if is_file_unavailable(&error) => Ok(false),
        Err(error) => Err(map_ftp_error(error, ErrorPhase::Probe, false)),
    }
}

/// Proves an object is absent by listing its parent directory.
///
/// FTP reports "not found", "permission denied" and other unavailability with
/// the same 550 status, so a failed `MLST` on the object cannot be read as
/// absence. A parent that cannot be listed is an error, never an absence.
async fn ftp_object_exists(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
) -> StorageResult<bool> {
    let (parent, name) = key.rsplit_once('/').unwrap_or((".", key));
    let mut found = false;
    scan_directory(ftp, parent, context, &mut 0, |file| {
        if file.name() == name {
            found = file.is_file();
        }
        Ok(())
    })
    .await?;
    Ok(found)
}

// Includes MLSD facts and a UTF-8 name. Bounding a single line also stops a
// server that never sends a newline from growing the buffer without limit.
const MAX_MLSD_LINE_BYTES: u64 = 32 * 1_024;

async fn read_listing_line<R: AsyncBufRead + Unpin>(
    reader: &mut R,
) -> StorageResult<Option<String>> {
    let mut bytes = Vec::new();
    let read = reader
        .take(MAX_MLSD_LINE_BYTES + 1)
        .read_until(b'\n', &mut bytes)
        .await
        .map_err(|_| transfer_io_error(ErrorPhase::Read, false))?;
    if read == 0 {
        return Ok(None);
    }
    if read as u64 > MAX_MLSD_LINE_BYTES {
        return Err(list_scan_limit_error());
    }
    if bytes.last() == Some(&b'\n') {
        bytes.pop();
    }
    if bytes.last() == Some(&b'\r') {
        bytes.pop();
    }
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| list_parse_error())
}

async fn scan_directory<F>(
    ftp: &mut AsyncFtpStream,
    directory: &str,
    context: &OperationContext<'_>,
    scanned: &mut usize,
    mut visit: F,
) -> StorageResult<()>
where
    F: FnMut(File) -> StorageResult<()> + Send,
{
    context
        .control
        .run(
            async {
                let (_, stream) = ftp
                    .custom_data_command(
                        format!("MLSD {directory}"),
                        &[Status::AboutToSend, Status::AlreadyOpen],
                    )
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))?;
                let mut reader = BufReader::new(stream);
                while let Some(line) = read_listing_line(&mut reader).await? {
                    if *scanned >= context.policy.max_list_items {
                        return Err(list_scan_limit_error());
                    }
                    *scanned += 1;
                    let file = ListParser::parse_mlsd(&line).map_err(|_| list_parse_error())?;
                    visit(file)?;
                }
                ftp.close_data_connection(reader.into_inner())
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await
}

async fn stat_file(
    ftp: &mut AsyncFtpStream,
    key: &str,
    context: &OperationContext<'_>,
) -> StorageResult<ObjectMetadata> {
    let line = context
        .control
        .run(
            async {
                ftp.mlst(Some(key))
                    .await
                    .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
            },
            ErrorPhase::Read,
            false,
        )
        .await?;
    let file = ListParser::parse_mlst(&line).map_err(|_| {
        StorageError::new(
            ErrorCategory::Protocol,
            ErrorPhase::Read,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_STAT_PARSE_FAILED",
            "FTP MLST response is invalid",
        )
        .with_provider(PROVIDER_ID)
    })?;
    Ok(public_metadata(key.to_owned(), &file))
}

/// Independent budget for tearing down a data transfer after a failure.
///
/// The caller's deadline may already have expired, and `ABOR` plus its replies
/// must not keep a cancelled operation running without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

/// Tears down an open data transfer.
///
/// `ABOR` only closes the data connection: FTP does not promise the partially
/// written object is removed, so the remote outcome the caller already reported
/// stays ambiguous and is left untouched.
async fn abort_transfer<S>(ftp: &mut AsyncFtpStream, stream: S)
where
    S: AsyncRead + AsyncWrite + Send + Unpin + 'static,
{
    let _ = tokio::time::timeout(CLEANUP_BUDGET, ftp.abort(stream)).await;
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

fn public_metadata(key: String, file: &File) -> ObjectMetadata {
    ObjectMetadata {
        key,
        size: u64::try_from(file.size()).unwrap_or(u64::MAX),
        last_modified: format_system_time(file.modified()),
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

fn is_file_unavailable(error: &FtpError) -> bool {
    matches!(
        error,
        FtpError::UnexpectedResponse(response) if response.status == Status::FileUnavailable
    )
}

fn map_ftp_auth_error(_error: FtpError) -> StorageError {
    StorageError::new(
        ErrorCategory::Authentication,
        ErrorPhase::Connect,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_AUTHENTICATION_FAILED",
        "FTP server rejected the credentials",
    )
    .with_provider(PROVIDER_ID)
}

fn map_ftp_error(error: FtpError, phase: ErrorPhase, mutating: bool) -> StorageError {
    let (category, effect, retry, code, message) = match error {
        // Status 550 covers "not found" and several other unavailability
        // conditions. Absence may only be claimed when no mutation was started;
        // otherwise the remote outcome is genuinely ambiguous.
        FtpError::UnexpectedResponse(response)
            if response.status == Status::FileUnavailable && !mutating =>
        {
            (
                ErrorCategory::NotFound,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_OBJECT_NOT_FOUND",
                "FTP object was not found or unavailable",
            )
        }
        FtpError::UnexpectedResponse(response)
            if matches!(
                response.status,
                Status::NotLoggedIn | Status::InvalidCredentials
            ) =>
        {
            (
                ErrorCategory::Authentication,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_AUTHENTICATION_FAILED",
                "FTP server rejected the credentials",
            )
        }
        FtpError::UnexpectedResponse(response) if response.status == Status::ExceededStorage => (
            ErrorCategory::ResourceLimit,
            if mutating {
                RemoteEffect::Unknown
            } else {
                RemoteEffect::None
            },
            if mutating {
                RetryDisposition::RequiresRecovery
            } else {
                RetryDisposition::Never
            },
            "FTP_STORAGE_LIMIT_EXCEEDED",
            "FTP server storage limit was exceeded",
        ),
        FtpError::UnexpectedResponse(response)
            if matches!(
                response.status,
                Status::NotImplemented | Status::NotImplementedParameter
            ) =>
        {
            (
                ErrorCategory::Unsupported,
                RemoteEffect::None,
                RetryDisposition::Never,
                "FTP_OPERATION_UNSUPPORTED",
                "FTP server does not support the operation",
            )
        }
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "FTP_MUTATION_FAILED",
            "FTP mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "FTP_REQUEST_FAILED",
            "FTP request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
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
        "FTP_TRANSFER_IO_FAILED",
        "FTP transfer stream failed",
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
        "FTP listing visited more directory entries than the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}

fn list_parse_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_LIST_PARSE_FAILED",
        "FTP MLSD response is invalid",
    )
    .with_provider(PROVIDER_ID)
}

fn list_name_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "FTP_LIST_NAME_UNREPRESENTABLE",
        "FTP listing returned a name that is not a valid public object key",
    )
    .with_provider(PROVIDER_ID)
}

fn committed_verification_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::RequiresRecovery,
        "FTP_COMMITTED_METADATA_UNAVAILABLE",
        "FTP destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

#[cfg(test)]
mod tests {
    use super::{MAX_MLSD_LINE_BYTES, read_listing_line, scan_directory, validate_key};
    use plenora_storage_core::{EngineConfig, ExecutionControl, OperationContext};
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    #[test]
    fn keys_cannot_escape_the_remote_root() {
        assert!(validate_key("folder/object.bin").is_ok());
        assert!(validate_key("../secret").is_err());
        assert!(validate_key("/absolute").is_err());
    }

    #[tokio::test]
    async fn listing_line_without_a_terminator_has_a_fixed_memory_bound() {
        let mut reader = BufReader::new(tokio::io::repeat(b'x'));
        let error = read_listing_line(&mut reader)
            .await
            .expect_err("oversized line");
        assert_eq!(error.code, "LIST_SCAN_LIMIT_EXCEEDED");
        assert_eq!(MAX_MLSD_LINE_BYTES, 32 * 1_024);
    }

    #[tokio::test]
    async fn listing_stops_at_scan_limit_without_waiting_for_directory_eof() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("control listener");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            let (control, _) = listener.accept().await.expect("control");
            let mut control = BufReader::new(control);
            control
                .get_mut()
                .write_all(b"220 ready\r\n")
                .await
                .expect("greeting");
            let mut command = String::new();
            control.read_line(&mut command).await.expect("PASV");
            assert_eq!(command, "PASV\r\n");
            let data = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("data listener");
            let port = data.local_addr().expect("data address").port();
            control
                .get_mut()
                .write_all(
                    format!("227 passive (127,0,0,1,{},{})\r\n", port / 256, port % 256).as_bytes(),
                )
                .await
                .expect("passive reply");
            let (mut data, _) = data.accept().await.expect("data");
            command.clear();
            control.read_line(&mut command).await.expect("MLSD");
            assert_eq!(command, "MLSD .\r\n");
            control
                .get_mut()
                .write_all(b"150 listing\r\n")
                .await
                .expect("listing reply");
            data.write_all(
                b"type=file;size=1; a\r\ntype=file;size=1; b\r\ntype=file;size=1; c\r\n",
            )
            .await
            .expect("entries");
            std::future::pending::<()>().await;
        });
        let mut ftp = suppaftp::tokio::AsyncFtpStream::connect(address)
            .await
            .expect("connect");
        let policy = EngineConfig {
            max_list_items: 2,
            ..EngineConfig::default()
        };
        let control = ExecutionControl::default()
            .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
        let mut visited = Vec::new();
        let error = scan_directory(
            &mut ftp,
            ".",
            &OperationContext {
                policy: &policy,
                control: &control,
            },
            &mut 0,
            |file| {
                visited.push(file.name().to_owned());
                Ok(())
            },
        )
        .await
        .expect_err("scan bound before EOF");
        server.abort();
        assert_eq!(error.code, "LIST_SCAN_LIMIT_EXCEEDED");
        assert_eq!(visited, ["a", "b"]);
    }
}
