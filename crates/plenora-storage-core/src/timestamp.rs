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
#[path = "timestamp_tests.rs"]
mod tests;
