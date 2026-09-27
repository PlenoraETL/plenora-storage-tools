use super::*;

#[test]
fn unrelated_xml_is_not_an_empty_listing() {
    let root = Url::parse("https://fixture.invalid/storage/").unwrap();
    for data in [
        b"<html/>".as_slice(),
        b"<Error><Message>private</Message></Error>",
    ] {
        assert!(parse_properties(&root, data).is_err());
    }
}

#[test]
fn namespaced_properties_preserve_metadata_and_reject_escaping_hrefs() {
    let root = Url::parse("https://fixture.invalid/storage/").unwrap();
    let document = |href: &str| {
        format!(
            "<d:multistatus xmlns:d=\"DAV:\"><d:response><d:href>{href}</d:href><d:propstat><d:prop><d:getcontentlength>123</d:getcontentlength><d:getetag>\"abc\"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
        )
    };
    let entries = parse_properties(&root, document("/storage/a%20b").as_bytes()).unwrap();
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].key, "a b");
    assert_eq!(entries[0].size, 123);
    assert_eq!(entries[0].etag.as_deref(), Some("\"abc\""));
    for href in [
        "https://other.invalid/storage/private",
        "/storage/../private",
        "/storage/%2E%2E/private",
        "/storage/a?private",
        "/storage/a#private",
    ] {
        let error = parse_properties(&root, document(href).as_bytes())
            .err()
            .unwrap();
        assert!(!error.message.contains("private"));
        assert!(error.details.is_empty());
    }
}
