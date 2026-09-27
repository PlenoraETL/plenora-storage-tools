
use super::*;

#[test]
fn artifact_failures_keep_cause_without_exposing_os_messages() {
    use std::io::ErrorKind;
    for (kind, category) in [
        (ErrorKind::NotFound, ErrorCategory::NotFound),
        (ErrorKind::PermissionDenied, ErrorCategory::Authorization),
        (ErrorKind::AlreadyExists, ErrorCategory::Conflict),
        (ErrorKind::StorageFull, ErrorCategory::ResourceLimit),
        (ErrorKind::QuotaExceeded, ErrorCategory::ResourceLimit),
        (ErrorKind::Other, ErrorCategory::Io),
    ] {
        let os_error = std::io::Error::new(kind, "sentinel-private-path");
        let error = artifact_io_error(&os_error, ErrorPhase::Read, "INPUT_OPEN_FAILED");
        assert_eq!(error.category, category);
        assert_eq!(error.phase, ErrorPhase::Read);
        assert_eq!(error.remote_effect, RemoteEffect::None);
        assert_eq!(error.retry, RetryDisposition::Never);
        assert!(!serde_json::to_string(&error).unwrap().contains("sentinel"));
        let publication = publish_error(&os_error);
        assert_eq!(publication.category, category);
        assert_eq!(publication.phase, ErrorPhase::Commit);
        assert_eq!(publication.remote_effect, RemoteEffect::None);
        assert_eq!(publication.retry, RetryDisposition::Never);
        assert!(
            !serde_json::to_string(&publication)
                .unwrap()
                .contains("sentinel")
        );
        let rolled_back = error.rolled_back();
        assert_eq!(rolled_back.remote_effect, RemoteEffect::RolledBack);
        assert_eq!(rolled_back.category, category);
        if category != ErrorCategory::Io {
            assert_eq!(rolled_back.retry, RetryDisposition::Never);
        }
    }
}
