use super::{optional_prefix_path, required_path};

#[test]
fn object_keys_are_relative_and_normalized() {
    assert!(required_path("folder/object.bin").is_ok());
    assert!(required_path("").is_err());
    assert!(required_path(&"x".repeat(4_097)).is_err());
    assert!(required_path("../secret").is_err());
    assert!(required_path("folder//object.bin").is_err());
    assert!(required_path("folder/./object.bin").is_err());
    assert!(required_path("folder/").is_err());
}

#[test]
fn an_empty_prefix_means_the_whole_namespace() {
    assert_eq!(optional_prefix_path(Some("")).expect("empty prefix"), None);
    assert!(
        optional_prefix_path(Some("incoming/"))
            .expect("prefix")
            .is_some()
    );
    assert!(optional_prefix_path(Some("/absolute")).is_err());
    assert!(optional_prefix_path(Some("../secret")).is_err());
}

/// Case 9e of the common runtime matrix: a proved publication whose metadata
/// cannot be read back is `committed` in `cleanup` and is never retried,
/// because repeating the request would publish again.
#[test]
fn unavailable_metadata_after_publication_is_committed_and_never_retried() {
    let error = super::committed_verification_error();
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::Committed
    );
    assert_eq!(error.phase, plenora_storage_core::ErrorPhase::Cleanup);
    assert_eq!(error.retry, plenora_storage_core::RetryDisposition::Never);
}
