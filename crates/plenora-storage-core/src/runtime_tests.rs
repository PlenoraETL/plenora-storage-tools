use super::*;
use crate::CAPABILITY_NAME;

#[test]
fn result_limit_counts_json_bytes_and_never_truncates() {
    let payload = serde_json::json!({"value": "\"\né"});
    let before = payload.clone();
    let size = serde_json::to_vec(&payload).unwrap().len();
    let descriptor = &RUNTIME_OPERATIONS[1];
    assert!(result_limit::check(&payload, descriptor, size).is_ok());
    let error = result_limit::check(&payload, descriptor, size - 1).unwrap_err();
    assert_eq!(error.category, ErrorCategory::ResourceLimit);
    assert_eq!(error.phase, ErrorPhase::Read);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(error.retry, RetryDisposition::Never);
    assert_eq!(payload, before);
}

#[test]
fn result_limit_preserves_completed_mutations_and_unproven_sink_publication() {
    for (index, payload, effect, retry) in [
        (
            3,
            serde_json::json!({}),
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
        ),
        (
            4,
            serde_json::json!({}),
            RemoteEffect::Committed,
            RetryDisposition::Never,
        ),
        (
            5,
            serde_json::json!({}),
            RemoteEffect::Committed,
            RetryDisposition::Never,
        ),
        (
            6,
            serde_json::json!({"deleted": true}),
            RemoteEffect::Committed,
            RetryDisposition::Never,
        ),
        (
            6,
            serde_json::json!({"deleted": false}),
            RemoteEffect::None,
            RetryDisposition::Never,
        ),
    ] {
        let error = result_limit::check(&payload, &RUNTIME_OPERATIONS[index], 0).unwrap_err();
        assert_eq!(error.category, ErrorCategory::ResourceLimit);
        assert_eq!(error.remote_effect, effect);
        assert_eq!(error.retry, retry);
        if index == 3 || index == 6 {
            assert_eq!(error.phase, ErrorPhase::Commit);
        }
        assert!(!error.message.contains("deleted"));
    }
}

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
