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
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, StorageError,
    StorageResult, resolve_network_target,
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
            .map_err(|error| smb_error(&error, false))?;
        let session = Session::setup(
            &mut conn,
            material.required("username")?,
            material.required("password")?,
            material.optional("domain").unwrap_or_default(),
        )
        .await
        .map_err(|error| smb_error(&error, false).with_detail("operation", "session_setup"))?;
        // Require authenticated SMB3 encryption. No guest sessions, ambient
        // credentials, DFS referrals or automatic reconnect to unvalidated hosts.
        let cipher = conn
            .params()
            .and_then(|policy| policy.cipher)
            .unwrap_or(smb2::crypto::encryption::Cipher::Aes128Ccm);
        match (session.encryption_key, session.decryption_key) {
            (Some(enc), Some(dec)) if session.should_sign => {
                conn.activate_encryption(enc, dec, cipher);
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
fn smb_error(error: &smb2::Error, mutating: bool) -> StorageError {
    let category = match error.kind() {
        ErrorKind::NotFound => ErrorCategory::NotFound,
        ErrorKind::AlreadyExists => ErrorCategory::Conflict,
        ErrorKind::AccessDenied => ErrorCategory::Authorization,
        ErrorKind::AuthRequired => ErrorCategory::Authentication,
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
