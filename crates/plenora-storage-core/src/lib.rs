//! Provider-neutral public boundary for Plenora storage operations.

#![forbid(unsafe_code)]

mod capability;
mod control;
mod credentials;
mod engine;
mod error;
mod model;
mod network;
mod provider;
mod runtime;

pub use capability::{
    CAPABILITY_ATTRIBUTES_CONTRACT, CAPABILITY_NAME, CAPABILITY_SCHEMA_VERSION, COMPONENT_ID,
    CapabilityDocument, CapabilityInterface, CapabilityStatus, ExecutionControls,
    OperationCapability, PayloadCapability, ProviderCapabilities, SideEffect, Surface,
};
pub use control::{CancellationToken, ExecutionControl};
pub use credentials::{CredentialMaterial, CredentialResolver, EnvironmentCredentialResolver};
pub use engine::{
    Engine, EngineConfig, LIST_CURSOR_MAX_ACTIVE, LIST_CURSOR_MAX_BYTES, LIST_CURSOR_TTL_SECONDS,
};
pub use error::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};
pub use model::{
    ArtifactMetadata, ArtifactReference, ArtifactSinkReference, CopyInput, CopyRequest,
    DeleteInput, DeleteRequest, DeleteResult, GetInput, GetRequest, IntegrityMetadata, ListInput,
    ListRequest, ListResult, OPERATION_SCHEMA_VERSION, ObjectMetadata, ProviderConnection,
    ProviderListRequest, ProviderListResult, PublicationPolicy, PutInput, PutRequest, StatInput,
    StatRequest, TestInput, TestResult, TransferResult, validate_operation_schema_version,
};
pub use network::validate_network_target;
pub use provider::{OperationContext, StorageProvider};
pub use runtime::{
    ArtifactResolver, ArtifactRole, ArtifactSink, ArtifactSource, ERROR_CONTENT_TYPE,
    ERROR_CONTRACT, JSON_CONTENT_TYPE, RUNTIME_BINDING_VERSION, RUNTIME_OPERATIONS, RuntimeBinding,
    RuntimeInvocation, RuntimeOperationDescriptor, RuntimeRequestMetadata, RuntimeResultEnvelope,
    RuntimeResultMetadata, RuntimeRoute, SecretResolver, validate_runtime_route,
};
