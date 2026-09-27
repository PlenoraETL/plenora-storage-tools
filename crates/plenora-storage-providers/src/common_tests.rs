use super::*;

#[test]
fn exhausted_storage_preserves_uncertain_mutation_effect() {
    for kind in [
        std::io::ErrorKind::StorageFull,
        std::io::ErrorKind::QuotaExceeded,
    ] {
        let error = io_error(
            &std::io::Error::new(kind, "sentinel-private-path"),
            ErrorPhase::Write,
            true,
        );
        assert_eq!(error.category, ErrorCategory::ResourceLimit);
        assert_eq!(error.remote_effect, RemoteEffect::Unknown);
        assert_eq!(error.retry, RetryDisposition::RequiresRecovery);
        assert!(!serde_json::to_string(&error).unwrap().contains("sentinel"));
        let recovered = error.rolled_back();
        assert_eq!(recovered.remote_effect, RemoteEffect::RolledBack);
        assert_eq!(recovered.retry, RetryDisposition::Never);
    }
}
