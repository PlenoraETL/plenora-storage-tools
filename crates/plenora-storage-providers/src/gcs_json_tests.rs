use super::json;
use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect};

#[tokio::test]
async fn remote_json_rejects_duplicates_before_typed_response_decoding() {
    for document in [
        r#"{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}"#,
        r#"{"items":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}"#,
    ] {
        let response = reqwest::Response::from(::http::Response::new(document.to_owned()));
        let error = json::<serde_json::Value>(response).await.unwrap_err();
        assert_eq!(error.code, "JSON_DUPLICATE_MEMBER");
        assert_eq!(error.category, ErrorCategory::Protocol);
        assert_eq!(error.phase, ErrorPhase::Read);
        assert_eq!(error.remote_effect, RemoteEffect::None);
        assert!(!error.to_string().contains("PRIVATE"));
    }
    let response = reqwest::Response::from(::http::Response::new(
        r#"{"items":[{"key":1},{"key":2}]}"#.to_owned(),
    ));
    assert!(json::<serde_json::Value>(response).await.is_ok());
}
