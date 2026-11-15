use std::time::Duration;

use super::*;

#[test]
fn dates_in_range_are_rendered() {
    let instant = UNIX_EPOCH + Duration::from_hours(488_580);
    assert_eq!(
        rfc3339_from_system_time(instant).as_deref(),
        Some("2025-09-26T12:00:00Z")
    );
    let before = UNIX_EPOCH - Duration::from_hours(24);
    assert_eq!(
        rfc3339_from_system_time(before).as_deref(),
        Some("1969-12-31T00:00:00Z")
    );
}

#[test]
fn instants_without_a_calendar_date_are_unknown_not_a_panic() {
    // Year 10000 and beyond: what a hostile listing such as
    // `modify=+0250926120000` produces, and what panicked before.
    let far = UNIX_EPOCH.checked_add(Duration::from_hours(70_389_528));
    let far = far.expect("representable on the test platform");
    assert_eq!(rfc3339_from_system_time(far), None);
    if let Some(farther) = UNIX_EPOCH.checked_add(Duration::from_secs(u64::MAX / 4)) {
        assert_eq!(rfc3339_from_system_time(farther), None);
    }
}
