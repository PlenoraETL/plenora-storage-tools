use crate::common::invalid;
#[cfg(feature = "azure")]
use object_store::{
    ClientOptions,
    client::{HttpClient, HttpConnector},
};
use plenora_storage_core::{EngineConfig, OperationContext, StorageResult, resolve_network_target};
use std::{net::SocketAddr, time::Duration};
use url::Url;

pub fn endpoint(value: &str, policy: &EngineConfig) -> StorageResult<Url> {
    let url = Url::parse(value).map_err(|_| invalid("ENDPOINT_INVALID"))?;
    if value.len() > 2048
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.scheme(), "http" | "https")
        || (url.scheme() == "http" && !policy.allow_insecure_http)
    {
        return Err(invalid("ENDPOINT_FORBIDDEN"));
    }
    Ok(url)
}

/// Limit on inactivity of one HTTP request without a deadline.
///
/// A request fails as `timeout` when, for this long, the transport takes no
/// frame of its body and no frame of the answer arrives: while sending, while
/// waiting for the answer and while reading it. A transfer that keeps moving
/// in either direction is never cut short. A frame counts once the transport
/// has taken it, which may be into socket buffers the server never reads. With
/// a deadline every request is bounded by the time remaining instead.
pub const HTTP_READ_TIMEOUT_WITHOUT_DEADLINE: Duration = Duration::from_secs(300);
/// Upper bound on establishing a connection; the time remaining before the
/// deadline applies when it is shorter.
pub const HTTP_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// Timeouts of the HTTP client for one operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HttpTimeouts {
    /// Whole request, from sending to the end of the response body.
    pub total: Option<Duration>,
    /// Inactivity in either direction (see `crate::watched`).
    pub idle: Option<Duration>,
    /// Establishing the connection.
    pub connect: Duration,
}

impl HttpTimeouts {
    /// With a deadline every request may last as long as the time remaining
    /// (the operation control ends it exactly at the deadline); without one,
    /// only inactivity is bounded. Before 3.0.0 every request had a fixed
    /// 60 s total limit, whatever the deadline.
    pub fn for_remaining(remaining: Option<Duration>) -> Self {
        remaining.map_or(
            Self {
                total: None,
                idle: Some(HTTP_READ_TIMEOUT_WITHOUT_DEADLINE),
                connect: HTTP_CONNECT_TIMEOUT,
            },
            |remaining| Self {
                total: Some(remaining),
                idle: None,
                connect: HTTP_CONNECT_TIMEOUT.min(remaining),
            },
        )
    }
}

#[derive(Clone, Debug)]
pub struct Connector {
    host: String,
    addresses: Vec<SocketAddr>,
    allow_http: bool,
    timeouts: HttpTimeouts,
}
impl Connector {
    pub(crate) async fn new(url: &Url, context: &OperationContext<'_>) -> StorageResult<Self> {
        let host = url.host_str().ok_or_else(|| invalid("ENDPOINT_INVALID"))?;
        let addresses = resolve_network_target(
            host,
            url.port_or_known_default().unwrap_or(443),
            context.policy.allow_private_network,
        )
        .await?;
        Ok(Self {
            host: host.to_owned(),
            addresses,
            allow_http: context.policy.allow_insecure_http,
            timeouts: HttpTimeouts::for_remaining(context.control.remaining()),
        })
    }

    /// The client of the operation. It has no read timeout: reqwest starts
    /// one with the request and does not re-arm it with the bytes sent, so it
    /// would cut an upload that keeps moving. Requests go through
    /// `crate::watched` with [`Self::idle`] instead.
    pub(crate) fn client(&self) -> Result<reqwest::Client, reqwest::Error> {
        let builder = reqwest::Client::builder()
            .https_only(!self.allow_http)
            .no_proxy()
            .retry(reqwest::retry::never())
            .redirect(reqwest::redirect::Policy::none())
            .resolve_to_addrs(&self.host, &self.addresses)
            .connect_timeout(self.timeouts.connect)
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd();
        match self.timeouts.total {
            Some(total) => builder.timeout(total),
            None => builder,
        }
        .build()
    }

    /// The inactivity limit of every request of the operation.
    pub(crate) const fn idle(&self) -> Option<Duration> {
        self.timeouts.idle
    }
}
#[cfg(feature = "azure")]
impl HttpConnector for Connector {
    fn connect(&self, _: &ClientOptions) -> object_store::Result<HttpClient> {
        self.client()
            .map(|client| {
                HttpClient::new(crate::azure_listing::ValidatingClient {
                    client,
                    idle: self.idle(),
                })
            })
            .map_err(|error| object_store::Error::Generic {
                store: "Plenora",
                source: Box::new(error),
            })
    }
}

#[cfg(test)]
#[path = "http_tests.rs"]
mod tests;
