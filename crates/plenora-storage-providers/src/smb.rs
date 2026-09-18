use crate::{
    common::{Backend, ProviderFactory, Reader, failure, invalid, metadata, page, parse},
    local::portable_key,
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
pub struct SmbConnectionConfig {
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    pub share: String,
    #[serde(default)]
    pub root: String,
}
const fn default_port() -> u16 {
    445
}
pub struct Smb;
#[async_trait]
impl ProviderFactory for Smb {
    const ID: &'static str = "smb";
    const CONTRACT: &'static str = "plenora-storage-smb-connection-v1";
    const ATOMIC: bool = false;
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
#[async_trait]
impl Backend for SmbBackend {
    async fn test(&mut self) -> StorageResult<()> {
        let path = self.path("")?;
        let info = self
            .tree
            .stat(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        if !info.is_directory {
            return Err(invalid("SMB_ROOT_NOT_DIRECTORY"));
        }
        Ok(())
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let mut stack = vec![String::new()];
        let mut selected = BTreeMap::new();
        let mut scanned = 0_usize;
        while let Some(parent) = stack.pop() {
            let path = self.path(&parent)?;
            crate::smb_listing::directory(
                &mut self.conn,
                &self.tree,
                &path,
                &parent,
                request,
                limit,
                &mut selected,
                &mut stack,
                &mut scanned,
            )
            .await?;
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        let path = self.path(key)?;
        let info = self
            .tree
            .stat(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        if info.is_directory {
            return Err(invalid("SMB_REGULAR_FILE_REQUIRED"));
        }
        Ok(metadata(key, info.size))
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let path = self.path(key)?;
        let reader = self
            .tree
            .open_file_reader(self.conn.clone(), &path)
            .await
            .map_err(|error| smb_error(&error, false))?;
        Ok((
            metadata(key, reader.size()),
            Box::new(SmbReader {
                reader: Some(reader),
                offset: 0,
            }),
        ))
    }
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()> {
        let path = self.path(&request.key)?;
        let mut prepared = false;
        let result = async {
            if let Some((parent, _)) = request.key.rsplit_once('/') {
                let mut current = String::new();
                for part in parent.split('/') {
                    if !current.is_empty() {
                        current.push('/');
                    }
                    current.push_str(part);
                    let path = self.path(&current)?;
                    match self.tree.stat(&mut self.conn, &path).await {
                        Ok(info) if info.is_directory => {}
                        Ok(_) => return Err(invalid("SMB_PARENT_NOT_DIRECTORY")),
                        Err(error) if error.kind() == ErrorKind::NotFound => {
                            prepared = true;
                            if let Err(error) =
                                self.tree.create_directory(&mut self.conn, &path).await
                                && error.kind() != ErrorKind::AlreadyExists
                            {
                                return Err(smb_error(&error, true));
                            }
                        }
                        Err(error) => return Err(smb_error(&error, false)),
                    }
                }
            }
            let mut writer = if request.overwrite {
                self.tree.create_file_writer(self.conn.clone(), &path).await
            } else {
                self.tree
                    .create_file_writer_exclusive(self.conn.clone(), &path)
                    .await
            }
            .map_err(|error| smb_error(&error, true))?;
            for chunk in data.chunks(64 * 1024) {
                writer.write_chunk(chunk).await.map_err(|error| {
                    smb_error(&error, true).cleanup_unconfirmed("destination_may_be_partial")
                })?;
            }
            let size = writer.finish().await.map_err(|error| {
                smb_error(&error, true).cleanup_unconfirmed("destination_may_be_partial")
            })?;
            if size != data.len() as u64 {
                return Err(failure(ErrorCategory::Protocol, ErrorPhase::Commit, true));
            }
            Ok(())
        }
        .await;
        result.map_err(|error: StorageError| error.with_preparation_effect(prepared))
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()> {
        let path = self.path(key)?;
        self.stat(key).await?;
        self.tree
            .delete_file(&mut self.conn, &path)
            .await
            .map_err(|error| smb_error(&error, true))
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
