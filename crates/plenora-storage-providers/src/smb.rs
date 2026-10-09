mod operations;
mod publication;

use crate::{
    common::{
        Backend, ProviderFactory, Reader, failure, invalid, metadata, page, parse, timed_out,
    },
    keys::portable_key,
};
use async_trait::async_trait;
use bytes::Bytes;
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect,
    RetryDisposition, StorageError, StorageResult, resolve_network_target,
};
use serde::Deserialize;
use smb2::{ErrorKind, FileReader, Session, Tree, client::connection::Connection};
use std::{collections::BTreeMap, net::SocketAddr, sync::Arc, time::Duration};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Non-secret SMB3 share addressing; authenticated encryption is required.
pub struct SmbConnectionConfig {
    /// Server host or IP address, validated against the engine network policy before dialing.
    pub host: String,
    #[serde(default = "default_port")]
    /// Nonzero SMB service port; defaults to 445.
    pub port: u16,
    /// SMB share name; normalized portable key rules apply.
    pub share: String,
    #[serde(default)]
    /// Optional normalized portable path inside the share; defaults to the share root.
    pub root: String,
}
const fn default_port() -> u16 {
    445
}
/// SMB3 backend factory requiring authenticated encryption and validated addresses.
pub struct Smb;
#[async_trait]
impl ProviderFactory for Smb {
    const ID: &'static str = "smb";
    const CONTRACT: &'static str = "plenora-storage-smb-connection-v1";
    const ATOMIC: bool = false;
    const SPOOLED_PUT: bool = true;
    fn validate(connection: &ProviderConnection, _: &EngineConfig) -> StorageResult<()> {
        let cfg: SmbConnectionConfig = parse(connection)?;
        if cfg.host.is_empty()
            || cfg.host.len() > 253
            || cfg.host.contains(['/', '\\', '\0'])
            || cfg.host.chars().any(char::is_whitespace)
            || cfg.port == 0
            || cfg.share.contains('/')
        {
            return Err(invalid("SMB_CONFIG_INVALID"));
        }
        portable_key(&cfg.share)?;
        if !cfg.root.is_empty() {
            portable_key(&cfg.root)?;
        }
        Ok(())
    }
    async fn connect(
        connection: &ProviderConnection,
        credentials: &dyn CredentialResolver,
        context: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        let cfg: SmbConnectionConfig = parse(connection)?;
        let addresses =
            resolve_network_target(&cfg.host, cfg.port, context.policy.allow_private_network)
                .await?;
        let material = credentials.resolve(&connection.credential_ref)?;
        let connect_timeout = context
            .control
            .remaining()
            .map_or(SMB_CONNECT_TIMEOUT, |remaining| {
                SMB_CONNECT_TIMEOUT.min(remaining)
            });
        let mut conn = first_connection(&addresses, |address| async move {
            Connection::connect(&address.to_string(), connect_timeout).await
        })
        .await?;
        arm_response_timeout(&conn, context.control.remaining());
        conn.negotiate()
            .await
            .map_err(|error| smb_login_error(&error))?;
        let session = Session::setup(
            &mut conn,
            material.required("username")?,
            material.required("password")?,
            material.optional("domain").unwrap_or_default(),
        )
        .await
        .map_err(|error| smb_login_error(&error).with_detail("operation", "session_setup"))?;
        // Require authenticated SMB3 encryption. No guest sessions, ambient
        // credentials, DFS referrals or automatic reconnect to unvalidated hosts.
        let cipher = conn
            .params()
            .and_then(|policy| policy.cipher)
            .unwrap_or(smb2::crypto::encryption::Cipher::Aes128Ccm);
        match (session.encryption_key, session.decryption_key) {
            (Some(enc), Some(dec)) if session.should_sign => {
                conn.activate_encryption(enc, dec, cipher)
                    .map_err(|error| {
                        smb_error(&error, false).with_detail("operation", "activate_encryption")
                    })?;
            }
            _ => {
                return Err(StorageError::unsupported(
                    "SMB3 authenticated encryption is required",
                ));
            }
        }
        let tree = Arc::new(
            Tree::connect(&mut conn, &cfg.share)
                .await
                .map_err(|error| {
                    smb_error(&error, false).with_detail("operation", "tree_connect")
                })?,
        );
        Ok(Box::new(SmbBackend {
            conn,
            tree,
            root: cfg.root,
        }))
    }
}
struct SmbBackend {
    conn: Connection,
    tree: Arc<Tree>,
    root: String,
}
impl SmbBackend {
    fn path(&self, key: &str) -> StorageResult<String> {
        if !key.is_empty() {
            portable_key(key)?;
        }
        Ok(if self.root.is_empty() {
            key.to_owned()
        } else if key.is_empty() {
            self.root.clone()
        } else {
            format!("{}/{key}", self.root)
        })
    }
}
struct SmbReader {
    reader: Option<FileReader>,
    offset: u64,
}
#[async_trait]
impl Reader for SmbReader {
    async fn next(&mut self) -> StorageResult<Option<Bytes>> {
        let reader = self
            .reader
            .as_ref()
            .ok_or_else(|| invalid("SMB_READER_CLOSED"))?;
        if self.offset >= reader.size() {
            return Ok(None);
        }
        let data = reader
            .read_at(self.offset, (reader.size() - self.offset).min(64 * 1024))
            .await
            .map_err(|error| smb_error(&error, false))?;
        if data.is_empty() {
            return Err(failure(ErrorCategory::Protocol, ErrorPhase::Read, false));
        }
        self.offset += data.len() as u64;
        Ok(Some(data.into()))
    }
    async fn close(&mut self) -> StorageResult<()> {
        if let Some(reader) = self.reader.take() {
            reader
                .close()
                .await
                .map_err(|error| smb_error(&error, false))?;
        }
        Ok(())
    }
}
/// Upper bound on establishing the TCP connection; the time remaining before
/// the deadline applies when it is shorter.
const SMB_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// How long the SMB client waits in silence for one response when the
/// operation has no deadline.
///
/// The client restarts this wait on every interim
/// `STATUS_PENDING`, and allows six times as much on a connection whose
/// keepalive proves it alive (see plenora-smb2 `ALIVE_DEADLINE_FACTOR`).
pub const SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE: Duration = Duration::from_secs(30);

