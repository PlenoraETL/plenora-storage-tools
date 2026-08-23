use std::{
    collections::{BTreeMap, btree_map::Entry},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use tokio::io::{AsyncRead, AsyncWrite};

use crate::{
    CapabilityDocument, CopyRequest, DeleteRequest, DeleteResult, ExecutionControl, GetRequest,
    ListRequest, ListResult, ObjectMetadata, OperationContext, ProviderConnection, PutRequest,
    StatRequest, StorageError, StorageProvider, StorageResult, Surface, TestResult, TransferResult,
};

#[derive(Clone, Debug)]
// Each flag is an independent, fail-closed host authorization. Keeping them
// explicit prevents enabling one insecure transport from enabling another.
#[allow(clippy::struct_excessive_bools)]
pub struct EngineConfig {
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
}

impl Engine {
    #[must_use]
    pub fn new(config: EngineConfig) -> Self {
        Self {
            config,
            providers: BTreeMap::new(),
            closed: AtomicBool::new(false),
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
        self.capabilities_for(&[Surface::Rust])
    }

    #[must_use]
    pub fn capabilities_for(&self, surfaces: &[Surface]) -> CapabilityDocument {
        let providers = self
            .providers
            .values()
            .map(|provider| provider.capabilities())
            .collect();
        CapabilityDocument::new(surfaces, providers)
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn provider(&self, connection: &ProviderConnection) -> StorageResult<&dyn StorageProvider> {
        if self.is_closed() {
            return Err(StorageError::engine_closed());
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
        self.provider(connection)?
            .list(connection, request, &self.context(control))
            .await
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
}
