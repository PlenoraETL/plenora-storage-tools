use super::load_connection;

#[tokio::test]
async fn connection_file_rejects_duplicates_before_provider_validation() {
    let path = std::env::temp_dir().join(format!("storage-duplicate-{}.json", std::process::id()));
    for document in [
        r#"{"provider":"PRIVATE_VALUE","provider":"local"}"#,
        r#"{"config":{"outer":[{"PRIVATE_KEY":1,"PRIVATE_KEY":2}]}}"#,
    ] {
        std::fs::write(&path, document).unwrap();
        let error = load_connection(&path).await.unwrap_err();
        assert_eq!(error.code, "JSON_DUPLICATE_MEMBER");
        assert!(!error.to_string().contains("PRIVATE"));
    }
    std::fs::write(
        &path,
        r#"{"provider":"local","config_contract":"local","config":{},"credential_ref":"env:TEST"}"#,
    )
    .unwrap();
    assert!(load_connection(&path).await.is_ok());
    std::fs::remove_file(path).unwrap();
}