/// The SMB response timeout for an operation: the time remaining before the
/// deadline (the operation control ends every wait exactly there), never
/// zero; [`SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE`] without one. Before 3.0.0
/// it was the library's fixed 30 s, whatever the deadline.
fn response_timeout(remaining: Option<Duration>) -> Duration {
    remaining.map_or(SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE, |remaining| {
        remaining.max(Duration::from_millis(1))
    })
}

/// Whether an SMB failure means the client gave up waiting: a request left
/// unanswered past its response timeout, including on a connection declared
/// unresponsive because nothing at all answered, a request that could not
/// reach the socket in time, a wait for credits that ran out of time, or an
/// I/O timeout anywhere in the cause chain. Credits that cannot arrive at all
/// (`CreditsExhausted`) fail without waiting and are not a timeout.
fn is_smb_timeout(error: &smb2::Error) -> bool {
    if matches!(
        error,
        smb2::Error::Timeout
            | smb2::Error::SendTimeout { .. }
            | smb2::Error::ServerUnresponsive { .. }
            | smb2::Error::CreditStarvation { .. }
    ) || error.kind() == ErrorKind::TimedOut
    {
        return true;
    }
    let mut cause: Option<&(dyn std::error::Error + 'static)> = Some(error);
    while let Some(current) = cause {
        if current
            .downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::TimedOut)
        {
            return true;
        }
        cause = current.source();
    }
    false
}

/// Dials `addresses` in order and returns the first connection established.
///
/// When none is, the outcome depends on every failure, never on which address
/// came last: see [`smb_connect_error`].
async fn first_connection<C, F, Fut>(addresses: &[SocketAddr], mut dial: F) -> StorageResult<C>
where
    F: FnMut(SocketAddr) -> Fut,
    Fut: Future<Output = Result<C, smb2::Error>>,
{
    let mut failures = Vec::with_capacity(addresses.len());
    for &address in addresses {
        match dial(address).await {
            Ok(connection) => return Ok(connection),
            Err(error) => failures.push(error),
        }
    }
    Err(smb_connect_error(&failures))
}

/// The TCP connection failed on every address: a timeout when every attempt
/// ran out of time, `io` as soon as one was refused or failed otherwise, so
/// the outcome does not depend on the order of the addresses. Nothing was
/// sent, so a timeout is safe to retry.
fn smb_connect_error(failures: &[smb2::Error]) -> StorageError {
    if !failures.is_empty() && failures.iter().all(connect_timed_out) {
        timed_out(ErrorPhase::Connect, false)
    } else {
        failure(ErrorCategory::Io, ErrorPhase::Connect, false)
    }
}

/// Whether one failed dial ran out of time on every address it tried.
fn connect_timed_out(error: &smb2::Error) -> bool {
    match error {
        smb2::Error::ConnectFailed { attempts, .. } => {
            !attempts.is_empty()
                && attempts.iter().all(|attempt| {
                    attempt
                        .error_kind
                        .is_none_or(|kind| kind == std::io::ErrorKind::TimedOut)
                })
        }
        other => is_smb_timeout(other),
    }
}

