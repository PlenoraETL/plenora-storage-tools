use super::validate_list_response;

#[test]
fn unrelated_xml_is_not_an_empty_listing() {
    for data in [
        b"<html/>".as_slice(),
        b"<Error><Message>private</Message></Error>",
    ] {
        assert!(validate_list_response(data).is_err());
    }
}

#[test]
fn isolated_invalid_keys_are_rejected_before_normalization() {
    for key in ["folder/", "/folder", "folder//file", "folder/../file"] {
        let xml =
            format!("<ListBucketResult><Contents><Key>{key}</Key></Contents></ListBucketResult>");
        assert_eq!(
            validate_list_response(xml.as_bytes())
                .expect_err("invalid key")
                .code,
            "OBJECT_KEY_UNREPRESENTABLE"
        );
    }
}

#[test]
fn valid_names_and_xml_entities_remain_representable() {
    for key in ["folder/file", "folder/a&amp;b", "folder/a%2Fb", "folder/雪"] {
        let xml =
            format!("<ListBucketResult><Contents><Key>{key}</Key></Contents></ListBucketResult>");
        validate_list_response(xml.as_bytes()).expect("valid key");
    }
    validate_list_response(b"<ListBucketResult/>").expect("empty listing");
}
