//! SFTP adapter for `plenora-storage-core`.
//!
//! Transfers stream through bounded chunks. Atomic publication additionally
//! requires a qualified rename connection, overwrite enabled and the server's
//! POSIX rename extension. Cancellation during rename can leave an unknown effect.

#![forbid(unsafe_code)]

mod errors;
mod operations;
mod publication;
mod transfer;
use errors::{
    committed_verification_error, configuration_error, flush_error, list_scan_limit_error,
    map_exclusive_open_error, map_sftp_error, map_ssh_connect_error, mutation_stream_error,
    stream_error, transfer_io_error, transfer_limit_error,
};
use transfer::{
    atomic_replace, copy_with_control, discard_staged_object, ensure_parent_directories,
    list_limit, public_metadata, qualify_atomic_session, relative_key, remote_path, scan_directory,
    temporary_path, transfer_result,
};
use validation::{
    parse_config, validate_file_metadata, validate_key, validate_prefix, validate_sftp_publication,
};
mod validation;

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
    ArtifactMetadata, CopyRequest, CredentialMaterial, CredentialResolver, DeleteRequest,
    DeleteResult, ErrorCategory, ErrorPhase, ExecutionControl, GetRequest, IntegrityMetadata,
    ObjectMetadata, OperationContext, ProviderCapabilities, ProviderConnection,
    ProviderListRequest, ProviderListResult, PublicationPolicy, PutRequest, RemoteEffect,
    RetryDisposition, StatRequest, StorageError, StorageProvider, StorageResult, TestResult,
    TransferResult, directory_may_contain, key_matches_prefix, resolve_network_target,
    validate_object_key, validate_object_prefix,
};
use russh::{
    client,
    keys::{HashAlg, PrivateKey, PrivateKeyWithHashAlg, PublicKeyOrCertificate, decode_secret_key},
};
use russh_sftp::{
    client::{
        Config as SftpClientConfig, RawSftpSession, SftpSession, error::Error as SftpError,
        fs::Metadata,
    },
    protocol::{OpenFlags, Packet, StatusCode},
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

/// Stable provider identifier for dispatch and capability discovery.
pub const PROVIDER_ID: &str = "sftp";
/// Limit on the server's answer to one SFTP request when the operation has no
/// deadline.
///
/// The SFTP client library otherwise gives every request a fixed 10 s timeout
/// that ignores the operation deadline, and a slow but correct `fsync` of a
/// large upload exceeded it. With a deadline the operation's
/// [`ExecutionControl`] bounds every request with the time that remains, and
/// this constant is not used. Without one, a request that gets no answer for
/// this long fails as `timeout` instead of waiting forever: the limit covers a
/// single unanswered request, not the whole operation.
pub const REQUEST_TIMEOUT_WITHOUT_DEADLINE: Duration = Duration::from_secs(300);
/// Versioned connection contract accepted by this adapter.
pub const CONFIG_CONTRACT: &str = "plenora-storage-sftp-connection-v1";
static TEMPORARY_NAME_NONCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// SSH addressing and publication policy, separate from authentication material.
pub struct SftpConnectionConfig {
    /// Server host or IP address, validated against the engine network policy before dialing.
    pub host: String,
    #[serde(default = "default_port")]
    /// Nonzero SSH service port; defaults to 22.
    pub port: u16,
    #[serde(default = "default_root")]
    /// Remote root, 1–4096 characters, without NUL, backslashes or parent segments.
    /// Defaults to `.` (the authenticated user's working directory).
    pub root: String,
    #[serde(default)]
    /// SHA-256 host-key fingerprint; omission requires explicit engine permission.
    pub host_key_sha256: Option<String>,
    #[serde(default)]
    /// Declares that the deployment has qualified the server's atomic rename.
    /// This does not bypass extension detection or the overwrite requirement.
    pub atomic_rename: bool,
}

const fn default_port() -> u16 {
    22
}

fn default_root() -> String {
    ".".to_owned()
}

/// SFTP adapter; server keys are checked before credential authentication.
pub struct SftpProvider {
    credentials: Arc<dyn CredentialResolver>,
}

impl SftpProvider {
    /// Retains the resolver; network and credential work begin with an operation.
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
        let authentication = SftpAuthentication::from_material(&credential)?;
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
        let authenticated = match authentication {
            SftpAuthentication::Password(password) => {
                ssh.authenticate_password(username, password).await
            }
            SftpAuthentication::PrivateKey(key) => {
                let hash = ssh
                    .best_supported_rsa_hash()
                    .await
                    .map_err(map_ssh_connect_error)?
                    .flatten();
                ssh.authenticate_publickey(username, PrivateKeyWithHashAlg::new(key, hash))
                    .await
            }
        }
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
        let sftp = open_session(channel.into_stream(), context.control)
            .await
            .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
        Ok(SftpConnection { ssh, sftp, root })
    }
}

