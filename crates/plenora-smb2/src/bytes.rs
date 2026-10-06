//! Fixed-width little-endian reads that report truncation instead of
//! panicking.
//!
//! Wire parsers check a length and then slice; the slice-to-array conversion
//! after that check cannot fail, but expressing it with `unwrap` would put a
//! panic in production code the moment a check and a slice drift apart.
//! These return [`Error::InvalidData`] naming `what` instead.

use crate::Error;

fn array<const N: usize>(buf: &[u8], at: usize, what: &'static str) -> Result<[u8; N], Error> {
    at.checked_add(N)
        .and_then(|end| buf.get(at..end))
        .and_then(|b| <[u8; N]>::try_from(b).ok())
        .ok_or_else(|| Error::invalid_data(what))
}

/// Little-endian `u32` at `at`.
pub(crate) fn le_u32(buf: &[u8], at: usize, what: &'static str) -> Result<u32, Error> {
    array(buf, at, what).map(u32::from_le_bytes)
}

/// Little-endian `u64` at `at`.
pub(crate) fn le_u64(buf: &[u8], at: usize, what: &'static str) -> Result<u64, Error> {
    array(buf, at, what).map(u64::from_le_bytes)
}

/// Little-endian `i64` at `at`.
pub(crate) fn le_i64(buf: &[u8], at: usize, what: &'static str) -> Result<i64, Error> {
    array(buf, at, what).map(i64::from_le_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_in_bounds_and_reports_truncation() {
        let buf = [1, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0];
        assert_eq!(le_u32(&buf, 0, "x").unwrap(), 1);
        assert_eq!(le_u64(&buf, 4, "x").unwrap(), 2);
        assert_eq!(le_i64(&buf, 4, "x").unwrap(), 2);
        assert!(matches!(
            le_u32(&buf, 10, "x"),
            Err(Error::InvalidData { .. })
        ));
        assert!(matches!(
            le_u32(&buf, usize::MAX, "x"),
            Err(Error::InvalidData { .. })
        ));
    }
}
