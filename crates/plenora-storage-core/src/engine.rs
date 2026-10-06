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
    validate_object_key, validate_object_prefix,
};

/// Lifetime of engine-local continuation cursors, in seconds.
pub const LIST_CURSOR_TTL_SECONDS: u64 = 900;
/// Maximum encoded continuation cursor length in bytes.
pub const LIST_CURSOR_MAX_BYTES: usize = 512;
/// Maximum retained continuation tokens per live engine.
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

/// Host authorizations and per-operation resource bounds shared by all adapters.
/// Enabling one transport exception does not enable the others.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
// Each flag is an independent, fail-closed host authorization. Keeping them
// explicit prevents enabling one insecure transport from enabling another.
#[allow(clippy::struct_excessive_bools)]
pub struct EngineConfig {
    /// Retained for source compatibility. The qualified v1 catalog is available
    /// without experimental opt-in; this flag does not relax transport policy.
    pub allow_experimental_contracts: bool,
    /// Allow HTTP endpoints; certificate verification remains required for HTTPS.
    pub allow_insecure_http: bool,
    /// Permit unencrypted FTP independently of HTTP and private-network access.
    pub allow_insecure_ftp: bool,
    /// Permit destinations on private networks after address validation.
    pub allow_private_network: bool,
    /// Permit SFTP without a host-key pin. Keep false for authenticated hosts.
    pub allow_unverified_ssh: bool,
    /// Maximum bytes admitted for one transfer, including streaming transfers.
    pub max_transfer_bytes: u64,
    /// Upper bound on the number of objects collected for one listing request.
    pub max_list_items: usize,
    /// Maximum buffered upload/copy payload, including conditional S3 uploads.
    /// Streaming still uses working memory but does not retain the entire file.
    pub max_buffered_put_bytes: u64,
}

/// Default in-memory bound for buffered, conditional uploads.
pub const DEFAULT_MAX_BUFFERED_PUT_BYTES: u64 = 67_108_864;

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
            max_buffered_put_bytes: DEFAULT_MAX_BUFFERED_PUT_BYTES,
        }
    }
}

/// Provider registry and engine-owned pagination state.
///
/// Calls share policy, not an application transaction. Provider capabilities
/// determine publication guarantees; an ambiguous mutation requires recovery.
pub struct Engine {
    config: EngineConfig,
    providers: BTreeMap<String, Arc<dyn StorageProvider>>,
    closed: AtomicBool,
    cursors: Mutex<BTreeMap<String, CursorState>>,
    cursor_nonce: AtomicU64,
}

impl Engine {
    /// Create an empty registry. The application factory registers enabled providers.
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

    /// Register an adapter without replacing an existing adapter of the same ID.
    ///
    /// # Errors
    /// Returns `ENGINE_CLOSED` after closure or `DUPLICATE_PROVIDER` for a reused ID.
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
    /// Discover operations through the Rust surface for the registered providers.
    pub fn capabilities(&self) -> CapabilityDocument {
        self.capabilities_for(Surface::Rust)
    }

    #[must_use]
    /// Discover operations through the requested consumer surface.
    pub fn capabilities_for(&self, surface: Surface) -> CapabilityDocument {
        let providers = self
            .providers
            .values()
            .map(|provider| provider.capabilities())
            .collect();
        CapabilityDocument::new(surface, providers)
    }

