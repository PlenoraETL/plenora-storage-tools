
use super::*;
#[test]
fn malformed_directory_offsets_and_names_are_rejected() {
    for data in [vec![], vec![0; 63], vec![255; 128]] {
        assert!(
            parse_page(
                &data,
                "",
                &ProviderListRequest::default(),
                10,
                &mut BTreeMap::new(),
                &mut Vec::new(),
                &mut 0
            )
            .is_err()
        );
    }
}
