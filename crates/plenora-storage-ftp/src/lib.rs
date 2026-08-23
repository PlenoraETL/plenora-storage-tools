//! FTP adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

use std::{collections::BTreeMap, sync::Arc, time::SystemTime};

use async_trait::async_trait;
use plenora_storage_core::{
    CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory, ErrorPhase,
    GetRequest, IntegrityMetadata, ListRequest, ListResult, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, PutRequest, RemoteEffect, RetryDisposition,
    StatRequest, StorageError, StorageProvider, StorageResult, TestResult, TransferResult,
    validate_network_target,
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
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

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
        validate_root(&config.root)?;
        if !context.policy.allow_insecure_ftp {
            return Err(StorageError::invalid_configuration(
                "INSECURE_FTP_FORBIDDEN",
                "plain FTP requires explicit engine authorization",
            )
            .with_provider(PROVIDER_ID));
        }
        validate_network_target(
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
        let mut ftp = AsyncFtpStream::connect((config.host.as_str(), config.port))
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        ftp.login(username, password)
            .await
            .map_err(map_ftp_auth_error)?;
        ftp.set_mode(match config.mode {
            FtpMode::Passive => Mode::Passive,
            FtpMode::ExtendedPassive => Mode::ExtendedPassive,
        });
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
                ("conditional_write".to_owned(), "false".to_owned()),
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
        request: &ListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ListResult> {
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
        let mut stack = vec![".".to_owned()];
        let mut objects = Vec::new();
        while let Some(directory) = stack.pop() {
            let lines = context
                .control
                .run(
                    async {
                        remote
                            .ftp
                            .mlsd(Some(&directory))
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            for line in lines {
                let file = ListParser::parse_mlsd(&line).map_err(|_| {
                    StorageError::new(
                        ErrorCategory::Protocol,
                        ErrorPhase::Read,
                        RemoteEffect::None,
                        RetryDisposition::Never,
                        "FTP_LIST_PARSE_FAILED",
                        "FTP MLSD response is invalid",
                    )
                    .with_provider(PROVIDER_ID)
                })?;
                if matches!(file.name(), "." | "..") {
                    continue;
                }
                let key = if directory == "." {
                    file.name().to_owned()
                } else {
                    format!("{directory}/{}", file.name())
                };
                if file.is_directory() {
                    stack.push(key);
                } else if file.is_file()
                    && request
                        .prefix
                        .as_ref()
                        .is_none_or(|prefix| key.starts_with(prefix))
                    && request
                        .start_after
                        .as_ref()
                        .is_none_or(|offset| key > *offset)
                {
                    objects.push(public_metadata(key, &file));
                    if objects.len() > context.policy.max_list_items {
                        return Err(list_scan_limit_error());
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
        Ok(ListResult {
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
        let (bytes_transferred, digest) =
            copy_with_control(&mut stream, sink, context, false).await?;
        context
            .control
            .run(
                async {
                    remote
                        .ftp
                        .finalize_retr_stream(stream)
                        .await
                        .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, false))
                },
                ErrorPhase::Commit,
                false,
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
        if !request.overwrite && ftp_exists(&mut remote.ftp, &request.key).await? {
            return Err(conflict_error("FTP_CREATE_CONFLICT"));
        }
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
                let _ = remote.ftp.abort(stream).await;
                return Err(error);
            }
        };
        if request
            .content_length
            .is_some_and(|expected| expected != bytes_transferred)
        {
            let _ = remote.ftp.abort(stream).await;
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
        let result = context
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
        validate_key(&request.source_key)?;
        validate_key(&request.destination_key)?;
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
        if !request.overwrite
            && ftp_exists(&mut destination_remote.ftp, &request.destination_key).await?
        {
            return Err(conflict_error("FTP_COPY_CONFLICT"));
        }
        let mut source = source_remote
            .ftp
            .retr_as_stream(&request.source_key)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))?;
        let mut destination = destination_remote
            .ftp
            .put_with_stream(&request.destination_key)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, true))?;
        if let Err(error) = copy_with_control(&mut source, &mut destination, context, true).await {
            let _ = destination_remote.ftp.abort(destination).await;
            let _ = source_remote.ftp.abort(source).await;
            return Err(error);
        }
        destination_remote
            .ftp
            .finalize_put_stream(destination)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))?;
        source_remote
            .ftp
            .finalize_retr_stream(source)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, false))?;
        stat_file(
            &mut destination_remote.ftp,
            &request.destination_key,
            context,
        )
        .await
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<FtpConnectionConfig> {
    serde_json::from_value(connection.config.clone()).map_err(|_| {
        StorageError::invalid_configuration(
            "FTP_CONFIGURATION_INVALID",
            "FTP configuration does not match its public contract",
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
            "FTP_ROOT_INVALID",
            "FTP root path is invalid",
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
            "FTP does not preserve object content type or custom metadata",
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
        if !ftp_exists(ftp, &current).await? {
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

async fn ftp_exists(ftp: &mut AsyncFtpStream, path: &str) -> StorageResult<bool> {
    match ftp.mlst(Some(path)).await {
        Ok(_) => Ok(true),
        Err(error) if is_file_unavailable(&error) => Ok(false),
        Err(error) => Err(map_ftp_error(error, ErrorPhase::Probe, false)),
    }
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

fn list_limit(request: &ListRequest, context: &OperationContext<'_>) -> StorageResult<usize> {
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
    TransferResult {
        key,
        bytes_transferred,
        checksum: IntegrityMetadata {
            algorithm: "sha256".to_owned(),
            value: format!("{:x}", digest.finalize()),
        },
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
        FtpError::UnexpectedResponse(response) if response.status == Status::FileUnavailable => (
            ErrorCategory::NotFound,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_OBJECT_NOT_FOUND",
            "FTP object was not found or unavailable",
        ),
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
            RemoteEffect::Partial
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

fn conflict_error(code: &'static str) -> StorageError {
    StorageError::new(
        ErrorCategory::Conflict,
        ErrorPhase::Prepare,
        RemoteEffect::None,
        RetryDisposition::Never,
        code,
        "FTP destination already exists",
    )
    .with_provider(PROVIDER_ID)
}

fn transfer_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::Partial,
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
        "FTP listing exceeds the engine scan limit",
    )
    .with_provider(PROVIDER_ID)
}

#[cfg(test)]
mod tests {
    use super::validate_key;

    #[test]
    fn keys_cannot_escape_the_remote_root() {
        assert!(validate_key("folder/object.bin").is_ok());
        assert!(validate_key("../secret").is_err());
        assert!(validate_key("/absolute").is_err());
    }
}
