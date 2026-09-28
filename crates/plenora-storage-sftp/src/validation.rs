//! Configuration, key and publication preconditions checked before mutation.

use super::{
    ErrorCategory, ErrorPhase, PROVIDER_ID, ProviderConnection, PublicationPolicy, PutRequest,
    RemoteEffect, RetryDisposition, SftpConnectionConfig, StorageError, StorageResult,
    configuration_error, validate_object_key, validate_object_prefix,
};

pub fn parse_config(connection: &ProviderConnection) -> StorageResult<SftpConnectionConfig> {
    let config: SftpConnectionConfig =
        serde_json::from_value(connection.config.clone()).map_err(|_| configuration_error())?;
    // Serde alone accepts values that `plenora-storage-sftp-connection-v1`
    // rejects, so the schema bounds are enforced here as well.
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    let valid = (1..=253).contains(&config.host.chars().count())
        && !config.host.contains(char::is_whitespace)
        && !config.host.contains('\0')
        && config.port > 0
        && is_valid_root(&config.root)
        && config
            .host_key_sha256
            .as_ref()
            .is_none_or(|value| is_ssh_sha256_fingerprint(value));
    if valid {
        Ok(config)
    } else {
        Err(configuration_error())
    }
}

pub fn is_valid_root(root: &str) -> bool {
    (1..=4_096).contains(&root.chars().count())
        && !root.contains('\\')
        && !root.contains('\0')
        && !root.split('/').any(|part| part == "..")
}

/// Matches the `host_key_sha256` pattern of the SFTP connection contract. It is
/// checked even when unverified host keys are authorized, so that relaxing the
/// policy cannot also relax the configuration contract.
pub fn is_ssh_sha256_fingerprint(value: &str) -> bool {
    let Some(encoded) = value.strip_prefix("SHA256:") else {
        return false;
    };
    let body = encoded.strip_suffix('=').unwrap_or(encoded);
    body.len() == 43
        && body
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/'))
}

pub fn validate_key(key: &str) -> StorageResult<()> {
    validate_object_key(key).map_err(|error| error.with_provider(PROVIDER_ID))
}

pub fn validate_prefix(prefix: &str) -> StorageResult<()> {
    validate_object_prefix(prefix).map_err(|error| error.with_provider(PROVIDER_ID))
}

pub fn validate_file_metadata(request: &PutRequest) -> StorageResult<()> {
    if request.content_type.is_some() || !request.metadata.is_empty() {
        return Err(StorageError::unsupported(
            "SFTP does not preserve object content type or custom metadata",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

pub fn validate_sftp_publication(
    connection: &ProviderConnection,
    overwrite: bool,
    publication_policy: PublicationPolicy,
) -> StorageResult<bool> {
    if publication_policy != PublicationPolicy::AtomicRequired {
        return Ok(false);
    }
    let config = parse_config(connection)?;
    if !config.atomic_rename || !overwrite {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "SFTP_ATOMIC_PUBLICATION_UNAVAILABLE",
            "SFTP atomic publication requires a qualified atomic rename connection and overwrite=true",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(true)
}
