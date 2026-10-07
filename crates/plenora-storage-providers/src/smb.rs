mod operations;
mod publication;

use crate::{
    common::{Backend, ProviderFactory, Reader, failure, invalid, metadata, page, parse},
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
use std::{collections::BTreeMap, sync::Arc, time::Duration};

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
        let mut connected = None;
        for address in addresses {
            if let Ok(conn) =
                Connection::connect(&address.to_string(), Duration::from_secs(5)).await
            {
                connected = Some(conn);
                break;
            }
        }
        let mut conn =
            connected.ok_or_else(|| failure(ErrorCategory::Io, ErrorPhase::Connect, false))?;
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
        _ if matches!(error.kind(), ErrorKind::TimedOut) => (
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