struct SshClient {
    expected_fingerprint: Option<String>,
    allow_unverified: bool,
}

enum SftpAuthentication {
    Password(String),
    PrivateKey(Arc<PrivateKey>),
}

impl SftpAuthentication {
    fn from_material(material: &CredentialMaterial) -> StorageResult<Self> {
        match (
            material.optional("password"),
            material.optional("private_key"),
        ) {
            (Some(password), None) if material.optional("passphrase").is_none() => {
                Ok(Self::Password(password.to_owned()))
            }
            (None, Some(encoded)) if encoded.len() <= 65_536 => {
                let key =
                    decode_secret_key(encoded, material.optional("passphrase")).map_err(|_| {
                        StorageError::new(
                            ErrorCategory::Authentication,
                            ErrorPhase::Validate,
                            RemoteEffect::None,
                            RetryDisposition::Never,
                            "SFTP_PRIVATE_KEY_INVALID",
                            "SFTP private key could not be decoded or decrypted",
                        )
                        .with_provider(PROVIDER_ID)
                    })?;
                Ok(Self::PrivateKey(Arc::new(key)))
            }
            _ => Err(StorageError::invalid_configuration(
                "SFTP_CREDENTIAL_FIELDS_INVALID",
                "SFTP credentials require either password or private_key with optional passphrase",
            )
            .with_provider(PROVIDER_ID)),
        }
    }
}

#[allow(
    clippy::unused_async_trait_impl,
    reason = "russh declares the handler methods as async; the impl keeps that shape"
)]
impl client::Handler for SshClient {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        // This connection contract pins a raw host key, not a certificate authority.
        let PublicKeyOrCertificate::PublicKey { key, .. } = server_public_key else {
            return Ok(false);
        };
        if self.allow_unverified {
            return Ok(true);
        }
        let actual = key.fingerprint(HashAlg::Sha256).to_string();
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
    async fn atomic_session(&self, control: &ExecutionControl) -> StorageResult<RawSftpSession> {
        let channel = self
            .ssh
            .channel_open_session()
            .await
            .map_err(map_ssh_connect_error)?;
        channel
            .request_subsystem(true, "sftp")
            .await
            .map_err(map_ssh_connect_error)?;
        let session = open_raw_session(channel.into_stream(), control);
        qualify_atomic_session(&session).await?;
        Ok(session)
    }
}

/// Seconds the SFTP client library may wait for the answer to one request.
///
/// With a deadline this is the time remaining, rounded up so the library never
/// expires before the deadline: [`ExecutionControl::run`], which wraps every
/// request, ends the wait exactly at the deadline and reports `timeout`, so the
/// effective limit of each request is the time remaining when it is sent.
/// Without a deadline it is [`REQUEST_TIMEOUT_WITHOUT_DEADLINE`].
fn request_timeout_secs(control: &ExecutionControl) -> u64 {
    control
        .deadline
        .map_or(REQUEST_TIMEOUT_WITHOUT_DEADLINE.as_secs(), |deadline| {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            let whole = remaining.as_secs();
            let rounded = if remaining.subsec_nanos() == 0 {
                whole
            } else {
                whole.saturating_add(1)
            };
            rounded.max(1)
        })
}

/// Opens the high-level SFTP session with the request timeout of `control`
/// instead of the library's fixed 10 s.
async fn open_session<S>(stream: S, control: &ExecutionControl) -> Result<SftpSession, SftpError>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let config = SftpClientConfig {
        request_timeout_secs: request_timeout_secs(control),
        ..SftpClientConfig::default()
    };
    SftpSession::new_with_config(stream, config).await
}

/// Opens a raw SFTP session with the request timeout of `control`, before its
/// first request.
fn open_raw_session<S>(stream: S, control: &ExecutionControl) -> RawSftpSession
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let session = RawSftpSession::new(stream);
    session.set_timeout(request_timeout_secs(control));
    session
}

/// Independent budget for cleanup after a failure.
///
/// The caller's deadline may already have expired, and a cleanup that hangs must
/// not extend the operation without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
