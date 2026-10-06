use super::*;
use crate::CAPABILITY_NAME;

#[test]
fn runtime_routes_fail_closed_before_invocation() {
    let valid = RuntimeRoute {
        capability_name: CAPABILITY_NAME,
        capability_version: 1,
        operation: "storage.put",
        operation_version: 1,
        input_contract: "plenora-storage-put-input-v1",
        content_type: JSON_CONTENT_TYPE,
        idempotency_key: None,
    };
    assert_eq!(
        validate_runtime_route(valid).map(|item| item.artifact_role),
        Ok(ArtifactRole::Source)
    );
    assert!(
        validate_runtime_route(RuntimeRoute {
            idempotency_key: Some("unsupported"),
            ..valid
        })
        .is_err()
    );
    assert!(
        validate_runtime_route(RuntimeRoute {
            input_contract: "plenora-storage-put-input-v2",
            ..valid
        })
        .is_err()
    );
}
