//! `StorageProvider` operations and their ordering guarantees.

use super::{
    ArtifactMetadata, AsyncRead, AsyncWrite, AsyncWriteExt, BTreeMap, CONFIG_CONTRACT, CopyRequest,
    DeleteRequest, DeleteResult, Digest, ErrorCategory, ErrorPhase, GetRequest, ObjectMetadata,
    ObjectStore, ObjectStoreExt, OperationContext, PROVIDER_ID, ProviderCapabilities,
    ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect,
    RetryDisposition, S3Provider, Sha256, StatRequest, StorageError, StorageProvider,
    StorageResult, StreamExt, TestResult, TransferResult, artifact_sink_limit_error, async_trait,
    map_store_error, optional_prefix_path, parse_config, public_metadata, required_path,
    sha256_metadata, transfer_limit_error, unrepresentable_key_error, validate_endpoint,
};

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

    #[allow(
        clippy::too_many_lines,
        reason = "Keep sink byte accounting, partial effects and flush ordering visible together"
    )]
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
        super::publication::put(self, connection, request, source, context).await
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
        super::publication::copy(self, connection, request, context).await
    }
}
