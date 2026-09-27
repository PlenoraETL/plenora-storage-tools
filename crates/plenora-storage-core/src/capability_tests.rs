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
        operation.status == super::CapabilityStatus::Available
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
