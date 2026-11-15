//! Remote modification times rendered for metadata.
//!
//! The instant comes from the remote side (an FTP listing, SFTP attributes):
//! it may lie outside the range a calendar date can represent. Such a value is
//! reported as unknown, never as a panic, so every provider converts through
//! this single function.

use std::time::{SystemTime, UNIX_EPOCH};

use time::{OffsetDateTime, format_description::well_known::Rfc3339};

/// RFC 3339 rendering of `value`, or `None` when it has no calendar date.
#[must_use]
pub fn rfc3339_from_system_time(value: SystemTime) -> Option<String> {
    let nanos = match value.duration_since(UNIX_EPOCH) {
        Ok(after) => i128::try_from(after.as_nanos()).ok()?,
        Err(before) => -i128::try_from(before.duration().as_nanos()).ok()?,
    };
    OffsetDateTime::from_unix_timestamp_nanos(nanos)
        .ok()?
        .format(&Rfc3339)
        .ok()
}

#[cfg(test)]
mod tests {
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
}
