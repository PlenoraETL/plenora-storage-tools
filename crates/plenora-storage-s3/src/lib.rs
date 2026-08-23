//! S3-compatible adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

use std::{
    borrow::Cow,
    collections::BTreeMap,
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
    sync::Arc,
};

use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::{
    Attribute, AttributeValue, Attributes, CopyMode, CopyOptions, ObjectMeta, ObjectStore,
    ObjectStoreExt, PutMultipartOptions, WriteMultipart,
    aws::{AmazonS3, AmazonS3Builder},
    path::Path,
};
use plenora_storage_core::{
    CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory, ErrorPhase,
    GetRequest, IntegrityMetadata, ListRequest, ListResult, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, PutRequest, RemoteEffect, RetryDisposition,
    StatRequest, StorageError, StorageProvider, StorageResult, TestResult, TransferResult,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use url::{Host, Url};

pub const PROVIDER_ID: &str = "s3";
pub const CONFIG_CONTRACT: &str = "plenora-storage-s3-connection-v1";

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct S3ConnectionConfig {
    pub endpoint: String,
    pub bucket: String,
    #[serde(default = "default_region")]
    pub region: String,
    #[serde(default)]
    pub virtual_hosted_style: bool,
}

fn default_region() -> String {
    "us-east-1".to_owned()
}

pub struct S3Provider {
    credentials: Arc<dyn CredentialResolver>,
}

impl S3Provider {
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self { credentials }
    }

    async fn store(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<AmazonS3> {
        let config = parse_config(connection)?;
        validate_endpoint(&config.endpoint, context).await?;
        validate_bucket(&config.bucket)?;
        let credential = self
            .credentials
            .resolve(&connection.credential_ref)
            .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let access_key = credential.required("access_key_id")?.to_owned();
        let secret_key = credential.required("secret_access_key")?.to_owned();
        let mut builder = AmazonS3Builder::new()
            .with_endpoint(config.endpoint)
            .with_bucket_name(config.bucket)
            .with_region(config.region)
            .with_virtual_hosted_style_request(config.virtual_hosted_style)
            .with_allow_http(context.policy.allow_insecure_http)
            .with_access_key_id(access_key)
            .with_secret_access_key(secret_key);
        if let Some(token) = credential.optional("session_token") {
            builder = builder.with_token(token.to_owned());
        }
        builder
            .build()
            .map_err(|error| map_store_error(error, ErrorPhase::Connect, false))
    }
}

#[async_trait]
impl StorageProvider for S3Provider {
    fn id(&self) -> &'static str {
        PROVIDER_ID
    }

    fn config_contract(&self) -> &'static str {
        CONFIG_CONTRACT
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: PROVIDER_ID.to_owned(),
            config_contract: CONFIG_CONTRACT.to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .into_iter()
                .map(str::to_owned)
                .collect(),
            attributes: BTreeMap::from([
                ("api".to_owned(), "s3-compatible".to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "true".to_owned()),
                ("conditional_streaming_put".to_owned(), "false".to_owned()),
                ("list_order".to_owned(), "lexicographic".to_owned()),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let store = self.store(connection, context).await?;
        context
            .control
            .run(
                async {
                    let mut stream = store.list(None);
                    if let Some(result) = stream.next().await {
                        result.map_err(|error| map_store_error(error, ErrorPhase::Probe, false))?;
                    }
                    Ok(TestResult {
                        provider: PROVIDER_ID.to_owned(),
                        reachable: true,
                    })
                },
                ErrorPhase::Probe,
                false,
            )
            .await
    }

    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ListResult> {
        let limit = request.max_items.unwrap_or(1_000);
        if limit == 0 || limit > context.policy.max_list_items {
            return Err(StorageError::new(
                ErrorCategory::ResourceLimit,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "LIST_LIMIT_INVALID",
                "list max_items is zero or exceeds the engine policy",
            )
            .with_provider(PROVIDER_ID));
        }
        let prefix = optional_path(request.prefix.as_deref())?;
        let offset = optional_path(request.start_after.as_deref())?;
        let store = self.store(connection, context).await?;
        context
            .control
            .run(
                async {
                    let mut stream = offset.as_ref().map_or_else(
                        || store.list(prefix.as_ref()),
                        |offset| store.list_with_offset(prefix.as_ref(), offset),
                    );
                    let mut objects = Vec::with_capacity(limit.min(1_024));
                    while objects.len() <= limit {
                        let Some(result) = stream.next().await else {
                            break;
                        };
                        let metadata = result
                            .map_err(|error| map_store_error(error, ErrorPhase::Read, false))?;
                        objects.push(public_metadata(metadata));
                    }
                    let truncated = objects.len() > limit;
                    if truncated {
                        objects.truncate(limit);
                    }
                    let next_start_after = truncated
                        .then(|| objects.last().map(|object| object.key.clone()))
                        .flatten();
                    Ok(ListResult {
                        objects,
                        truncated,
                        next_start_after,
                    })
                },
                ErrorPhase::Read,
                false,
            )
            .await
    }

    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let path = required_path(&request.key)?;
        let store = self.store(connection, context).await?;
        context
            .control
            .run(
                async {
                    store
                        .head(&path)
                        .await
                        .map(public_metadata)
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await
    }

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let path = required_path(&request.key)?;
        let store = self.store(connection, context).await?;
        let result = context
            .control
            .run(
                async {
                    store
                        .get(&path)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        if result.meta.size > context.policy.max_transfer_bytes {
            return Err(transfer_limit_error());
        }
        let etag = result.meta.e_tag.clone();
        let version = result.meta.version.clone();
        let mut stream = result.into_stream();
        let mut transferred = 0_u64;
        let mut digest = Sha256::new();
        loop {
            let chunk = context
                .control
                .run(
                    async {
                        stream
                            .next()
                            .await
                            .transpose()
                            .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            let Some(chunk) = chunk else {
                break;
            };
            transferred = transferred
                .checked_add(chunk.len() as u64)
                .ok_or_else(transfer_limit_error)?;
            if transferred > context.policy.max_transfer_bytes {
                return Err(transfer_limit_error());
            }
            context
                .control
                .run(
                    async {
                        sink.write_all(&chunk).await.map_err(|_| {
                            StorageError::new(
                                ErrorCategory::Io,
                                ErrorPhase::Write,
                                RemoteEffect::None,
                                RetryDisposition::Never,
                                "ARTIFACT_WRITE_FAILED",
                                "artifact sink write failed",
                            )
                        })
                    },
                    ErrorPhase::Write,
                    false,
                )
                .await?;
            digest.update(&chunk);
        }
        context
            .control
            .run(
                async {
                    sink.flush().await.map_err(|_| {
                        StorageError::new(
                            ErrorCategory::Io,
                            ErrorPhase::Write,
                            RemoteEffect::None,
                            RetryDisposition::Never,
                            "ARTIFACT_FLUSH_FAILED",
                            "artifact sink flush failed",
                        )
                    })
                },
                ErrorPhase::Write,
                false,
            )
            .await?;
        Ok(TransferResult {
            key: request.key.clone(),
            bytes_transferred: transferred,
            checksum: sha256_metadata(digest),
            etag,
            version,
        })
    }

    async fn put(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        if !request.overwrite {
            return Err(StorageError::unsupported(
                "S3 streaming put requires explicit overwrite=true in contract v1",
            )
            .with_provider(PROVIDER_ID));
        }
        if request
            .content_length
            .is_some_and(|length| length > context.policy.max_transfer_bytes)
        {
            return Err(transfer_limit_error());
        }
        validate_metadata(request)?;
        let path = required_path(&request.key)?;
        let store = self.store(connection, context).await?;
        let options = PutMultipartOptions {
            attributes: put_attributes(request),
            ..PutMultipartOptions::default()
        };
        let upload = context
            .control
            .run(
                async {
                    store
                        .put_multipart_opts(&path, options)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Prepare, true))
                },
                ErrorPhase::Prepare,
                true,
            )
            .await?;
        let mut writer = WriteMultipart::new(upload);
        let mut buffer = vec![0_u8; 64 * 1024];
        let mut transferred = 0_u64;
        let mut digest = Sha256::new();
        loop {
            let read = match context
                .control
                .run(
                    async {
                        source.read(&mut buffer).await.map_err(|_| {
                            StorageError::new(
                                ErrorCategory::Io,
                                ErrorPhase::Read,
                                RemoteEffect::Unknown,
                                RetryDisposition::RequiresRecovery,
                                "ARTIFACT_READ_FAILED",
                                "artifact source read failed",
                            )
                        })
                    },
                    ErrorPhase::Read,
                    true,
                )
                .await
            {
                Ok(read) => read,
                Err(error) => {
                    let _ = writer.abort().await;
                    return Err(error);
                }
            };
            if read == 0 {
                break;
            }
            transferred = match transferred.checked_add(read as u64) {
                Some(total) if total <= context.policy.max_transfer_bytes => total,
                _ => {
                    let _ = writer.abort().await;
                    return Err(transfer_limit_error());
                }
            };
            digest.update(&buffer[..read]);
            writer.write(&buffer[..read]);
            if let Err(error) = context
                .control
                .run(
                    async {
                        writer
                            .wait_for_capacity(4)
                            .await
                            .map_err(|source| map_store_error(source, ErrorPhase::Write, true))
                    },
                    ErrorPhase::Write,
                    true,
                )
                .await
            {
                let _ = writer.abort().await;
                return Err(error);
            }
        }
        if request
            .content_length
            .is_some_and(|expected| expected != transferred)
        {
            let _ = writer.abort().await;
            return Err(StorageError::invalid_configuration(
                "CONTENT_LENGTH_MISMATCH",
                "artifact length differs from declared content_length",
            )
            .with_provider(PROVIDER_ID));
        }
        let result = context
            .control
            .run(
                async move {
                    writer
                        .finish()
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
        Ok(TransferResult {
            key: request.key.clone(),
            bytes_transferred: transferred,
            checksum: sha256_metadata(digest),
            etag: result.e_tag,
            version: result.version,
        })
    }

    async fn delete(
        &self,
        connection: &ProviderConnection,
        request: &DeleteRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        let path = required_path(&request.key)?;
        let store = self.store(connection, context).await?;
        let head = context
            .control
            .run(
                async {
                    store
                        .head(&path)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await;
        let exists = match head {
            Ok(_) => true,
            Err(error) if request.ignore_missing && error.category == ErrorCategory::NotFound => {
                false
            }
            Err(error) => return Err(error),
        };
        if !exists {
            return Ok(DeleteResult {
                key: request.key.clone(),
                deleted: false,
            });
        }
        context
            .control
            .run(
                async {
                    store
                        .delete(&path)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
        Ok(DeleteResult {
            key: request.key.clone(),
            deleted: true,
        })
    }

    async fn copy(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        if !request.overwrite {
            return Err(StorageError::unsupported(
                "S3 copy requires explicit overwrite=true in contract v1",
            )
            .with_provider(PROVIDER_ID));
        }
        let source = required_path(&request.source_key)?;
        let destination = required_path(&request.destination_key)?;
        if source == destination {
            return Err(StorageError::invalid_configuration(
                "COPY_TARGET_EQUALS_SOURCE",
                "copy source and destination must differ",
            ));
        }
        let store = self.store(connection, context).await?;
        context
            .control
            .run(
                async {
                    store
                        .copy_opts(
                            &source,
                            &destination,
                            CopyOptions::new().with_mode(CopyMode::Overwrite),
                        )
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await?;
        context
            .control
            .run(
                async {
                    store
                        .head(&destination)
                        .await
                        .map(public_metadata)
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<S3ConnectionConfig> {
    serde_json::from_value(connection.config.clone()).map_err(|_| {
        StorageError::invalid_configuration(
            "S3_CONFIG_INVALID",
            "S3 connection configuration is invalid",
        )
        .with_provider(PROVIDER_ID)
    })
}

async fn validate_endpoint(endpoint: &str, context: &OperationContext<'_>) -> StorageResult<()> {
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
    let host = url.host().ok_or_else(|| {
        StorageError::invalid_configuration("S3_ENDPOINT_HOST_MISSING", "S3 endpoint lacks a host")
            .with_provider(PROVIDER_ID)
    })?;
    if context.policy.allow_private_network {
        return Ok(());
    }
    match host {
        Host::Ipv4(address) if !is_public_ipv4(address) => private_endpoint_error(),
        Host::Ipv6(address) if !is_public_ipv6(address) => private_endpoint_error(),
        Host::Domain(domain) => {
            let port = url.port_or_known_default().ok_or_else(|| {
                StorageError::invalid_configuration(
                    "S3_ENDPOINT_PORT_UNKNOWN",
                    "S3 endpoint has no resolvable port",
                )
            })?;
            let addresses = tokio::net::lookup_host((domain, port)).await.map_err(|_| {
                StorageError::new(
                    ErrorCategory::Transient,
                    ErrorPhase::Connect,
                    RemoteEffect::None,
                    RetryDisposition::Safe,
                    "DNS_RESOLUTION_FAILED",
                    "S3 endpoint DNS resolution failed",
                )
                .with_provider(PROVIDER_ID)
            })?;
            if addresses
                .into_iter()
                .any(|address| !is_public_address(address.ip()))
            {
                return private_endpoint_error();
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn private_endpoint_error<T>() -> StorageResult<T> {
    Err(StorageError::invalid_configuration(
        "PRIVATE_NETWORK_FORBIDDEN",
        "private-network storage endpoint requires explicit engine authorization",
    )
    .with_provider(PROVIDER_ID))
}

fn validate_bucket(bucket: &str) -> StorageResult<()> {
    if bucket.is_empty() || bucket.len() > 255 || bucket.chars().any(char::is_whitespace) {
        return Err(StorageError::invalid_configuration(
            "S3_BUCKET_INVALID",
            "S3 bucket name is invalid",
        )
        .with_provider(PROVIDER_ID));
    }
    Ok(())
}

fn required_path(value: &str) -> StorageResult<Path> {
    if value.is_empty() || value.len() > 4_096 {
        return Err(StorageError::invalid_configuration(
            "OBJECT_KEY_INVALID_LENGTH",
            "storage object key length is outside public bounds",
        ));
    }
    Path::parse(value).map_err(|_| {
        StorageError::invalid_configuration(
            "OBJECT_KEY_INVALID",
            "storage object key is not a normalized relative path",
        )
    })
}

fn optional_path(value: Option<&str>) -> StorageResult<Option<Path>> {
    value.map(required_path).transpose()
}

fn validate_metadata(request: &PutRequest) -> StorageResult<()> {
    if request.metadata.len() > 64
        || request
            .metadata
            .iter()
            .any(|(key, value)| key.len() > 128 || value.len() > 2_048)
    {
        return Err(StorageError::invalid_configuration(
            "OBJECT_METADATA_TOO_LARGE",
            "object metadata exceeds public bounds",
        ));
    }
    if request
        .content_type
        .as_ref()
        .is_some_and(|content_type| content_type.len() > 255 || !content_type.contains('/'))
    {
        return Err(StorageError::invalid_configuration(
            "CONTENT_TYPE_INVALID",
            "object content type is invalid",
        ));
    }
    Ok(())
}

fn put_attributes(request: &PutRequest) -> Attributes {
    let mut attributes = Attributes::new();
    if let Some(content_type) = &request.content_type {
        attributes.insert(
            Attribute::ContentType,
            AttributeValue::from(content_type.clone()),
        );
    }
    for (key, value) in &request.metadata {
        attributes.insert(
            Attribute::Metadata(Cow::Owned(key.clone())),
            AttributeValue::from(value.clone()),
        );
    }
    attributes
}

fn public_metadata(metadata: ObjectMeta) -> ObjectMetadata {
    ObjectMetadata {
        key: metadata.location.to_string(),
        size: metadata.size,
        last_modified: Some(metadata.last_modified.to_rfc3339()),
        etag: metadata.e_tag,
        version: metadata.version,
    }
}

fn sha256_metadata(digest: Sha256) -> IntegrityMetadata {
    IntegrityMetadata {
        algorithm: "sha256".to_owned(),
        value: format!("{:x}", digest.finalize()),
    }
}

fn transfer_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeds the engine byte limit",
    )
    .with_provider(PROVIDER_ID)
}

fn map_store_error(error: object_store::Error, phase: ErrorPhase, mutating: bool) -> StorageError {
    let (category, effect, retry, code, message) = match error {
        object_store::Error::NotFound { .. } => (
            ErrorCategory::NotFound,
            RemoteEffect::None,
            RetryDisposition::Never,
            "OBJECT_NOT_FOUND",
            "storage object was not found",
        ),
        object_store::Error::AlreadyExists { .. }
        | object_store::Error::Precondition { .. }
        | object_store::Error::NotModified { .. } => (
            ErrorCategory::Conflict,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PRECONDITION_FAILED",
            "storage operation precondition failed",
        ),
        object_store::Error::PermissionDenied { .. } => (
            ErrorCategory::Authorization,
            RemoteEffect::None,
            RetryDisposition::Never,
            "AUTHORIZATION_FAILED",
            "storage provider denied the operation",
        ),
        object_store::Error::Unauthenticated { .. } => (
            ErrorCategory::Authentication,
            RemoteEffect::None,
            RetryDisposition::Never,
            "AUTHENTICATION_FAILED",
            "storage provider rejected the credentials",
        ),
        object_store::Error::NotSupported { .. } | object_store::Error::NotImplemented { .. } => (
            ErrorCategory::Unsupported,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PROVIDER_OPERATION_UNSUPPORTED",
            "storage provider does not support the operation",
        ),
        object_store::Error::InvalidPath { .. }
        | object_store::Error::UnknownConfigurationKey { .. } => (
            ErrorCategory::InvalidConfiguration,
            RemoteEffect::None,
            RetryDisposition::Never,
            "PROVIDER_CONFIGURATION_INVALID",
            "storage provider configuration is invalid",
        ),
        _ if mutating => (
            ErrorCategory::Execution,
            RemoteEffect::Unknown,
            RetryDisposition::RequiresRecovery,
            "PROVIDER_MUTATION_FAILED",
            "storage mutation failed with an unknown remote outcome",
        ),
        _ => (
            ErrorCategory::Transient,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "PROVIDER_REQUEST_FAILED",
            "storage provider request failed",
        ),
    };
    StorageError::new(category, phase, effect, retry, code, message).with_provider(PROVIDER_ID)
}

fn is_public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_public_ipv4(address),
        IpAddr::V6(address) => is_public_ipv6(address),
    }
}

fn is_public_ipv4(address: Ipv4Addr) -> bool {
    let octets = address.octets();
    !(address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_broadcast()
        || address.is_documentation()
        || address.is_unspecified()
        || address.is_multicast()
        || octets[0] == 0
        || (octets[0] == 100 && (64..=127).contains(&octets[1]))
        || (octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
        || (octets[0] == 198 && (18..=19).contains(&octets[1]))
        || octets[0] >= 240)
}

fn is_public_ipv6(address: Ipv6Addr) -> bool {
    if let Some(mapped) = address.to_ipv4_mapped() {
        return is_public_ipv4(mapped);
    }
    let segments = address.segments();
    !(address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || (segments[0] & 0xfe00) == 0xfc00
        || (segments[0] & 0xffc0) == 0xfe80
        || (segments[0] == 0x2001 && segments[1] == 0x0db8))
}

#[cfg(test)]
mod tests {
    use super::{is_public_address, required_path};
    use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

    #[test]
    fn private_addresses_are_blocked() {
        assert!(!is_public_address(IpAddr::V4(Ipv4Addr::LOCALHOST)));
        assert!(!is_public_address(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))));
        assert!(!is_public_address(IpAddr::V6(Ipv6Addr::LOCALHOST)));
        assert!(is_public_address(IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1))));
    }

    #[test]
    fn object_keys_are_relative_and_normalized() {
        assert!(required_path("folder/object.bin").is_ok());
        assert!(required_path("").is_err());
        assert!(required_path(&"x".repeat(4_097)).is_err());
        assert!(required_path("../secret").is_err());
    }
}
