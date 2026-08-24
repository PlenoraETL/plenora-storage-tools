use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const COMPONENT_ID: &str = "plenora-storage-tools";
pub const CAPABILITY_NAME: &str = "plenora.storage-tools";
pub const CAPABILITY_SCHEMA_VERSION: u32 = 2;
pub const CAPABILITY_ATTRIBUTES_CONTRACT: &str = "plenora-storage-capability-attributes-v1";

const RUST_INTERFACE_CONTRACT: &str = "plenora-rust-public-v1";
const CLI_INTERFACE_CONTRACT: &str = "plenora-cli-v2";
const RUNTIME_INTERFACE_CONTRACT: &str = "plenora-runtime-binding-v1";

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct CapabilityDocument {
    pub schema_version: u32,
    pub component: String,
    pub component_version: String,
    pub interfaces: Vec<CapabilityInterface>,
    pub operations: Vec<OperationCapability>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct CapabilityInterface {
    pub kind: Surface,
    pub contract: String,
    pub version: u32,
    pub artifact: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Surface {
    Rust,
    Cli,
    Runtime,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct OperationCapability {
    pub id: String,
    pub version: u32,
    pub status: CapabilityStatus,
    pub surfaces: Vec<Surface>,
    pub input: PayloadCapability,
    pub output: PayloadCapability,
    pub side_effect: SideEffect,
    pub controls: ExecutionControls,
    pub attributes: BTreeMap<String, Value>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityStatus {
    Experimental,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct PayloadCapability {
    pub contract: String,
    pub content_types: Vec<String>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SideEffect {
    None,
    Remote,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
pub struct ExecutionControls {
    pub cancellation: bool,
    pub deadline: bool,
    pub idempotency_key: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ProviderCapabilities {
    pub provider: String,
    pub config_contract: String,
    pub operations: Vec<String>,
    pub attributes: BTreeMap<String, String>,
}

impl CapabilityDocument {
    #[must_use]
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
        status: CapabilityStatus::Experimental,
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
        ("get", Surface::Cli) => "local_file_sink",
        ("put", Surface::Cli) => "local_file_source",
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
mod tests {
    use std::collections::BTreeMap;

    use super::{CapabilityDocument, ProviderCapabilities, Surface};

    #[test]
    fn catalog_has_the_seven_initial_operations() {
        let document = CapabilityDocument::new(
            Surface::Rust,
            vec![ProviderCapabilities {
                provider: "test".to_owned(),
                config_contract: "plenora-storage-test-connection-v1".to_owned(),
                operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                    .map(str::to_owned)
                    .to_vec(),
                attributes: BTreeMap::new(),
            }],
        );
        assert_eq!(document.operations.len(), 7);
        assert!(document.operations.iter().all(|operation| {
            operation.status == super::CapabilityStatus::Experimental
                && operation.surfaces == [Surface::Rust]
        }));
        assert!(
            document
                .operations
                .iter()
                .all(|operation| operation.controls.cancellation
                    && operation.controls.deadline
                    && !operation.controls.idempotency_key)
        );
    }

    #[test]
    fn catalog_omits_operations_without_a_registered_provider() {
        let document = CapabilityDocument::new(Surface::Rust, Vec::new());
        assert!(document.operations.is_empty());
    }
}
