use super::status_error;
use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};

/// Only 401 rejects the credentials.
#[test]
fn only_401_is_an_authentication_failure() {
    let error = status_error(401, false);
    assert_eq!(error.category, ErrorCategory::Authentication);
    assert_eq!(error.retry, RetryDisposition::Never);
    for status in [429, 500, 502, 503, 504] {
        assert_ne!(
            status_error(status, false).category,
            ErrorCategory::Authentication
        );
    }
}

/// A server refusing requests under load is transient: without a mutation
/// nothing happened, so the retry is safe. Before 3.0.0 these were `protocol`
/// with retry `never`.
#[test]
fn transient_refusals_without_mutation_are_safe_to_retry() {
    for status in [429, 502, 503, 504] {
        let error = status_error(status, false);
        assert_eq!(
            (error.category, error.remote_effect, error.retry),
            (
                ErrorCategory::Transient,
                RemoteEffect::None,
                RetryDisposition::Safe
            ),
            "{status}"
        );
    }
}

/// During a mutation the same refusal leaves the remote outcome unknown.
#[test]
fn transient_refusals_during_a_mutation_require_recovery() {
    for status in [429, 503] {
        let error = status_error(status, true);
        assert_eq!(
            (error.category, error.remote_effect, error.retry),
            (
                ErrorCategory::Transient,
                RemoteEffect::Unknown,
                RetryDisposition::RequiresRecovery
            ),
            "{status}"
        );
    }
}

/// Unexpected statuses stay explicit protocol errors.
#[test]
fn unexpected_statuses_are_protocol_errors() {
    for status in [400, 418, 500] {
        let error = status_error(status, false);
        assert_eq!(error.category, ErrorCategory::Protocol, "{status}");
        assert_eq!(error.retry, RetryDisposition::Never);
    }
}
