use crate::{
    common::{Backend, ProviderFactory, Reader, failure, invalid, page, parse, select},
    http,
};
use async_trait::async_trait;
use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use object_store::{
    Attribute, AttributeValue, Attributes, ObjectStore, ObjectStoreExt, PutMode, PutOptions,
    azure::MicrosoftAzureBuilder, path::Path,
};
use plenora_storage_core::{
    CredentialResolver, EngineConfig, ErrorCategory, ErrorPhase, ObjectMetadata, OperationContext,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, StorageError,
    StorageResult, validate_object_key,
};
use serde::Deserialize;
use std::{collections::BTreeMap, pin::Pin, sync::Arc};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AzureConnectionConfig {
    pub endpoint: String,
    pub account: String,
    pub container: String,
}
pub struct Azure;

fn name(s: &str) -> StorageResult<()> {
    if s.is_empty()
        || s.len() > 255
        || !s
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        Err(invalid("CLOUD_NAME_INVALID"))
    } else {
        Ok(())
    }
}
#[async_trait]
impl ProviderFactory for Azure {
    const ID: &'static str = "azure";
    const CONTRACT: &'static str = "plenora-storage-azure-connection-v1";
    const ATOMIC: bool = true;
    const METADATA: bool = true;
    fn validate(connection: &ProviderConnection, policy: &EngineConfig) -> StorageResult<()> {
        let config: AzureConnectionConfig = parse(connection)?;
        http::endpoint(&config.endpoint, policy)?;
        name(&config.account)?;
        name(&config.container)
    }
    async fn connect(
        connection: &ProviderConnection,
        credentials: &dyn CredentialResolver,
        context: &OperationContext<'_>,
    ) -> StorageResult<Box<dyn Backend>> {
        let cfg: AzureConnectionConfig = parse(connection)?;
        let url = http::endpoint(&cfg.endpoint, context.policy)?;
        let connector = http::Connector::new(&url, context).await?;
        let material = credentials.resolve(&connection.credential_ref)?;
        let builder = MicrosoftAzureBuilder::new()
            .with_account(cfg.account)
            .with_container_name(cfg.container)
            .with_endpoint(cfg.endpoint)
            .with_allow_http(context.policy.allow_insecure_http)
            .with_http_connector(connector);
        let builder = if let Some(token) = material.optional("bearer_token") {
            builder.with_bearer_token_authorization(token)
        } else {
            builder.with_access_key(material.required("account_key")?)
        };
        Ok(Box::new(Cloud {
            store: Arc::new(builder.build().map_err(|error| store_error(error, false))?),
        }))
    }
}
struct Cloud {
    store: Arc<dyn ObjectStore>,
}
struct CloudReader {
    stream: Pin<Box<dyn Stream<Item = object_store::Result<Bytes>> + Send>>,
}
#[async_trait]
impl Reader for CloudReader {
    async fn next(&mut self) -> StorageResult<Option<Bytes>> {
        self.stream
            .next()
            .await
            .transpose()
            .map_err(|error| store_error(error, false))
    }
}
#[async_trait]
impl Backend for Cloud {
    async fn test(&mut self) -> StorageResult<()> {
        self.store
            .list(None)
            .next()
            .await
            .transpose()
            .map_err(|error| store_error(error, false))?;
        Ok(())
    }
    async fn list(
        &mut self,
        request: &ProviderListRequest,
        limit: usize,
    ) -> StorageResult<ProviderListResult> {
        let prefix = request
            .prefix
            .as_deref()
            .filter(|policy| !policy.is_empty())
            .map(|policy| path(policy.trim_end_matches('/')))
            .transpose()?;
        let mut stream = self.store.list(prefix.as_ref());
        let mut selected = BTreeMap::new();
        // Azure listing order must not be inferred from a generic store.
        // Retain only the smallest page; deadline bounds total enumeration time.
        while let Some(item) = stream.next().await {
            let item = item.map_err(|error| store_error(error, false))?;
            select(&mut selected, public_meta(item)?, request, limit)?;
        }
        Ok(page(selected, limit))
    }
    async fn stat(&mut self, key: &str) -> StorageResult<ObjectMetadata> {
        public_meta(
            self.store
                .head(&path(key)?)
                .await
                .map_err(|error| store_error(error, false))?,
        )
    }
    async fn get(&mut self, key: &str) -> StorageResult<(ObjectMetadata, Box<dyn Reader>)> {
        let result = self
            .store
            .get(&path(key)?)
            .await
            .map_err(|error| store_error(error, false))?;
        let meta = public_meta(result.meta.clone())?;
        Ok((
            meta,
            Box::new(CloudReader {
                stream: result.into_stream(),
            }),
        ))
    }
    async fn put(&mut self, request: &PutRequest, data: Bytes) -> StorageResult<()> {
        let mut attributes = Attributes::new();
        if let Some(value) = &request.content_type {
            attributes.insert(Attribute::ContentType, AttributeValue::from(value.clone()));
        }
        for (key, value) in &request.metadata {
            attributes.insert(
                Attribute::Metadata(key.clone().into()),
                AttributeValue::from(value.clone()),
            );
        }
        let options = PutOptions {
            mode: if request.overwrite {
                PutMode::Overwrite
            } else {
                PutMode::Create
            },
            attributes,
            ..PutOptions::default()
        };
        self.store
            .put_opts(&path(&request.key)?, data.into(), options)
            .await
            .map_err(|error| store_error(error, true))?;
        Ok(())
    }
    async fn delete(&mut self, key: &str) -> StorageResult<()> {
        // Some object stores return success for an absent key. Probe preserves
        // the v1 ignore_missing=false behavior (concurrent deletion is allowed).
        self.stat(key).await?;
        self.store
            .delete(&path(key)?)
            .await
            .map_err(|error| store_error(error, true))
    }
}
fn path(key: &str) -> StorageResult<Path> {
    validate_object_key(key)?;
    let policy = Path::parse(key).map_err(|_| invalid("OBJECT_KEY_INVALID"))?;
    if policy.as_ref() != key {
        return Err(invalid("OBJECT_KEY_INVALID"));
    }
    Ok(policy)
}
fn public_meta(meta: object_store::ObjectMeta) -> StorageResult<ObjectMetadata> {
    let key = meta.location.to_string();
    validate_object_key(&key)?;
    Ok(ObjectMetadata {
        key,
        size: meta.size,
        last_modified: Some(meta.last_modified.to_rfc3339()),
        etag: meta.e_tag,
        version: meta.version,
    })
}
fn store_error(error: object_store::Error, mutating: bool) -> StorageError {
    let category =
        match error {
            object_store::Error::NotFound { .. } => ErrorCategory::NotFound,
            object_store::Error::AlreadyExists { .. }
            | object_store::Error::Precondition { .. } => ErrorCategory::Conflict,
            object_store::Error::PermissionDenied { .. } => ErrorCategory::Authorization,
            object_store::Error::Unauthenticated { .. } => ErrorCategory::Authentication,
            object_store::Error::NotSupported { .. }
            | object_store::Error::NotImplemented { .. } => ErrorCategory::Unsupported,
            _ => ErrorCategory::Io,
        };
    failure(
        category,
        if mutating {
            ErrorPhase::Commit
        } else {
            ErrorPhase::Read
        },
        mutating,
    )
}
