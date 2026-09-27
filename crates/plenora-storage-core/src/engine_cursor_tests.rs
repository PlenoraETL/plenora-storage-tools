
use super::*;

fn connection() -> ProviderConnection {
    ProviderConnection {
        provider: "test".to_owned(),
        config_contract: "plenora-storage-test-connection-v1".to_owned(),
        config: serde_json::json!({"scope": "a"}),
        credential_ref: "secret://storage/test".to_owned(),
    }
}

fn request() -> ListRequest {
    ListRequest {
        prefix: Some("prefix/".to_owned()),
        cursor: None,
        max_items: Some(10),
    }
}

#[test]
fn cursor_duration_close_and_eviction_are_fail_closed() {
    assert_eq!(LIST_CURSOR_TTL_SECONDS, 15 * 60);
    let engine = Engine::new(EngineConfig::default());
    let connection = connection();
    let request = request();

    let expired = engine
        .issue_cursor(&connection, &request, "expired")
        .expect("issue cursor");
    assert!(expired.len() <= LIST_CURSOR_MAX_BYTES);
    engine
        .cursors
        .lock()
        .expect("cursor lock")
        .get_mut(&expired)
        .expect("cursor state")
        .expires_at = Instant::now();
    assert_eq!(
        engine
            .resolve_cursor(&expired, &connection, &request)
            .expect_err("expired cursor must fail")
            .code,
        "LIST_CURSOR_INVALID_OR_EXPIRED"
    );

    let oldest = engine
        .issue_cursor(&connection, &request, "oldest")
        .expect("issue oldest");
    for index in 0..LIST_CURSOR_MAX_ACTIVE {
        engine
            .issue_cursor(&connection, &request, &format!("key-{index}"))
            .expect("issue cursor for eviction");
    }
    assert_eq!(
        engine
            .resolve_cursor(&oldest, &connection, &request)
            .expect_err("oldest cursor must be evicted")
            .code,
        "LIST_CURSOR_INVALID_OR_EXPIRED"
    );

    let closed = engine
        .issue_cursor(&connection, &request, "closed")
        .expect("issue cursor before close");
    engine.close();
    assert_eq!(
        engine
            .resolve_cursor(&closed, &connection, &request)
            .expect_err("close must invalidate cursors")
            .code,
        "LIST_CURSOR_INVALID_OR_EXPIRED"
    );
}
