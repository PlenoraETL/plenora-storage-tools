//! `StorageProvider` operations and their ordering guarantees.

use super::{
    AsyncRead, AsyncWrite, BTreeMap, CONFIG_CONTRACT, CopyRequest, DeleteRequest, DeleteResult,
    ErrorCategory, ErrorPhase, FTPS_CONFIG_CONTRACT, FTPS_PROVIDER_ID, FtpProvider, GetRequest,
    ObjectMetadata, OperationContext, PROVIDER_ID, ProviderCapabilities, ProviderConnection,
    ProviderListRequest, ProviderListResult, PutRequest, RemoteEffect, RetryDisposition,
    StatRequest, StorageError, StorageProvider, StorageResult, TestResult, TransferResult,
    async_trait, committed_mismatch_error, configuration_error, copy_with_control,
    directory_may_contain, ftp_object_exists, key_matches_prefix, list_limit, list_name_error,
    map_ftp_error, parse_config, public_metadata, scan_directory, stat_file, transfer_result,
    validate_key, validate_prefix,
};

#[async_trait]
impl StorageProvider for FtpProvider {
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        policy: &plenora_storage_core::EngineConfig,
    ) -> StorageResult<()> {
        connection.validate()?;
        let config = parse_config(connection)?;
        if connection.provider != self.id()
            || connection.config_contract != self.config_contract()
            || (!self.secure && config.tls_ca_pem.is_some())
        {
            return Err(configuration_error());
        }
        if !self.secure && !policy.allow_insecure_ftp {
            return Err(StorageError::invalid_configuration(
                "INSECURE_FTP_FORBIDDEN",
                "plain FTP requires explicit engine authorization",
            )
            .with_provider(PROVIDER_ID));
        }
        Ok(())
    }

    fn id(&self) -> &'static str {
        if self.secure {
            FTPS_PROVIDER_ID
        } else {
            PROVIDER_ID
        }
    }

    fn config_contract(&self) -> &'static str {
        if self.secure {
            FTPS_CONFIG_CONTRACT
        } else {
            CONFIG_CONTRACT
        }
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: self.id().to_owned(),
            config_contract: self.config_contract().to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .into_iter()
                .map(str::to_owned)
                .collect(),
            attributes: BTreeMap::from([
                ("api".to_owned(), self.id().to_owned()),
                (
                    "transport_security".to_owned(),
                    if self.secure {
                        "tls-verified"
                    } else {
                        "none-opt-in"
                    }
                    .to_owned(),
                ),
                ("authentication".to_owned(), "password".to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "true".to_owned()),
                ("put_create_if_absent_atomic".to_owned(), "false".to_owned()),
                (
                    "copy_create_if_absent_atomic".to_owned(),
                    "false".to_owned(),
                ),
                ("overwrite_false".to_owned(), "rejected".to_owned()),
                ("atomic_publication".to_owned(), "false".to_owned()),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let outcome = async {
            let mut remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            context
                .control
                .run(
                    async {
                        remote
                            .ftp
                            .noop()
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Probe, false))?;
                        Ok(TestResult {
                            provider: self.id().to_owned(),
                            reachable: true,
                        })
                    },
                    ErrorPhase::Probe,
                    false,
                )
                .await
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(self.id()))
    }

    async fn list(
        &self,
        connection: &ProviderConnection,
        request: &ProviderListRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
        let outcome = async {
            let limit = list_limit(request, context)?;
            validate_prefix(request.prefix.as_deref().unwrap_or_default())?;
            if let Some(start_after) = request.start_after.as_deref() {
                validate_key(start_after)?;
            }
            let mut remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let prefix = request.prefix.as_deref().unwrap_or_default();
            let mut stack = vec![".".to_owned()];
            // Only the smallest `limit + 1` matching keys are retained, so page size
            // bounds memory instead of the whole matching namespace.
            let mut selected = BTreeMap::new();
            let mut scanned = 0_usize;
            while let Some(directory) = stack.pop() {
                scan_directory(&mut remote.ftp, &directory, context, &mut scanned, |file| {
                    if matches!(file.name(), "." | "..") {
                        return Ok(());
                    }
                    let key = if directory == "." {
                        file.name().to_owned()
                    } else {
                        format!("{directory}/{}", file.name())
                    };
                    // A server-supplied name that cannot be a public key must not be
                    // published: it would break the output contract and produce a
                    // cursor the next page rejects.
                    if validate_key(&key).is_err() {
                        return Err(list_name_error());
                    }
                    if file.is_directory() {
                        if directory_may_contain(&key, prefix) {
                            stack.push(key);
                        }
                    } else if file.is_file()
                        && key_matches_prefix(&key, prefix)
                        && request
                            .start_after
                            .as_ref()
                            .is_none_or(|offset| key > *offset)
                    {
                        selected.insert(key.clone(), public_metadata(key, &file));
                        if selected.len() > limit.saturating_add(1)
                            && let Some(highest) = selected.keys().next_back().cloned()
                        {
                            selected.remove(&highest);
                        }
                    }
                    Ok(())
                })
                .await?;
            }
            let mut objects = selected.into_values().collect::<Vec<_>>();
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
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(self.id()))
    }

    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let outcome = async {
            validate_key(&request.key)?;
            let mut remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            stat_file(&mut remote.ftp, &request.key, context).await
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(self.id()))
    }

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let outcome = async {
            validate_key(&request.key)?;
            let mut remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            let expected_size = stat_file(&mut remote.ftp, &request.key, context)
                .await?
                .size;
            let mut stream = context
                .control
                .run(
                    async {
                        remote
                            .ftp
                            .retr_as_stream(&request.key)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            // Once bytes are offered to the caller-owned sink, failures can leave
            // an externally visible partial artifact.
            let (bytes_transferred, digest) =
                copy_with_control(&mut stream, sink, context, true).await?;
            context
                .control
                .run(
                    async {
                        stream
                            .finish()
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await?;
            if bytes_transferred != expected_size {
                return Err(committed_mismatch_error().with_provider(self.id()));
            }
            Ok(transfer_result(
                request.key.clone(),
                bytes_transferred,
                digest,
            ))
        }
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(self.id()))
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
        let outcome = async {
            validate_key(&request.key)?;
            let mut remote = context
                .control
                .run(
                    self.connect(connection, context),
                    ErrorPhase::Connect,
                    false,
                )
                .await?;
            // Absence is proved before the mutation. FTP reports "not found" and
            // several other unavailability conditions with the same 550 status, so
            // a failed `rm` cannot be read as "the object was already missing".
            let exists = context
                .control
                .run(
                    ftp_object_exists(&mut remote.ftp, &request.key, context),
                    ErrorPhase::Probe,
                    false,
                )
                .await?;
            if !exists {
                return if request.ignore_missing {
                    Ok(DeleteResult {
                        key: request.key.clone(),
                        deleted: false,
                    })
                } else {
                    Err(StorageError::new(
                        ErrorCategory::NotFound,
                        ErrorPhase::Probe,
                        RemoteEffect::None,
                        RetryDisposition::Never,
                        "FTP_OBJECT_NOT_FOUND",
                        "FTP object was not found",
                    )
                    .with_provider(PROVIDER_ID))
                };
            }
            context
                .control
                .run(
                    async {
                        remote
                            .ftp
                            .rm(&request.key)
                            .await
                            .map_err(|error| map_ftp_error(error, ErrorPhase::Commit, true))
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
        .await;
        outcome.map_err(|error: StorageError| error.with_provider(self.id()))
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
