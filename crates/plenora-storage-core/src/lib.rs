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

pub use capability::{
    CAPABILITY_ATTRIBUTES_CONTRACT, CAPABILITY_NAME, CAPABILITY_SCHEMA_VERSION, COMPONENT_ID,
    CapabilityDocument, CapabilityInterface, CapabilityStatus, ExecutionControls,
    OperationCapability, PayloadCapability, ProviderCapabilities, SideEffect, Surface,
};
pub use control::{CancellationToken, ExecutionControl};
pub use credentials::{CredentialMaterial, CredentialResolver, EnvironmentCredentialResolver};
pub use engine::{Engine, EngineConfig};
pub use error::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};
pub use model::{
    CopyRequest, DeleteRequest, DeleteResult, GetRequest, IntegrityMetadata, ListRequest,
    ListResult, ObjectMetadata, ProviderConnection, PutRequest, StatRequest, TestResult,
    TransferResult,
};
pub use network::validate_network_target;
pub use provider::{OperationContext, StorageProvider};
