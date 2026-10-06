use super::smb_error;
use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};

#[test]
fn an_internal_smb_failure_is_reported_as_internal_not_as_io() {
    let error = smb_error(
        &smb2::Error::Internal {
            what: "connection waiter map",
        },
        false,
    );
    assert_eq!(error.category, ErrorCategory::Internal);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(error.retry, RetryDisposition::Never);
}

#[test]
fn an_internal_failure_while_mutating_keeps_the_effect_unknown() {
    let error = smb_error(
        &smb2::Error::Internal {
            what: "connection crypto state",
        },
        true,
    );
    assert_eq!(error.category, ErrorCategory::Internal);
    assert_eq!(error.remote_effect, RemoteEffect::Unknown);
    assert_eq!(error.retry, RetryDisposition::RequiresRecovery);
}
