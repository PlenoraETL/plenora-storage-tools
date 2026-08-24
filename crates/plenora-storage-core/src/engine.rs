use std::{
    collections::{BTreeMap, btree_map::Entry},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncWrite};

use crate::{
    CapabilityDocument, CopyRequest, DeleteRequest, DeleteResult, ErrorCategory, ErrorPhase,
    ExecutionControl, GetRequest, ListRequest, ListResult, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, PutRequest, RemoteEffect, RetryDisposition,
    StatRequest, StorageError, StorageProvider, StorageResult, Surface, TestResult, TransferResult,
};

pub const LIST_CURSOR_TTL_SECONDS: u64 = 900;
pub const LIST_CURSOR_MAX_BYTES: usize = 512;
pub const LIST_CURSOR_MAX_ACTIVE: usize = 1_024;
const CURSOR_TTL: Duration = Duration::from_secs(LIST_CURSOR_TTL_SECONDS);

#[derive(Clone)]
struct CursorState {
    provider: String,
    connection_fingerprint: String,
    prefix: Option<String>,
    max_items: Option<usize>,
    start_after: String,
    expires_at: Instant,
}

#[derive(Clone, Debug)]
// Each flag is an independent, fail-closed host authorization. Keeping them
// explicit prevents enabling one insecure transport from enabling another.
#[allow(clippy::struct_excessive_bools)]
pub struct EngineConfig {
    pub allow_experimental_contracts: bool,
    pub allow_insecure_http: bool,
    pub allow_insecure_ftp: bool,
    pub allow_private_network: bool,
    pub allow_unverified_ssh: bool,
    pub max_transfer_bytes: u64,
    pub max_list_items: usize,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            allow_experimental_contracts: false,
            allow_insecure_http: false,
            allow_insecure_ftp: false,
            allow_private_network: false,
            allow_unverified_ssh: false,
            max_transfer_bytes: 1_073_741_824,
            max_list_items: 10_000,
        }
    }
}

pub struct Engine {
    config: EngineConfig,
    providers: BTreeMap<String, Arc<dyn StorageProvider>>,
    closed: AtomicBool,
    cursors: Mutex<BTreeMap<String, CursorState>>,
    cursor_nonce: AtomicU64,
}

impl Engine {
    #[must_use]
    pub fn new(config: EngineConfig) -> Self {
        Self {
            config,
            providers: BTreeMap::new(),
            closed: AtomicBool::new(false),
            cursors: Mutex::new(BTreeMap::new()),
            cursor_nonce: AtomicU64::new(0),
        }
    }

    pub fn register_provider(&mut self, provider: Arc<dyn StorageProvider>) -> StorageResult<()> {
        if self.closed.load(Ordering::Acquire) {
            return Err(StorageError::engine_closed());
        }
        let id = provider.id().to_owned();
        match self.providers.entry(id.clone()) {
            Entry::Vacant(entry) => {
                entry.insert(provider);
            }
            Entry::Occupied(_) => {
                return Err(StorageError::invalid_configuration(
                    "DUPLICATE_PROVIDER",
                    format!("provider '{id}' is already registered"),
                ));
            }
        }
        Ok(())
    }

    #[must_use]
    pub fn capabilities(&self) -> CapabilityDocument {
        self.capabilities_for(Surface::Rust)
    }

    #[must_use]
    pub fn capabilities_for(&self, surface: Surface) -> CapabilityDocument {
        let providers = self
            .providers
            .values()
            .map(|provider| provider.capabilities())
            .collect();
        CapabilityDocument::new(surface, providers)
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
        if let Ok(mut cursors) = self.cursors.lock() {
            cursors.clear();
        }
    }

    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn provider(&self, connection: &ProviderConnection) -> StorageResult<&dyn StorageProvider> {
        if self.is_closed() {
            return Err(StorageError::engine_closed());
        }
        if !self.config.allow_experimental_contracts {
            return Err(StorageError::invalid_configuration(
                "EXPERIMENTAL_CONTRACT_OPT_IN_REQUIRED",
                "storage v1 operations require explicit experimental-contract authorization",
            ));
        }
        let provider = self.providers.get(&connection.provider).ok_or_else(|| {
            StorageError::unsupported(format!(
                "storage provider '{}' is not available in this artifact",
                connection.provider
            ))
        })?;
        if connection.config_contract != provider.config_contract() {
            return Err(StorageError::invalid_configuration(
                "UNSUPPORTED_PROVIDER_CONFIG_CONTRACT",
                "provider configuration contract is unsupported",
            )
            .with_provider(&connection.provider));
        }
        Ok(provider.as_ref())
    }

