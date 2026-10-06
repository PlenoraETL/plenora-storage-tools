use super::*;

#[test]
fn result_identities_are_random_version_4_uuids() {
    let first = new_result_message_id().expect("random source");
    let second = new_result_message_id().expect("random source");
    assert_ne!(first, second);
    for id in [&first, &second] {
        assert!(canonical_uuid(id), "{id}");
        assert_eq!(&id[14..15], "4", "{id}");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"), "{id}");
    }
}

/// An unavailable random source is a typed error, never a panic or an empty
/// or fixed identity.
#[test]
fn an_unavailable_random_source_is_a_typed_error() {
    let error = result_message_id_from(|_| Err(getrandom::Error::UNSUPPORTED))
        .expect_err("no identity without a random source");
    assert_eq!(error.code, "RUNTIME_RESULT_IDENTITY_UNAVAILABLE");
    assert_eq!(error.category, ErrorCategory::Internal);
    assert_eq!(error.phase, ErrorPhase::Validate);
    assert_eq!(error.remote_effect, RemoteEffect::None);
}

/// A sink that accepted a prefix and then failed restates the failure as a
/// partial transfer, keeping its category and phase: a full disk stays a
/// `resource_limit`, so a caller that rolls the sink back never retries it.
#[test]
fn a_partial_sink_failure_keeps_its_cause() {
    let sink = CountingSink {
        inner: (),
        delivered: 5,
        failed: true,
    };
    let full = StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Write,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "LOCAL_DISK_FULL",
        "local disk is full",
    );
    let restated = sink.restate(full, "local");
    assert_eq!(restated.code, "STORAGE_GET_SINK_PARTIAL");
    assert_eq!(restated.category, ErrorCategory::ResourceLimit);
    assert_eq!(restated.phase, ErrorPhase::Write);
    assert_eq!(restated.remote_effect, RemoteEffect::Partial);
    assert_eq!(restated.retry, RetryDisposition::Never);
    assert_eq!(restated.provider.as_deref(), Some("local"));
    assert_eq!(restated.rolled_back().retry, RetryDisposition::Never);

    // Without a delivered prefix, or without a sink failure, the provider's
    // error is unchanged.
    for (delivered, failed) in [(0, true), (5, false)] {
        let sink = CountingSink {
            inner: (),
            delivered,
            failed,
        };
        let error = StorageError::unsupported("unchanged");
        assert_eq!(sink.restate(error.clone(), "local"), error);
    }
}
