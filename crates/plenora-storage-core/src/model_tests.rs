
use super::{directory_may_contain, key_matches_prefix};

#[test]
fn prefixes_select_whole_path_segments_on_every_provider() {
    assert!(key_matches_prefix("incoming/a.bin", "incoming/"));
    assert!(key_matches_prefix("incoming/a.bin", "incoming"));
    assert!(key_matches_prefix("incoming/deep/a.bin", "incoming"));
    assert!(key_matches_prefix("anything", ""));
    // A literal string match would select this; the object store providers
    // would not, so neither does the public semantics.
    assert!(!key_matches_prefix("incomingother/a.bin", "incoming"));
    assert!(!key_matches_prefix("incoming", "incoming"));
    assert!(!key_matches_prefix("other/a.bin", "incoming"));
}

#[test]
fn traversal_only_enters_directories_that_can_still_match() {
    assert!(directory_may_contain("incoming", "incoming/deep/"));
    assert!(directory_may_contain("incoming/deep", "incoming/"));
    assert!(directory_may_contain("incoming", "incoming"));
    assert!(directory_may_contain("anything", ""));
    assert!(!directory_may_contain("incomingother", "incoming"));
    assert!(!directory_may_contain("other", "incoming"));
}