    /// Reject new operations and invalidate pagination cursors.
    /// Calls already admitted retain their own cancellation controls and may finish.
    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
        if let Ok(mut cursors) = self.cursors.lock() {
            cursors.clear();
        }
    }

    #[must_use]
    /// Return whether new operations have been disabled by close.
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Runs the local admission checks of an operation without contacting the
    /// provider.
    ///
    /// A consumer that must create an external effect before invoking the
    /// Engine — resolving an artifact sink, for example — calls this first so
    /// that a locally invalid request cannot produce that effect.
    ///
    /// # Errors
    /// Rejects a closed engine, unavailable provider, invalid connection, unsupported
    /// configuration contract or unsafe key. This is not a remote permissions probe.
    pub fn preflight(&self, connection: &ProviderConnection, keys: &[&str]) -> StorageResult<()> {
        self.provider(connection)?;
        for key in keys {
            validate_object_key(key)?;
        }
        Ok(())
    }

    fn provider(&self, connection: &ProviderConnection) -> StorageResult<&dyn StorageProvider> {
        if self.is_closed() {
            return Err(StorageError::engine_closed());
        }
        connection.validate()?;
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
        provider.validate_connection(connection, &self.config)?;
        Ok(provider.as_ref())
    }

    const fn context<'a>(&'a self, control: &'a ExecutionControl) -> OperationContext<'a> {
        OperationContext {
            policy: &self.config,
            control,
        }
    }

    /// Probe the configured provider without creating a stored object.
    ///
    /// # Errors
    /// Reports admission, connection, authentication or control failures with their
    /// public phase and retry disposition; successful probing does not prove write access.
    pub async fn test(
        &self,
        connection: &ProviderConnection,
        control: &ExecutionControl,
    ) -> StorageResult<TestResult> {
        self.provider(connection)?
            .test(connection, &self.context(control))
            .await
    }

    /// Return a bounded page and an opaque cursor owned by this engine.
    /// Keep the connection, prefix and page-size arguments unchanged when resuming.
    ///
    /// # Errors
    /// Rejects invalid/expired cursors, invalid prefixes and provider/control errors.
    /// Closing the engine invalidates its outstanding cursors.
    pub async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ListRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ListResult> {
        let provider = self.provider(connection)?;
        validate_object_prefix(request.prefix.as_deref().unwrap_or_default())?;
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

    /// Read object metadata. Unavailable optional metadata remains `None`.
    ///
    /// # Errors
    /// A missing object is a `NotFound` error, distinct from absent metadata.
    /// Admission and provider/control failures retain their public error axes.
    pub async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ObjectMetadata> {
        let provider = self.provider(connection)?;
        validate_object_key(&request.key)?;
        provider
            .stat(connection, request, &self.context(control))
            .await
    }

    /// Transfer object bytes into a caller-owned sink.
    ///
    /// # Errors
    /// A failed read/write or interruption may leave bytes in the sink. A sink
    /// that accepts part of the transfer and then fails yields
    /// `STORAGE_GET_SINK_PARTIAL` (`partial`, `never`), keeping the category
    /// and phase of the sink failure. Callers
    /// requiring atomic local publication must use staging; this stream API does
    /// not replace or roll back a caller-owned destination.
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
        let provider = self.provider(connection)?;
        validate_object_key(&request.key)?;
        // Every provider writes through this wrapper, so a sink that accepts
        // part of the transfer and then fails is reported the same way by
        // every provider and surface.
        let mut sink = crate::runtime_admission::CountingSink::new(sink);
        let result = provider
            .get(connection, request, &mut sink, &self.context(control))
            .await;
        result.map_err(|error| sink.restate(error, provider.id()))
    }

    /// Consume a source once and publish according to the explicit request policy.
    ///
    /// # Errors
    /// Rejects a declared length above the transfer limit before reading the source.
    /// Buffer bounds and publication guarantees are provider-specific. On a failure
    /// near commit, inspect `remote_effect` and `retry`; do not assume rollback or
    /// replay a partially consumed source automatically.
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
        let provider = self.provider(connection)?;
        validate_object_key(&request.key)?;
        if request
            .content_length
            .is_some_and(|length| length > self.config.max_transfer_bytes)
        {
            return Err(StorageError::new(
                ErrorCategory::ResourceLimit,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "TRANSFER_LIMIT_EXCEEDED",
                "declared upload size exceeds the engine byte limit",
            ));
        }
        provider
            .put(connection, request, source, &self.context(control))
            .await
    }

    /// Delete one object using the caller's explicit missing-object policy.
    ///
    /// # Errors
    /// Missing objects fail unless `ignore_missing` is set. Other provider/control
    /// failures retain their effect and retry disposition, including ambiguous deletion.
    pub async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        control: &ExecutionControl,
    ) -> StorageResult<DeleteResult> {
        let provider = self.provider(connection)?;
        validate_object_key(&request.key)?;
        provider
            .delete(connection, request, &self.context(control))
            .await
    }

    /// Copy within one provider connection using its declared publication guarantees.
    ///
    /// # Errors
    /// Rejects unsafe or equal source/destination keys before mutation. Unsupported
    /// publication policies, size bounds and remote/control failures remain explicit.
    pub async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        control: &ExecutionControl,
    ) -> StorageResult<ObjectMetadata> {
        let provider = self.provider(connection)?;
        validate_object_key(&request.source_key)?;
        validate_object_key(&request.destination_key)?;
        // Held here rather than in each adapter: a self-copy that reaches a
        // filesystem provider opens the destination for truncation while the
        // source is still open, destroying the object it was asked to copy.
        if request.source_key == request.destination_key {
            return Err(StorageError::invalid_configuration(
                "COPY_TARGET_EQUALS_SOURCE",
                "copy source and destination must differ",
            ));
        }
        provider
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
        let token = format!("cursor://{}", hex::encode(digest.finalize()));
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
    Ok(hex::encode(Sha256::digest(encoded)))
}

fn cursor_error(code: &'static str, message: &'static str) -> StorageError {
    StorageError::invalid_configuration(code, message)
}

#[cfg(test)]
#[path = "engine_cursor_tests.rs"]
mod cursor_tests;
