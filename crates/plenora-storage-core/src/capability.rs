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
const PYTHON_INTERFACE_CONTRACT: &str = "plenora-python-sdk-v1";

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
    PythonSdk,
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
    Available,
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
    pub fn new(surfaces: &[Surface], providers: Vec<ProviderCapabilities>) -> Self {
        let interfaces = surfaces.iter().copied().map(interface).collect::<Vec<_>>();
        let provider_value = serde_json::to_value(&providers).unwrap_or_else(|_| json!([]));
        let operations = [
            ("storage.test", SideEffect::None),
            ("storage.list", SideEffect::None),
            ("storage.stat", SideEffect::None),
            ("storage.get", SideEffect::None),
            ("storage.put", SideEffect::Remote),
            ("storage.copy", SideEffect::Remote),
            ("storage.delete", SideEffect::Remote),
        ]
        .into_iter()
        .map(|(id, side_effect)| operation(id, side_effect, surfaces, provider_value.clone()))
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
        Surface::PythonSdk => (PYTHON_INTERFACE_CONTRACT, 1, "plenora-storage"),
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
    surfaces: &[Surface],
    providers: Value,
) -> OperationCapability {
    let action = id.strip_prefix("storage.").unwrap_or(id);
    OperationCapability {
        id: id.to_owned(),
        version: 1,
        status: CapabilityStatus::Available,
        surfaces: surfaces.to_vec(),
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
            (
                "transfer".to_owned(),
                json!({"streaming": matches!(action, "get" | "put"), "integrity": "sha256"}),
            ),
        ]),
    }
}

#[cfg(test)]
mod tests {
    use super::{CapabilityDocument, Surface};

    #[test]
    fn catalog_has_the_seven_initial_operations() {
        let document = CapabilityDocument::new(&[Surface::Rust], Vec::new());
        assert_eq!(document.operations.len(), 7);
        assert!(
            document
                .operations
                .iter()
                .all(|operation| operation.controls.cancellation
                    && operation.controls.deadline
                    && !operation.controls.idempotency_key)
        );
    }
}
