use async_trait::async_trait;
use tokio::io::{AsyncRead, AsyncWrite};

use crate::{
    CopyRequest, DeleteRequest, DeleteResult, EngineConfig, ExecutionControl, GetRequest,
    ObjectMetadata, ProviderCapabilities, ProviderConnection, ProviderListRequest,
    ProviderListResult, PutRequest, StatRequest, StorageResult, TestResult, TransferResult,
};

/// Borrowed host policy and controls for one operation; adapters must honor both.
pub struct OperationContext<'a> {
    pub policy: &'a EngineConfig,
    pub control: &'a ExecutionControl,
}

/// Adapter contract shared by Rust, CLI and Python operations.
///
/// Capabilities describe proven behavior. Implementations must reject unsupported
/// publication policies before effects, preserve caller controls, and return
/// redacted errors with the provable remote effect. Neither the engine nor an
/// adapter may infer rollback merely because a commit response was lost.
#[async_trait]
pub trait StorageProvider: Send + Sync {
    fn id(&self) -> &'static str;
    fn config_contract(&self) -> &'static str;
    fn capabilities(&self) -> ProviderCapabilities;

    /// Performs local provider admission before a caller opens an artifact sink.
    /// Implementations must not resolve credentials or perform network I/O here.
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        _policy: &EngineConfig,
    ) -> StorageResult<()> {
        connection.validate()
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult>;

    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult>;

    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata>;

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult>;

    async fn put(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult>;

    async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult>;

    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata>;
}
