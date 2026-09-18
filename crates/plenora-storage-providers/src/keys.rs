#[cfg(any(feature = "local", feature = "smb"))]
use crate::common::invalid;
#[cfg(any(feature = "local", feature = "smb"))]
use plenora_storage_core::{StorageResult, validate_object_key};

#[cfg(any(feature = "local", feature = "gcs"))]
pub fn stage_name() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NONCE: AtomicU64 = AtomicU64::new(0);
    let time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!(
        ".plenora-stage-{}-{time}-{}",
        std::process::id(),
        NONCE.fetch_add(1, Ordering::Relaxed)
    )
}
#[cfg(any(feature = "local", feature = "smb"))]
pub fn portable_key(key: &str) -> StorageResult<()> {
    validate_object_key(key)?;
    for part in key.split('/') {
        let stem = part
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        if part.ends_with(['.', ' '])
            || part.chars().any(|connection| {
                connection.is_control()
                    || "<>:\"|?*".contains(connection)
                    || ('\u{f000}'..='\u{f0ff}').contains(&connection)
            })
            || matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.as_bytes()[3].is_ascii_digit())
            || part.starts_with(".plenora-stage-")
        {
            return Err(invalid("FILESYSTEM_KEY_INVALID"));
        }
    }
    Ok(())
}
