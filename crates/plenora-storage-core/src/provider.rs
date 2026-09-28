use async_trait::async_trait;
use tokio::io::{AsyncRead, AsyncWrite};

use crate::{
    CopyRequest, DeleteRequest, DeleteResult, EngineConfig, ExecutionControl, GetRequest,
    ObjectMetadata, ProviderCapabilities, ProviderConnection, ProviderListRequest,
    ProviderListResult, PutRequest, StatRequest, StorageResult, TestResult, TransferResult,
};

/// Borrowed host policy and controls for one operation; adapters must honor both.
pub struct OperationContext<'a> {
    /// Borrowed engine admission policy and resource limits for this operation.
    pub policy: &'a EngineConfig,
    /// Borrowed cancellation token and deadline to enforce across all phases.
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
    /// Return the stable identifier used to select this adapter.
    fn id(&self) -> &'static str;
    /// Return the connection contract accepted by this adapter.
    fn config_contract(&self) -> &'static str;
    /// Describe only supported operations and enforceable backend guarantees.
    fn capabilities(&self) -> ProviderCapabilities;

    /// Performs local provider admission before a caller opens an artifact sink.
    /// Implementations must not resolve credentials or perform network I/O here.
    ///
    /// # Errors
    /// Returns a configuration or policy error before credential resolution, network I/O or artifact opening.
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        _policy: &EngineConfig,
    ) -> StorageResult<()> {
        connection.validate()
    }

    /// Probe the admitted connection without writing an object.
    ///
    /// # Errors
    /// Returns connection, authentication, probe or execution-control failures.
    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult>;

    /// Enumerate a bounded page in normalized key order.
    ///
    /// # Errors
    /// Returns validation, connection, listing or budget errors; invalid remote names fail closed.
    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult>;

    /// Read metadata without treating `ETag` or version as a content digest.
    ///
    /// # Errors
    /// Returns validation, connection, not-found or metadata read errors.
    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata>;

    /// Writes the object and flushes the caller's sink before returning success.
    /// The adapter does not close the sink or promise filesystem durability.
    ///
    /// # Errors
    /// Write/flush failures and interruption after bytes were accepted must retain
    /// an ambiguous artifact effect; caller-owned sinks cannot be rolled back here.
    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult>;

    /// Consume a source under the admitted publication and size policies.
    ///
    /// # Errors
    /// Source, write, commit and cleanup errors preserve their provable external effect; lost replies do not imply rollback.
    async fn put(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult>;

    /// Delete the selected key, honoring the explicit missing-object policy.
    ///
    /// # Errors
    /// Returns admission, connection or deletion failures; an interrupted deletion can have unknown effect.
    async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult>;

    /// Copy within the same connection under the destination publication policy.
    ///
    /// # Errors
    /// Unsupported guarantees fail before mutation; later failures preserve commit and recovery state.
    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata>;
}
