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
