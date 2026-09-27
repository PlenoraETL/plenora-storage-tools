use super::validate;
#[test]
fn unrelated_xml_is_not_an_empty_listing() {
    for data in [
        b"<html/>".as_slice(),
        b"<Error><Message>private</Message></Error>",
    ] {
        assert!(validate(data).is_err());
    }
}
#[test]
fn rejects_names_that_path_normalization_would_alias() {
    for key in ["folder/", "/folder", "a//b", "a/../b", ""] {
        let xml = format!(
            "<EnumerationResults><Blobs><Blob><Name>{key}</Name></Blob></Blobs></EnumerationResults>"
        );
        assert!(validate(xml.as_bytes()).is_err());
    }
    assert!(validate(b"<EnumerationResults><Blobs><Blob><Name Encoded=\"true\">a%2Fb</Name></Blob></Blobs></EnumerationResults>").is_err());
    validate(b"<EnumerationResults><Blobs><Blob><Name>a&amp;b/c</Name></Blob></Blobs></EnumerationResults>").unwrap();
    validate(b"<EnumerationResults><Blobs/></EnumerationResults>").unwrap();
}
