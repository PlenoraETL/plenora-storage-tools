use super::decode;

#[test]
fn every_native_input_is_checked_before_typed_decoding() {
    for document in [
        r#"{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}"#,
        r#"{"config":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}"#,
    ] {
        let error = decode::<serde_json::Value>(document).unwrap_err();
        assert_eq!(error.code, "JSON_DUPLICATE_MEMBER");
        assert!(!error.to_string().contains("PRIVATE"));
    }
    assert!(decode::<serde_json::Value>(r#"{"items":[{"key":1},{"key":2}]}"#).is_ok());
}
