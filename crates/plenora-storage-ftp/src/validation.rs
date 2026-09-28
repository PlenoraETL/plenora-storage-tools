//! Configuration, key and publication preconditions checked before mutation.

use super::{
    ErrorCategory, ErrorPhase, FtpConnectionConfig, PROVIDER_ID, ProviderConnection,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StorageError, StorageResult,
    configuration_error, validate_object_key, validate_object_prefix,
};

pub fn parse_config(connection: &ProviderConnection) -> StorageResult<FtpConnectionConfig> {
    let config: FtpConnectionConfig =
        serde_json::from_value(connection.config.clone()).map_err(|_| configuration_error())?;
    // Serde alone accepts values that `plenora-storage-ftp-connection-v1`
    // rejects, so the schema bounds are enforced here as well.
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    let valid = (1..=253).contains(&config.host.chars().count())
        && !config.host.contains(char::is_whitespace)
        && !config.host.contains('\0')
        && config.port > 0
        && is_valid_root(&config.root)
        && config
            .tls_ca_pem
            .as_ref()
            .is_none_or(|pem| !pem.is_empty() && pem.len() <= 65_536);
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

pub fn validate_key(key: &str) -> StorageResult<()> {
    validate_object_key(key).map_err(|error| error.with_provider(PROVIDER_ID))
}

pub fn validate_prefix(prefix: &str) -> StorageResult<()> {
    validate_object_prefix(prefix).map_err(|error| error.with_provider(PROVIDER_ID))
}

pub fn validate_file_metadata(request: &PutRequest) -> StorageResult<()> {
    if request.content_type.is_some() || !request.metadata.is_empty() {
        return Err(StorageError::unsupported(
            "FTP does not preserve object content type or custom metadata",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

pub fn validate_ftp_publication(
    overwrite: bool,
    publication_policy: PublicationPolicy,
) -> StorageResult<()> {
    if !overwrite {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_CREATE_IF_ABSENT_UNSUPPORTED",
            "FTP cannot guarantee atomic create-if-absent and rejects overwrite=false",
        )
        .with_provider(PROVIDER_ID));
    }
    if publication_policy == PublicationPolicy::AtomicRequired {
        return Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "FTP_ATOMIC_PUBLICATION_UNSUPPORTED",
            "FTP cannot guarantee atomic publication",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}
