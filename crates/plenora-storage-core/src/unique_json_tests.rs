use super::validate_json;
use crate::{
    ProviderConnection, PutRequest, RuntimeInvocation, RuntimeResultEnvelope, StorageError,
};
use serde::de::DeserializeOwned;

const BAD: [&str; 3] = [
    r#"{"PRIVATE_KEY":"PRIVATE_VALUE","PRIVATE_KEY":false}"#,
    r#"{"outer":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}"#,
    r#"{"key":1,"k\u0065y":2}"#,
];

#[test]
fn recursive_preflight_is_redacted_and_checks_the_complete_document() {
    for document in BAD {
        let error = validate_json(document.as_bytes()).unwrap_err();
        assert_eq!(error.code, "JSON_DUPLICATE_MEMBER");
        assert!(!error.to_string().contains("PRIVATE"));
    }
    for document in [
        r#"{"items":[{"key":1},{"key":2}],"x":null}"#,
        "1",
        "true",
        "-2",
        "1.5",
    ] {
        validate_json(document.as_bytes()).unwrap();
    }
    for document in ["{} {}", "{", "NaN"] {
        assert_eq!(
            validate_json(document.as_bytes()).unwrap_err().code,
            "JSON_INVALID"
        );
    }
}

fn check_field<T: DeserializeOwned>(field: &str, rest: &str, clean: &str) {
    for value in BAD {
        let document = format!("{{\"{field}\":{value},{rest}}}");
        let error = serde_json::from_str::<T>(&document)
            .err()
            .unwrap()
            .to_string();
        assert!(error.contains("duplicate members"), "{error}");
        assert!(!error.contains("PRIVATE"));
    }
    let document = format!("{{\"{field}\":{clean},{rest}}}");
    assert!(serde_json::from_str::<T>(&document).is_ok());
}

#[test]
fn direct_serde_value_and_map_fields_reject_before_schema_conversion() {
    check_field::<ProviderConnection>(
        "config",
        r#""provider":"local","config_contract":"local","credential_ref":"env:TEST""#,
        "{}",
    );
    check_field::<PutRequest>(
        "metadata",
        r#""key":"key","overwrite":false,"publication_policy":"atomic_required""#,
        r#"{"key":"value"}"#,
    );
    check_field::<StorageError>(
        "details",
        r#""category":"internal","phase":"prepare","remote_effect":"unknown","retry":{"kind":"requires_recovery"},"code":"TEST","message":"test""#,
        "{}",
    );
    check_field::<RuntimeInvocation>(
        "payload",
        r#""content_type":"application/json","metadata":{"plenora.message.id":"id","plenora.capability.name":"name","plenora.capability.version":"1","plenora.capability.operation":"test","plenora.operation.version":"1","plenora.input.contract":"input","plenora.trace.correlation_id":"id"}"#,
        "{}",
    );
    check_field::<RuntimeResultEnvelope>(
        "payload",
        r#""content_type":"application/json","metadata":{"plenora.message.id":"id","plenora.output.contract":"output"}"#,
        "{}",
    );
}
