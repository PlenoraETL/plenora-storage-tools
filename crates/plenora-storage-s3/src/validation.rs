//! Configuration, key and publication preconditions checked before mutation.

use super::{
    OperationContext, PROVIDER_ID, Path, ProviderConnection, PutRequest, S3ConnectionConfig,
    StorageError, StorageResult, Url, configuration_error, validate_object_key,
    validate_object_prefix,
};

pub fn parse_config(connection: &ProviderConnection) -> StorageResult<S3ConnectionConfig> {
    let config: S3ConnectionConfig =
        serde_json::from_value(connection.config.clone()).map_err(|_| configuration_error())?;
    // Serde alone accepts values that `plenora-storage-s3-connection-v1`
    // rejects, so the schema bounds are enforced here as well.
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    let valid = (1..=2_048).contains(&config.endpoint.chars().count())
        && (1..=255).contains(&config.bucket.chars().count())
        && !config.bucket.chars().any(char::is_whitespace)
        && (1..=128).contains(&config.region.chars().count())
        && !config.region.chars().any(char::is_whitespace);
    if valid {
        Ok(config)
    } else {
        Err(configuration_error())
    }
}

pub fn validate_endpoint(endpoint: &str, context: &OperationContext<'_>) -> StorageResult<Url> {
    let url = Url::parse(endpoint).map_err(|_| {
        StorageError::invalid_configuration("S3_ENDPOINT_INVALID", "S3 endpoint URL is invalid")
            .with_provider(PROVIDER_ID)
    })?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(StorageError::invalid_configuration(
            "S3_ENDPOINT_CREDENTIALS_FORBIDDEN",
            "S3 endpoint must not contain credentials",
        )
        .with_provider(PROVIDER_ID));
    }
    match url.scheme() {
        "https" => {}
        "http" if context.policy.allow_insecure_http => {}
        "http" => {
            return Err(StorageError::invalid_configuration(
                "INSECURE_HTTP_FORBIDDEN",
                "HTTP storage endpoint requires explicit engine authorization",
            )
            .with_provider(PROVIDER_ID));
        }
        _ => {
            return Err(StorageError::invalid_configuration(
                "S3_ENDPOINT_SCHEME_UNSUPPORTED",
                "S3 endpoint must use HTTPS or explicitly authorized HTTP",
            )
            .with_provider(PROVIDER_ID));
        }
    }
    if url.host_str().is_none() {
        return Err(StorageError::invalid_configuration(
            "S3_ENDPOINT_HOST_MISSING",
            "S3 endpoint lacks a host",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(url)
}

pub fn required_path(value: &str) -> StorageResult<Path> {
    validate_object_key(value).map_err(|error| error.with_provider(PROVIDER_ID))?;
    Path::parse(value).map_err(|_| {
        StorageError::invalid_configuration(
            "OBJECT_KEY_INVALID",
            "storage object key is not a normalized relative path",
        )
        .with_provider(PROVIDER_ID)
    })
}

/// An empty prefix means "the whole namespace" for every provider, so it maps to
/// no prefix rather than being rejected as an invalid path.
pub fn optional_prefix_path(value: Option<&str>) -> StorageResult<Option<Path>> {
    let Some(prefix) = value else {
        return Ok(None);
    };
    validate_object_prefix(prefix).map_err(|error| error.with_provider(PROVIDER_ID))?;
    if prefix.is_empty() {
        return Ok(None);
    }
    Path::parse(prefix).map(Some).map_err(|_| {
        StorageError::invalid_configuration(
            "OBJECT_PREFIX_INVALID",
            "storage object prefix is not a normalized relative path",
        )
        .with_provider(PROVIDER_ID)
    })
}

pub fn validate_metadata(request: &PutRequest) -> StorageResult<()> {
    // Lengths are counted in code points, as JSON Schema `maxLength` does.
    if request.metadata.len() > 64
        || request
            .metadata
            .iter()
            .any(|(key, value)| key.chars().count() > 128 || value.chars().count() > 2_048)
    {
        return Err(StorageError::invalid_configuration(
            "OBJECT_METADATA_TOO_LARGE",
            "object metadata exceeds public bounds",
        ));
    }
    if request.content_type.as_ref().is_some_and(|content_type| {
        content_type.chars().count() > 255 || !content_type.contains('/')
    }) {
        return Err(StorageError::invalid_configuration(
            "CONTENT_TYPE_INVALID",
            "object content type is invalid",
        ));
    }
    Ok(())
}