    fn context<'a>(&'a self, control: &'a ExecutionControl) -> OperationContext<'a> {
        OperationContext {
            policy: &self.config,
            control,
        }
    }

    pub async fn test(
        &self,
        connection: &ProviderConnection,
        control: &ExecutionControl,
    ) -> StorageResult<TestResult> {
        self.provider(connection)?
            .test(connection, &self.context(control))
            .await
    }

    pub async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ListRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ListResult> {
        let provider = self.provider(connection)?;
        let start_after = request
            .cursor
            .as_deref()
            .map(|cursor| self.resolve_cursor(cursor, connection, request))
            .transpose()?;
        let provider_request = ProviderListRequest {
            prefix: request.prefix.clone(),
            start_after,
            max_items: request.max_items,
        };
        let result = provider
            .list(connection, &provider_request, &self.context(control))
            .await?;
        let next_cursor = result
            .next_start_after
            .as_deref()
            .map(|start_after| self.issue_cursor(connection, request, start_after))
            .transpose()?;
        Ok(ListResult {
            objects: result.objects,
            truncated: result.truncated,
            next_cursor,
        })
    }

    pub async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ObjectMetadata> {
        self.provider(connection)?
            .stat(connection, request, &self.context(control))
            .await
    }

    pub async fn get<W>(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut W,
        control: &ExecutionControl,
    ) -> StorageResult<TransferResult>
    where
        W: AsyncWrite + Send + Unpin,
    {
        self.provider(connection)?
            .get(connection, request, sink, &self.context(control))
            .await
    }

    pub async fn put<R>(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut R,
        control: &ExecutionControl,
    ) -> StorageResult<TransferResult>
    where
        R: AsyncRead + Send + Unpin,
    {
        self.provider(connection)?
            .put(connection, request, source, &self.context(control))
            .await
    }

    pub async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        control: &ExecutionControl,
    ) -> StorageResult<DeleteResult> {
        self.provider(connection)?
            .delete(connection, request, &self.context(control))
            .await
    }

    pub async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ObjectMetadata> {
        self.provider(connection)?
            .copy(connection, request, &self.context(control))
            .await
    }

    fn resolve_cursor(
        &self,
        token: &str,
        connection: &ProviderConnection,
        request: &ListRequest,
    ) -> StorageResult<String> {
        if token.len() > LIST_CURSOR_MAX_BYTES || !token.starts_with("cursor://") {
            return Err(cursor_error(
                "LIST_CURSOR_INVALID_OR_EXPIRED",
                "list cursor is invalid or expired",
            ));
        }
        let fingerprint = connection_fingerprint(connection)?;
        let mut cursors = self.cursors.lock().map_err(|_| {
            StorageError::new(
                ErrorCategory::Internal,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "LIST_CURSOR_STATE_UNAVAILABLE",
                "list cursor state is unavailable",
            )
        })?;
        let now = Instant::now();
        cursors.retain(|_, state| state.expires_at > now);
        let state = cursors.get(token).ok_or_else(|| {
            cursor_error(
                "LIST_CURSOR_INVALID_OR_EXPIRED",
                "list cursor is invalid or expired",
            )
        })?;
        if state.provider != connection.provider
            || state.connection_fingerprint != fingerprint
            || state.prefix != request.prefix
            || state.max_items != request.max_items
        {
            return Err(cursor_error(
                "LIST_CURSOR_SCOPE_MISMATCH",
                "list cursor does not belong to this provider, connection or request scope",
            ));
        }
        let start_after = state.start_after.clone();
        drop(cursors);
        Ok(start_after)
    }

    fn issue_cursor(
        &self,
        connection: &ProviderConnection,
        request: &ListRequest,
        start_after: &str,
    ) -> StorageResult<String> {
        let fingerprint = connection_fingerprint(connection)?;
        let nonce = self.cursor_nonce.fetch_add(1, Ordering::Relaxed);
        let mut digest = Sha256::new();
        digest.update(connection.provider.as_bytes());
        digest.update(fingerprint.as_bytes());
        digest.update(request.prefix.as_deref().unwrap_or_default().as_bytes());
        digest.update(request.max_items.unwrap_or_default().to_le_bytes());
        digest.update(start_after.as_bytes());
        digest.update(nonce.to_le_bytes());
        digest.update(std::process::id().to_le_bytes());
        let token = format!("cursor://{:x}", digest.finalize());
        let mut cursors = self.cursors.lock().map_err(|_| {
            StorageError::new(
                ErrorCategory::Internal,
                ErrorPhase::Commit,
                RemoteEffect::None,
                RetryDisposition::Never,
                "LIST_CURSOR_STATE_UNAVAILABLE",
                "list cursor state is unavailable",
            )
        })?;
        let now = Instant::now();
        cursors.retain(|_, state| state.expires_at > now);
        if cursors.len() >= LIST_CURSOR_MAX_ACTIVE
            && let Some(oldest) = cursors
                .iter()
                .min_by_key(|(_, state)| state.expires_at)
                .map(|(token, _)| token.clone())
        {
            cursors.remove(&oldest);
        }
        cursors.insert(
            token.clone(),
            CursorState {
                provider: connection.provider.clone(),
                connection_fingerprint: fingerprint,
                prefix: request.prefix.clone(),
                max_items: request.max_items,
                start_after: start_after.to_owned(),
                expires_at: now + CURSOR_TTL,
            },
        );
        drop(cursors);
        Ok(token)
    }
}