/// The server's credit window cannot fund a request and no response can
/// widen it: a resource limit of this connection, not a timeout. Nothing was
/// sent; a new connection starts with a fresh window.
fn credits_exhausted(phase: ErrorPhase, mutating: bool) -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
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
        "SMB_CREDITS_EXHAUSTED",
        "SMB server granted too few credits for the request",
    )
}

/// Sets the connection's response timeout for an operation with `remaining`
/// time before its deadline.
fn arm_response_timeout(conn: &Connection, remaining: Option<Duration>) {
    conn.set_response_timeout(Some(response_timeout(remaining)));
}

/// Classifies a failed negotiate or session setup. Neither has a remote
/// effect, so a refusal that may clear by itself is safe to retry.
///
/// Only the server's logon-rejection statuses (`STATUS_LOGON_FAILURE` and the
/// account restrictions [`ErrorKind::AuthRequired`] groups) reject the
/// credentials. A local failure of the authentication exchange itself
/// (`smb2::Error::Auth`, for example a missing session key) is an unexpected
/// protocol state, never rejected credentials. Before 3.0.0 these failures
/// went through [`smb_error`]: phase `read`, transient refusals and timeouts
/// with retry `never`, and local exchange failures as `authentication`.
fn smb_login_error(error: &smb2::Error) -> StorageError {
    let (category, retry, code, message) = match error {
        smb2::Error::Auth { .. } => (
            ErrorCategory::Protocol,
            RetryDisposition::Never,
            "SMB_SESSION_SETUP_FAILED",
            "SMB authentication exchange failed",
        ),
        _ if error.kind() == ErrorKind::AuthRequired => (
            ErrorCategory::Authentication,
            RetryDisposition::Never,
            "SMB_AUTHENTICATION_FAILED",
            "SMB server rejected the credentials",
        ),
        _ if error.kind() == ErrorKind::AccessDenied => (
            ErrorCategory::Authorization,
            RetryDisposition::Never,
            "SMB_ACCESS_DENIED",
            "SMB server denied the session",
        ),
        smb2::Error::CreditsExhausted { .. } => {
            return credits_exhausted(ErrorPhase::Connect, false);
        }
        _ if is_smb_timeout(error) => (
            ErrorCategory::Timeout,
            RetryDisposition::Safe,
            "SMB_CONNECT_TIMEOUT",
            "SMB session setup timed out",
        ),
        _ if error.is_retryable()
            || matches!(
                error.status(),
                Some(smb2::types::status::NtStatus::REQUEST_NOT_ACCEPTED)
            )
            || matches!(error.kind(), ErrorKind::ConnectionLost | ErrorKind::Io) =>
        {
            (
                ErrorCategory::Transient,
                RetryDisposition::Safe,
                "SMB_SESSION_TEMPORARILY_REFUSED",
                "SMB server temporarily refused the session",
            )
        }
        smb2::Error::Internal { .. } => (
            ErrorCategory::Internal,
            RetryDisposition::Never,
            "SMB_INTERNAL_ERROR",
            "SMB client reached an internal error",
        ),
        _ => (
            ErrorCategory::Protocol,
            RetryDisposition::Never,
            "SMB_SESSION_SETUP_FAILED",
            "SMB session setup failed",
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
}

fn smb_error(error: &smb2::Error, mutating: bool) -> StorageError {
    let phase = if mutating {
        ErrorPhase::Commit
    } else {
        ErrorPhase::Read
    };
    // A response the client gave up waiting for is a timeout; before 3.0.0 it
    // was `io`.
    if is_smb_timeout(error) {
        return timed_out(phase, mutating);
    }
    if matches!(error, smb2::Error::CreditsExhausted { .. }) {
        return credits_exhausted(phase, mutating);
    }
    let category = match error.kind() {
        ErrorKind::NotFound => ErrorCategory::NotFound,
        ErrorKind::AlreadyExists => ErrorCategory::Conflict,
        ErrorKind::AccessDenied => ErrorCategory::Authorization,
        ErrorKind::AuthRequired => ErrorCategory::Authentication,
        ErrorKind::Internal => ErrorCategory::Internal,
        _ => ErrorCategory::Io,
    };
    failure(
        category,
        if mutating {
            ErrorPhase::Commit
        } else {
            ErrorPhase::Read
        },
        mutating,
    )
}

#[cfg(test)]
#[path = "smb_tests.rs"]
mod tests;
