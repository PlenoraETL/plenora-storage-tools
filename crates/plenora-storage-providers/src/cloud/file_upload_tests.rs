use super::*;

#[test]
fn signing_binds_exclusive_condition_and_emulator_path_without_zero_length() {
    let url = Url::parse("http://fixture.invalid/account/container/object%20key").unwrap();
    let mut headers = HeaderMap::new();
    insert(&mut headers, "content-length", "0").unwrap();
    insert(&mut headers, "x-ms-version", "2023-11-03").unwrap();
    insert(&mut headers, "x-ms-date", "Tue, 29 Sep 2026 00:00:00 GMT").unwrap();
    insert(&mut headers, "if-none-match", "*").unwrap();
    let signed = canonical("account", &url, &headers).unwrap();
    let lines: Vec<_> = signed.lines().collect();
    assert_eq!(lines[0], "PUT");
    assert_eq!(lines[3], "");
    assert_eq!(lines[9], "*");
    assert_eq!(lines[12], "x-ms-date:Tue, 29 Sep 2026 00:00:00 GMT");
    assert_eq!(lines[13], "x-ms-version:2023-11-03");
    assert_eq!(lines[14], "/account/account/container/object%20key");
    let query = Url::parse("https://fixture.invalid/container?private=value").unwrap();
    assert!(canonical("account", &query, &headers).is_err());
}

#[test]
fn metadata_cannot_duplicate_headers_or_inject_lines() {
    let mut headers = HeaderMap::new();
    insert(&mut headers, "x-ms-meta-name", "one").unwrap();
    assert!(insert(&mut headers, "X-MS-META-NAME", "two").is_err());
    assert!(
        insert(
            &mut headers,
            "x-ms-meta-other",
            "private\r\nAuthorization: bad"
        )
        .is_err()
    );
}

#[test]
fn signing_folds_header_whitespace_but_preserves_quoted_values() {
    assert_eq!(
        canonical_header("  alpha\t  \"two  spaces\"   \"escaped\\\"  quote\"  omega  "),
        "alpha \"two  spaces\" \"escaped\\\"  quote\" omega"
    );
    assert_eq!(canonical_header(" \t "), "");
    let mut headers = HeaderMap::new();
    insert(&mut headers, "x-ms-meta-value", "one   two").unwrap();
    let url = Url::parse("https://fixture.invalid/container/object").unwrap();
    assert!(
        canonical("account", &url, &headers)
            .unwrap()
            .contains("x-ms-meta-value:one two\n")
    );
}