fn connection_fingerprint(connection: &ProviderConnection) -> StorageResult<String> {
    let encoded = serde_json::to_vec(connection).map_err(|_| {
        StorageError::invalid_configuration(
            "STORAGE_CONNECTION_INVALID",
            "storage connection cannot be canonicalized for cursor scope",
        )
    })?;
    Ok(format!("{:x}", Sha256::digest(encoded)))
}

fn cursor_error(code: &'static str, message: &'static str) -> StorageError {
    StorageError::invalid_configuration(code, message)
}

#[cfg(test)]
mod cursor_tests {
    use super::*;

    fn connection() -> ProviderConnection {
        ProviderConnection {
            provider: "test".to_owned(),
            config_contract: "plenora-storage-test-connection-v1".to_owned(),
            config: serde_json::json!({"scope": "a"}),
            credential_ref: "secret://storage/test".to_owned(),
        }
    }

    fn request() -> ListRequest {
        ListRequest {
            prefix: Some("prefix/".to_owned()),
            cursor: None,
            max_items: Some(10),
        }
    }

    #[test]
    fn cursor_duration_close_and_eviction_are_fail_closed() {
        assert_eq!(LIST_CURSOR_TTL_SECONDS, 15 * 60);
        let engine = Engine::new(EngineConfig::default());
        let connection = connection();
        let request = request();

        let expired = engine
            .issue_cursor(&connection, &request, "expired")
            .expect("issue cursor");
        assert!(expired.len() <= LIST_CURSOR_MAX_BYTES);
        engine
            .cursors
            .lock()
            .expect("cursor lock")
            .get_mut(&expired)
            .expect("cursor state")
            .expires_at = Instant::now();
        assert_eq!(
            engine
                .resolve_cursor(&expired, &connection, &request)
                .expect_err("expired cursor must fail")
                .code,
            "LIST_CURSOR_INVALID_OR_EXPIRED"
        );

        let oldest = engine
            .issue_cursor(&connection, &request, "oldest")
            .expect("issue oldest");
        for index in 0..LIST_CURSOR_MAX_ACTIVE {
            engine
                .issue_cursor(&connection, &request, &format!("key-{index}"))
                .expect("issue cursor for eviction");
        }
        assert_eq!(
            engine
                .resolve_cursor(&oldest, &connection, &request)
                .expect_err("oldest cursor must be evicted")
                .code,
            "LIST_CURSOR_INVALID_OR_EXPIRED"
        );

        let closed = engine
            .issue_cursor(&connection, &request, "closed")
            .expect("issue cursor before close");
        engine.close();
        assert_eq!(
            engine
                .resolve_cursor(&closed, &connection, &request)
                .expect_err("close must invalidate cursors")
                .code,
            "LIST_CURSOR_INVALID_OR_EXPIRED"
        );
    }
}
