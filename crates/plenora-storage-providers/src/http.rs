use crate::common::invalid;
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

#[derive(Debug)]
pub struct Connector {
    host: String,
    addresses: Vec<SocketAddr>,
    allow_http: bool,
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
        })
    }
    pub(crate) fn client(&self) -> Result<reqwest::Client, reqwest::Error> {
        reqwest::Client::builder()
            .https_only(!self.allow_http)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .resolve_to_addrs(&self.host, &self.addresses)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(60))
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd()
            .build()
    }
}
impl HttpConnector for Connector {
    fn connect(&self, _: &ClientOptions) -> object_store::Result<HttpClient> {
        self.client()
            .map(|client| HttpClient::new(crate::azure_listing::ValidatingClient(client)))
            .map_err(|error| object_store::Error::Generic {
                store: "Plenora",
                source: Box::new(error),
            })
    }
}
