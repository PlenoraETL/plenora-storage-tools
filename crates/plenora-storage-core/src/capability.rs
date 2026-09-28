use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// Stable Storage Tools component identifier.
pub const COMPONENT_ID: &str = "plenora-storage-tools";
/// Public storage capability selector.
pub const CAPABILITY_NAME: &str = "plenora.storage-tools";
/// Version of the capability discovery document.
pub const CAPABILITY_SCHEMA_VERSION: u32 = 2;
/// Contract for storage-specific discovery attributes.
pub const CAPABILITY_ATTRIBUTES_CONTRACT: &str = "plenora-storage-capability-attributes-v1";

const RUST_INTERFACE_CONTRACT: &str = "plenora-rust-public-v1";
const CLI_INTERFACE_CONTRACT: &str = "plenora-cli-v2";
const RUNTIME_INTERFACE_CONTRACT: &str = "plenora-runtime-binding-v1";

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
/// Discovery document for the operations and interfaces compiled into this build.
pub struct CapabilityDocument {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Stable component identity, independent of the package version.
    pub component: String,
    /// Version of the compiled Storage Tools package.
    pub component_version: String,
    /// Entry points exposed by this build for the selected surface.
    pub interfaces: Vec<CapabilityInterface>,
    /// Operations supported by the compiled provider or capability.
    pub operations: Vec<OperationCapability>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
/// One public entry point and its versioned contract.
pub struct CapabilityInterface {
    /// Consumer surface described by this interface.
    pub kind: Surface,
    /// Versioned public contract identifier.
    pub contract: String,
    /// Version of the interface or operation contract.
    pub version: u32,
    /// Distribution artifact or runtime resource associated with this result.
    pub artifact: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Consumer entry point selected for capability discovery.
pub enum Surface {
    /// Native Rust library interface.
    Rust,
    /// Command-line JSON interface.
    Cli,
    /// Typed synchronous and asynchronous Python SDK.
    PythonSdk,
    /// Transport-neutral runtime binding.
    Runtime,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
/// Discovery record for one operation and its execution constraints.
pub struct OperationCapability {
    /// Stable operation identifier used for dispatch.
    pub id: String,
    /// Version of the interface or operation contract.
    pub version: u32,
    /// Availability declaration; it does not replace deployment qualification.
    pub status: CapabilityStatus,
    /// Consumer entry points through which the operation is exposed.
    pub surfaces: Vec<Surface>,
    /// Accepted request contract and content types.
    pub input: PayloadCapability,
    /// Successful response contract and content types.
    pub output: PayloadCapability,
    /// Whether execution can change external state.
    pub side_effect: SideEffect,
    /// Execution controls accepted by this operation.
    pub controls: ExecutionControls,
    /// Provider-specific capability details; not a proof of deployment compatibility.
    pub attributes: BTreeMap<String, Value>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Declared availability of an operation on the selected surface.
pub enum CapabilityStatus {
    /// Available in this build under the documented provider restrictions.
    Available,
    /// Exposed for evaluation without a stable compatibility promise.
    Experimental,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
/// Versioned payload identity and permitted media types.
pub struct PayloadCapability {
    /// Versioned public contract identifier.
    pub contract: String,
    /// Media types accepted by the payload contract.
    pub content_types: Vec<String>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Whether an operation can mutate external state.
pub enum SideEffect {
    /// Does not mutate external state.
    None,
    /// May change provider or host-owned artifact state.
    Remote,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
/// Control capabilities advertised to a consumer.
pub struct ExecutionControls {
    /// Cooperative cancellation support.
    pub cancellation: bool,
    /// Deadline support.
    pub deadline: bool,
    /// Idempotency key support; false means the control must be rejected.
    pub idempotency_key: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
/// Provider identity, operations and backend-specific guarantees.
pub struct ProviderCapabilities {
    /// Stable provider identifier used for dispatch and capability discovery.
    pub provider: String,
    /// Versioned provider connection contract; must match the selected adapter.
    pub config_contract: String,
    /// Operations supported by the compiled provider or capability.
    pub operations: Vec<String>,
    /// Provider-specific capability details; not a proof of deployment compatibility.
    pub attributes: BTreeMap<String, String>,
}

impl CapabilityDocument {
    #[must_use]
    /// Build discovery for one surface and its compiled provider inventory.
    #[allow(
        clippy::needless_pass_by_value,
        reason = "Preserve the public owned inventory constructor"
    )]
    pub fn new(surface: Surface, providers: Vec<ProviderCapabilities>) -> Self {
        let interfaces = vec![interface(surface)];
        let operations = [
            ("storage.test", SideEffect::None),
            ("storage.list", SideEffect::None),
            ("storage.stat", SideEffect::None),
            // The artifact sink can be externally visible even though the
            // storage-side access itself is read-only.
            ("storage.get", SideEffect::Remote),
            ("storage.put", SideEffect::Remote),
            ("storage.copy", SideEffect::Remote),
            ("storage.delete", SideEffect::Remote),
        ]
        .into_iter()
        .filter_map(|(id, side_effect)| {
            let action = id.strip_prefix("storage.").unwrap_or(id);
            let supporting_providers = providers
                .iter()
                .filter(|provider| {
                    provider
                        .operations
                        .iter()
                        .any(|operation| operation == action)
                })
                .cloned()
                .collect::<Vec<_>>();
            if supporting_providers.is_empty() {
                None
            } else {
                let provider_value =
                    serde_json::to_value(supporting_providers).unwrap_or_else(|_| json!([]));
                Some(operation(id, side_effect, surface, provider_value))
            }
        })
        .collect();
        Self {
            schema_version: CAPABILITY_SCHEMA_VERSION,
            component: COMPONENT_ID.to_owned(),
            component_version: env!("CARGO_PKG_VERSION").to_owned(),
            interfaces,
            operations,
        }
    }
}

fn interface(surface: Surface) -> CapabilityInterface {
    let (contract, version, artifact) = match surface {
        Surface::Rust => (RUST_INTERFACE_CONTRACT, 1, "plenora-storage-core"),
        Surface::Cli => (CLI_INTERFACE_CONTRACT, 2, "plenora-storage"),
        Surface::PythonSdk => ("plenora-python-sdk-v1", 1, "plenora-storage"),
        Surface::Runtime => (RUNTIME_INTERFACE_CONTRACT, 1, CAPABILITY_NAME),
    };
    CapabilityInterface {
        kind: surface,
        contract: contract.to_owned(),
        version,
        artifact: artifact.to_owned(),
    }
}

fn operation(
    id: &str,
    side_effect: SideEffect,
    surface: Surface,
    providers: Value,
) -> OperationCapability {
    let action = id.strip_prefix("storage.").unwrap_or(id);
    OperationCapability {
        id: id.to_owned(),
        version: 1,
        status: CapabilityStatus::Available,
        surfaces: vec![surface],
        input: PayloadCapability {
            contract: format!("plenora-storage-{action}-input-v1"),
            content_types: vec!["application/json".to_owned()],
        },
        output: PayloadCapability {
            contract: format!("plenora-storage-{action}-output-v1"),
            content_types: vec!["application/json".to_owned()],
        },
        side_effect,
        controls: ExecutionControls {
            cancellation: true,
            deadline: true,
            idempotency_key: false,
        },
        attributes: BTreeMap::from([
            (
                "contract".to_owned(),
                Value::String(CAPABILITY_ATTRIBUTES_CONTRACT.to_owned()),
            ),
            ("providers".to_owned(), providers),
            ("transfer".to_owned(), transfer_attributes(action, surface)),
        ]),
    }
}

fn transfer_attributes(action: &str, surface: Surface) -> Value {
    let mode = match (action, surface) {
        ("get", Surface::Rust) => "streaming_sink",
        ("put", Surface::Rust) => "streaming_source",
        ("get", Surface::Cli | Surface::PythonSdk) => "local_file_sink",
        ("put", Surface::Cli | Surface::PythonSdk) => "local_file_source",
        ("get", Surface::Runtime) => "runtime_artifact_sink",
        ("put", Surface::Runtime) => "runtime_artifact_source",
        _ => "none",
    };
    json!({
        "mode": mode,
        "integrity": if matches!(action, "get" | "put") { "sha256" } else { "none" }
    })
}

#[cfg(test)]
#[path = "capability_tests.rs"]
mod tests;
