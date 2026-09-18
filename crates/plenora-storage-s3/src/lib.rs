//! S3-compatible adapter for `plenora-storage-core`.

#![forbid(unsafe_code)]

mod list_validation;

use std::{borrow::Cow, collections::BTreeMap, net::SocketAddr, sync::Arc, time::Duration};

use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::{
    Attribute, AttributeValue, Attributes, ClientConfigKey, ClientOptions, CopyMode, CopyOptions,
    ObjectMeta, ObjectStore, ObjectStoreExt, PutMode, PutMultipartOptions, PutOptions,
    WriteMultipart,
    aws::{AmazonS3, AmazonS3Builder},
    client::{HttpClient, HttpConnector},
    path::Path,
};
use plenora_storage_core::{
    ArtifactMetadata, CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory,
    ErrorPhase, GetRequest, IntegrityMetadata, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest,
    RemoteEffect, RetryDisposition, StatRequest, StorageError, StorageProvider, StorageResult,
    TestResult, TransferResult, resolve_network_target, validate_object_key,
    validate_object_prefix,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use url::Url;

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

    /// Runs endpoint validation, DNS resolution, credential resolution and store
    /// construction under the caller's deadline and cancellation, as the other
    /// adapters already do for their connect phase.
    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<AmazonS3> {
        context
            .control
            .run(self.store(connection, context), ErrorPhase::Connect, false)
            .await
    }

    async fn store(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<AmazonS3> {
        let config = parse_config(connection)?;
        let url = validate_endpoint(&config.endpoint, context)?;
        let pinned = self.pinned_addresses(&url, &config, context).await?;
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
        // Installed unconditionally: the connector also refuses redirects and
        // proxies, which an endpoint reached by literal address needs just as
        // much as one reached by name.
        builder = builder.with_http_connector(PinnedDnsConnector { pinned });
        builder
            .build()
            .map_err(|error| map_store_error(error, ErrorPhase::Connect, false))
    }

    /// Resolves and validates every host the S3 client will actually contact,
    /// and returns the addresses the HTTP client must be pinned to.
    ///
    /// Validating a name and letting the HTTP client resolve it again would let
    /// a second, attacker-controlled resolution reach an address the policy just
    /// rejected.
    async fn pinned_addresses(
        &self,
        url: &Url,
        config: &S3ConnectionConfig,
        context: &OperationContext<'_>,
    ) -> StorageResult<Vec<(String, Vec<SocketAddr>)>> {
        let host = url.host_str().ok_or_else(|| {
            StorageError::invalid_configuration(
                "S3_ENDPOINT_HOST_MISSING",
                "S3 endpoint lacks a host",
            )
            .with_provider(PROVIDER_ID)
        })?;
        let port = url.port_or_known_default().ok_or_else(|| {
            StorageError::invalid_configuration(
                "S3_ENDPOINT_PORT_UNKNOWN",
                "S3 endpoint has no resolvable port",
            )
            .with_provider(PROVIDER_ID)
        })?;
        // With virtual hosted style the bucket becomes part of the request host,
        // so that name is validated and pinned too.
        let mut hosts = vec![host.to_owned()];
        if config.virtual_hosted_style {
            hosts.push(format!("{}.{host}", config.bucket));
        }
        let mut pinned = Vec::new();
        for host in hosts {
            let addresses =
                resolve_network_target(&host, port, context.policy.allow_private_network)
                    .await
                    .map_err(|error| error.with_provider(PROVIDER_ID))?;
            // A literal address needs no pinning: no name is ever resolved.
            if host.parse::<std::net::IpAddr>().is_err() {
                pinned.push((host, addresses));
            }
        }
        Ok(pinned)
    }
}

/// Builds the HTTP client `object_store` runs on, with the endpoint names bound
/// to the addresses the network policy already validated.
#[derive(Debug)]
struct PinnedDnsConnector {
    pinned: Vec<(String, Vec<SocketAddr>)>,
}

/// Mirrors the `object_store` client defaults this connector replaces.
const CLIENT_TIMEOUT: Duration = Duration::from_secs(30);
const CLIENT_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const CLIENT_USER_AGENT: &str = concat!("plenora-storage-tools/", env!("CARGO_PKG_VERSION"));

impl HttpConnector for PinnedDnsConnector {
    fn connect(&self, options: &ClientOptions) -> object_store::Result<HttpClient> {
        // The plaintext authorization stays with `ClientOptions` so that
        // replacing the connector cannot silently re-enable HTTP.
        let allow_http = options
            .get_config_value(&ClientConfigKey::AllowHttp)
            .is_some_and(|value| value == "true");
        let mut builder = reqwest::Client::builder()
            .https_only(!allow_http)
            // Redirects and proxies would resolve a host this connector never
            // validated, which is exactly the bypass the pinning exists to
            // prevent, so both are refused.
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .user_agent(CLIENT_USER_AGENT)
            .timeout(CLIENT_TIMEOUT)
            .connect_timeout(CLIENT_CONNECT_TIMEOUT)
            .http1_only()
            // Transparent compression rewrites `Content-Length`, which the
            // object size accounting depends on.
            .no_gzip()
            .no_brotli()
            .no_zstd()
            .no_deflate();
        for (host, addresses) in &self.pinned {
            builder = builder.resolve_to_addrs(host, addresses);
        }
        let client = builder
            .build()
            .map_err(|error| object_store::Error::Generic {
                store: "S3",
                source: Box::new(error),
            })?;
        Ok(HttpClient::new(list_validation::ValidatingClient(client)))
    }
}

#[async_trait]
impl StorageProvider for S3Provider {
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        policy: &plenora_storage_core::EngineConfig,
    ) -> StorageResult<()> {
        let config = parse_config(connection)?;
        let control = plenora_storage_core::ExecutionControl::default();
        validate_endpoint(
            &config.endpoint,
            &OperationContext {
                policy,
                control: &control,
            },
        )?;
        Ok(())
    }

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
                ("put_create_if_absent_atomic".to_owned(), "true".to_owned()),
                (
                    "copy_create_if_absent_atomic".to_owned(),
                    "false".to_owned(),
                ),
                ("copy_overwrite_false".to_owned(), "rejected".to_owned()),
                ("conditional_put".to_owned(), "native".to_owned()),
                ("atomic_publication".to_owned(), "true".to_owned()),
                ("list_order".to_owned(), "lexicographic".to_owned()),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let store = self.connect(connection, context).await?;
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
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
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
        let prefix = optional_prefix_path(request.prefix.as_deref())?;
        let offset = request
            .start_after
            .as_deref()
            .map(required_path)
            .transpose()?;
        let store = self.connect(connection, context).await?;
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
                        let object = public_metadata(metadata)?;
                        // S3 lists strictly increasing keys. A key that does not
                        // advance proves the path layer normalized a remote name
                        // — a trailing-slash marker, for example — so the page
                        // would report a name the bucket does not hold and the
                        // cursor would skip entries.
                        if objects
                            .last()
                            .is_some_and(|last: &ObjectMetadata| last.key >= object.key)
                        {
                            return Err(unrepresentable_key_error());
                        }
                        objects.push(object);
                    }
                    let truncated = objects.len() > limit;
                    if truncated {
                        objects.truncate(limit);
                    }
                    let next_start_after = truncated
                        .then(|| objects.last().map(|object| object.key.clone()))
                        .flatten();
                    Ok(ProviderListResult {
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
        let store = self.connect(connection, context).await?;
        context
            .control
            .run(
                async {
                    store
                        .head(&path)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                        .and_then(public_metadata)
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
        let store = self.connect(connection, context).await?;
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
            let sink_has_bytes = transferred > 0;
            let chunk = context
                .control
                .run(
                    async {
                        stream.next().await.transpose().map_err(|error| {
                            map_store_error(error, ErrorPhase::Read, sink_has_bytes)
                        })
                    },
                    ErrorPhase::Read,
                    sink_has_bytes,
                )
                .await?;
            let Some(chunk) = chunk else {
                break;
            };
            let next_transferred = transferred
                .checked_add(chunk.len() as u64)
                .ok_or_else(transfer_limit_error)?;
            if next_transferred > context.policy.max_transfer_bytes {
                return Err(if sink_has_bytes {
                    artifact_sink_limit_error()
                } else {
                    transfer_limit_error()
                });
            }
            context
                .control
                .run(
                    async {
                        sink.write_all(&chunk).await.map_err(|_| {
                            StorageError::new(
                                ErrorCategory::Io,
                                ErrorPhase::Write,
                                RemoteEffect::Unknown,
                                RetryDisposition::RequiresRecovery,
                                "ARTIFACT_WRITE_FAILED",
                                "artifact sink write failed and may be partial",
                            )
                        })
                    },
                    ErrorPhase::Write,
                    true,
                )
                .await?;
            transferred = next_transferred;
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
                            RemoteEffect::Unknown,
                            RetryDisposition::RequiresRecovery,
                            "ARTIFACT_FLUSH_FAILED",
                            "artifact sink flush failed and its published state is ambiguous",
                        )
                    })
                },
                ErrorPhase::Write,
                true,
            )
            .await?;
        let checksum = sha256_metadata(digest);
        Ok(TransferResult {
            key: request.key.clone(),
            bytes_transferred: transferred,
            artifact: ArtifactMetadata {
                content_type: None,
                size: Some(transferred),
                sha256: Some(checksum.value.clone()),
            },
            checksum,
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
        if request
            .content_length
            .is_some_and(|length| length > context.policy.max_transfer_bytes)
        {
            return Err(transfer_limit_error());
        }
        validate_metadata(request)?;
        let path = required_path(&request.key)?;
        let store = self.connect(connection, context).await?;
        if !request.overwrite {
            // Native create-if-absent is a single conditional PUT, so the whole
            // artifact must be buffered. That memory is bounded separately from
            // the streaming transfer limit.
            let buffered_limit = context
                .policy
                .max_buffered_put_bytes
                .min(context.policy.max_transfer_bytes);
            if request
                .content_length
                .is_some_and(|length| length > buffered_limit)
            {
                return Err(buffered_put_limit_error());
            }
            let mut payload = Vec::new();
            let mut buffer = vec![0_u8; 64 * 1024];
            let mut digest = Sha256::new();
            loop {
                let read = context
                    .control
                    .run(
                        async {
                            source.read(&mut buffer).await.map_err(|_| {
                                StorageError::new(
                                    ErrorCategory::Io,
                                    ErrorPhase::Read,
                                    RemoteEffect::None,
                                    RetryDisposition::Safe,
                                    "ARTIFACT_READ_FAILED",
                                    "artifact source read failed before conditional publication",
                                )
                            })
                        },
                        ErrorPhase::Read,
                        false,
                    )
                    .await?;
                if read == 0 {
                    break;
                }
                if payload.len().saturating_add(read) as u64 > buffered_limit {
                    return Err(buffered_put_limit_error());
                }
                digest.update(&buffer[..read]);
                payload.extend_from_slice(&buffer[..read]);
            }
            let transferred = payload.len() as u64;
            if request
                .content_length
                .is_some_and(|expected| expected != transferred)
            {
                return Err(StorageError::invalid_configuration(
                    "CONTENT_LENGTH_MISMATCH",
                    "artifact length differs from declared content_length",
                )
                .with_provider(PROVIDER_ID));
            }
            let options = PutOptions {
                mode: PutMode::Create,
                attributes: put_attributes(request),
                ..PutOptions::default()
            };
            let result = context
                .control
                .run(
                    async {
                        store
                            .put_opts(&path, payload.into(), options)
                            .await
                            .map_err(|error| map_store_error(error, ErrorPhase::Commit, true))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await?;
            let checksum = sha256_metadata(digest);
            return Ok(TransferResult {
                key: request.key.clone(),
                bytes_transferred: transferred,
                artifact: ArtifactMetadata {
                    content_type: request.content_type.clone(),
                    size: Some(transferred),
                    sha256: Some(checksum.value.clone()),
                },
                checksum,
                etag: result.e_tag,
                version: result.version,
            });
        }
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
                Err(error) => return Err(abort_multipart(writer, error).await),
            };
            if read == 0 {
                break;
            }
            transferred = match transferred.checked_add(read as u64) {
                Some(total) if total <= context.policy.max_transfer_bytes => total,
                _ => return Err(abort_multipart(writer, transfer_limit_error()).await),
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
                return Err(abort_multipart(writer, error).await);
            }
        }
        if request
            .content_length
            .is_some_and(|expected| expected != transferred)
        {
            let error = StorageError::invalid_configuration(
                "CONTENT_LENGTH_MISMATCH",
                "artifact length differs from declared content_length",
            )
            .with_provider(PROVIDER_ID);
            return Err(abort_multipart(writer, error).await);
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
        let checksum = sha256_metadata(digest);
        Ok(TransferResult {
            key: request.key.clone(),
            bytes_transferred: transferred,
            artifact: ArtifactMetadata {
                content_type: request.content_type.clone(),
                size: Some(transferred),
                sha256: Some(checksum.value.clone()),
            },
            checksum,
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
        let store = self.connect(connection, context).await?;
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
            return Err(StorageError::new(
                ErrorCategory::Unsupported,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "S3_COPY_CREATE_IF_ABSENT_UNSUPPORTED",
                "this S3 adapter cannot guarantee atomic create-if-absent for copy",
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
        let store = self.connect(connection, context).await?;
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
        // The destination is published from here on. The mapping is applied to
        // the outcome of `run`, so a deadline or cancellation raised during the
        // read-back is also reported as committed rather than unknown.
        context
            .control
            .run(
                async {
                    store
                        .head(&destination)
                        .await
                        .map_err(|error| map_store_error(error, ErrorPhase::Read, false))
                        .and_then(public_metadata)
                },
                ErrorPhase::Cleanup,
                true,
            )
            .await
            .map_err(|_| committed_verification_error())
    }
}

fn parse_config(connection: &ProviderConnection) -> StorageResult<S3ConnectionConfig> {
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

fn configuration_error() -> StorageError {
    StorageError::invalid_configuration(
        "S3_CONFIG_INVALID",
        "S3 connection configuration is invalid",
    )
    .with_provider(PROVIDER_ID)
}

fn validate_endpoint(endpoint: &str, context: &OperationContext<'_>) -> StorageResult<Url> {
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

fn required_path(value: &str) -> StorageResult<Path> {
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
fn optional_prefix_path(value: Option<&str>) -> StorageResult<Option<Path>> {
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

fn validate_metadata(request: &PutRequest) -> StorageResult<()> {
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

/// Publishes provider metadata only when the backend key is a valid public key.
///
/// A bucket can hold names that the public contract cannot express; emitting one
/// would produce an invalid `storage.list` output and a cursor the next page
/// rejects.
fn public_metadata(metadata: ObjectMeta) -> StorageResult<ObjectMetadata> {
    let key = metadata.location.to_string();
    validate_object_key(&key).map_err(|_| unrepresentable_key_error())?;
    Ok(ObjectMetadata {
        key,
        size: metadata.size,
        last_modified: Some(metadata.last_modified.to_rfc3339()),
        etag: metadata.e_tag,
        version: metadata.version,
    })
}

fn unrepresentable_key_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "OBJECT_KEY_UNREPRESENTABLE",
        "storage backend returned a name that is not a valid public object key",
    )
    .with_provider(PROVIDER_ID)
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

fn buffered_put_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "BUFFERED_PUT_LIMIT_EXCEEDED",
        "conditional upload exceeds the engine in-memory buffering limit",
    )
    .with_provider(PROVIDER_ID)
}

/// Independent budget for cleanup after a failure.
///
/// The caller's deadline may already have expired, and a cleanup that hangs must
/// not extend the operation without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

/// Aborts a started multipart upload and states the outcome the abort actually
/// achieved, instead of assuming the uploaded parts were removed.
///
/// The original cause is preserved either way: replacing it with a generic
/// cleanup error would hide why the upload failed.
async fn abort_multipart(writer: WriteMultipart, error: StorageError) -> StorageError {
    match tokio::time::timeout(CLEANUP_BUDGET, writer.abort()).await {
        Ok(Ok(())) => error.rolled_back(),
        _ => error.cleanup_unconfirmed("multipart_abort_failed"),
    }
}

fn committed_verification_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Execution,
        ErrorPhase::Cleanup,
        RemoteEffect::Committed,
        RetryDisposition::RequiresRecovery,
        "S3_COMMITTED_METADATA_UNAVAILABLE",
        "S3 destination was published but its metadata could not be read back",
    )
    .with_provider(PROVIDER_ID)
}

fn artifact_sink_limit_error() -> StorageError {
    StorageError::new(
        ErrorCategory::ResourceLimit,
        ErrorPhase::Write,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "TRANSFER_LIMIT_EXCEEDED",
        "storage transfer exceeded the engine byte limit after publishing sink bytes",
    )
    .with_provider(PROVIDER_ID)
}

fn map_store_error(error: object_store::Error, phase: ErrorPhase, mutating: bool) -> StorageError {
    // Preserve local response-validation failures through object_store's HTTP
    // error wrappers, without exposing backend URLs or response contents.
    let mut cause: &dyn std::error::Error = &error;
    loop {
        if let Some(error) = cause.downcast_ref::<StorageError>() {
            return error.clone();
        }
        let Some(source) = cause.source() else {
            break;
        };
        cause = source;
    }
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

#[cfg(test)]
mod tests {
    use super::{optional_prefix_path, required_path};

    #[test]
    fn object_keys_are_relative_and_normalized() {
        assert!(required_path("folder/object.bin").is_ok());
        assert!(required_path("").is_err());
        assert!(required_path(&"x".repeat(4_097)).is_err());
        assert!(required_path("../secret").is_err());
        assert!(required_path("folder//object.bin").is_err());
        assert!(required_path("folder/./object.bin").is_err());
        assert!(required_path("folder/").is_err());
    }

    #[test]
    fn an_empty_prefix_means_the_whole_namespace() {
        assert_eq!(optional_prefix_path(Some("")).expect("empty prefix"), None);
        assert!(
            optional_prefix_path(Some("incoming/"))
                .expect("prefix")
                .is_some()
        );
        assert!(optional_prefix_path(Some("/absolute")).is_err());
        assert!(optional_prefix_path(Some("../secret")).is_err());
    }
}
