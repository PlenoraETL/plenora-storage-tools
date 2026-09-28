//! S3-compatible adapter for `plenora-storage-core`.
//!
//! Overwriting uploads use multipart streaming; create-if-absent uploads are
//! buffered within the engine limit. A failed completion may already have
//! published the object: callers must inspect the error's effect and retry axes.
//! The adapter disables automatic HTTP retries and pins validated DNS addresses.

#![forbid(unsafe_code)]

mod errors;
mod operations;
mod publication;
mod transfer;
mod validation;
use errors::{
    artifact_sink_limit_error, buffered_put_limit_error, committed_verification_error,
    configuration_error, map_store_error, transfer_limit_error, unrepresentable_key_error,
};
use transfer::{abort_multipart, public_metadata, put_attributes, sha256_metadata};
use validation::{
    optional_prefix_path, parse_config, required_path, validate_endpoint, validate_metadata,
};

mod list_validation;

#[cfg(fuzzing)]
/// Exercise S3 listing validation in instrumented builds.
pub fn fuzz_listing(data: &[u8]) {
    list_validation::fuzz_listing(data);
}

use std::{borrow::Cow, collections::BTreeMap, net::SocketAddr, sync::Arc, time::Duration};

use async_trait::async_trait;
use futures_util::StreamExt;
use object_store::{
    Attribute, AttributeValue, Attributes, ClientConfigKey, ClientOptions, CopyMode, CopyOptions,
    ObjectMeta, ObjectStore, ObjectStoreExt, PutMode, PutMultipartOptions, PutOptions, RetryConfig,
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

/// Stable provider identifier for dispatch and capability discovery.
pub const PROVIDER_ID: &str = "s3";
/// Versioned connection contract accepted by this adapter.
pub const CONFIG_CONTRACT: &str = "plenora-storage-s3-connection-v1";

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Non-secret S3 addressing; credentials are supplied by the configured resolver.
pub struct S3ConnectionConfig {
    /// Base service URL; HTTP requires explicit engine authorization.
    pub endpoint: String,
    /// Bucket name within the configured endpoint; no embedded credentials.
    pub bucket: String,
    #[serde(default = "default_region")]
    /// S3 signing region; defaults to us-east-1.
    pub region: String,
    /// Address the bucket through a host prefix instead of a path segment.
    #[serde(default)]
    pub virtual_hosted_style: bool,
}

fn default_region() -> String {
    "us-east-1".to_owned()
}

/// S3 adapter that resolves credentials for each operation's connection.
pub struct S3Provider {
    credentials: Arc<dyn CredentialResolver>,
}

impl S3Provider {
    /// Retains the resolver without opening a connection or resolving secrets.
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
            // The host owns retries and reconciliation. In particular, an
            // HTTP 5xx response does not prove that a mutation was not applied.
            .with_retry(RetryConfig {
                max_retries: 0,
                ..RetryConfig::default()
            })
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
            .retry(reqwest::retry::never())
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

/// Independent budget for cleanup after a failure.
///
/// The caller's deadline may already have expired, and a cleanup that hangs must
/// not extend the operation without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
