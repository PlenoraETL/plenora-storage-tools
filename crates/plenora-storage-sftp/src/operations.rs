//! `StorageProvider` operations and their ordering guarantees.

use super::{
    AsyncRead, AsyncWrite, AsyncWriteExt, BTreeMap, CONFIG_CONTRACT, CopyRequest, DeleteRequest,
    DeleteResult, ErrorCategory, ErrorPhase, GetRequest, ObjectMetadata, OperationContext,
    PROVIDER_ID, ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult,
    PutRequest, RawSftpSession, SftpProvider, StatRequest, StorageError, StorageProvider,
    StorageResult, TestResult, TransferResult, async_trait, copy_with_control,
    directory_may_contain, key_matches_prefix, list_limit, map_sftp_error, map_ssh_connect_error,
    parse_config, public_metadata, relative_key, remote_path, scan_directory, transfer_io_error,
    transfer_result, validate_key, validate_prefix,
};

#[async_trait]
impl StorageProvider for SftpProvider {
    fn validate_connection(
        &self,
        connection: &ProviderConnection,
        policy: &plenora_storage_core::EngineConfig,
    ) -> StorageResult<()> {
        let config = parse_config(connection)?;
        if config.host_key_sha256.is_none() && !policy.allow_unverified_ssh {
            return Err(StorageError::invalid_configuration(
                "SFTP_HOST_KEY_REQUIRED",
                "SFTP requires a pinned SHA-256 host key fingerprint",
            )
            .with_provider(PROVIDER_ID));
        }
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
                ("api".to_owned(), "sftp-v3".to_owned()),
                (
                    "authentication".to_owned(),
                    "password,public_key".to_owned(),
                ),
                ("host_key_verification".to_owned(), "sha256-pin".to_owned()),
                ("streaming_get".to_owned(), "true".to_owned()),
                ("streaming_put".to_owned(), "true".to_owned()),
                ("put_create_if_absent_atomic".to_owned(), "true".to_owned()),
                ("copy_create_if_absent_atomic".to_owned(), "true".to_owned()),
                (
                    "atomic_publication".to_owned(),
                    "qualified_by_connection".to_owned(),
                ),
                (
                    "atomic_required".to_owned(),
                    "overwrite_true_only".to_owned(),
                ),
            ]),
        }
    }

    async fn test(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        let remote = context
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
                        .sftp
                        .canonicalize(&remote.root)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Probe, false))?;
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
        let limit = list_limit(request, context)?;
        validate_prefix(request.prefix.as_deref().unwrap_or_default())?;
        if let Some(start_after) = request.start_after.as_deref() {
            validate_key(start_after)?;
        }
        let prefix = request.prefix.as_deref().unwrap_or_default();
        // The high-level read_dir collects every batch before returning. A
        // raw session lets us enforce the scan budget between READDIR responses.
        let (_ssh, listing, root) = context
            .control
            .run(
                async {
                    let (ssh, root) = self.connect_ssh(connection, context).await?;
                    let channel = ssh
                        .channel_open_session()
                        .await
                        .map_err(map_ssh_connect_error)?;
                    channel
                        .request_subsystem(true, "sftp")
                        .await
                        .map_err(map_ssh_connect_error)?;
                    let listing = RawSftpSession::new(channel.into_stream());
                    listing
                        .init()
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Connect, false))?;
                    Ok((ssh, listing, root))
                },
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let mut stack = vec![root.clone()];
        // Only the smallest `limit + 1` matching keys are retained, so page size
        // bounds memory instead of the whole matching namespace.
        let mut selected = BTreeMap::new();
        let mut scanned = 0_usize;
        while let Some(directory) = stack.pop() {
            scan_directory(&listing, &directory, context, &mut scanned, |entry| {
                let metadata = entry.attrs;
                validate_key(&entry.filename)?;
                let path = remote_path(&directory, &entry.filename);
                let key = relative_key(&root, &path)?;
                if metadata.file_type().is_dir() {
                    // Directories that cannot hold a matching key are not
                    // entered at all.
                    if directory_may_contain(&key, prefix) {
                        stack.push(path);
                    }
                } else if metadata.file_type().is_file()
                    && key_matches_prefix(&key, prefix)
                    && request
                        .start_after
                        .as_ref()
                        .is_none_or(|offset| key > *offset)
                {
                    selected.insert(key.clone(), public_metadata(key, &metadata));
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

    async fn stat(
        &self,
        connection: &ProviderConnection,
        request: &StatRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let metadata = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .metadata(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        Ok(public_metadata(request.key.clone(), &metadata))
    }

    async fn get(
        &self,
        connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let mut file = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .open(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Read, false))
                },
                ErrorPhase::Read,
                false,
            )
            .await?;
        // Once bytes are offered to the caller-owned sink, failures can leave
        // an externally visible partial artifact.
        let (bytes_transferred, digest) = copy_with_control(&mut file, sink, context, true).await?;
        // Completing writes does not publish a caller-owned buffered sink.
        // Match the other adapters: flush under the same control, and preserve
        // ambiguity if the sink fails after accepting bytes.
        context
            .control
            .run(
                async {
                    sink.flush()
                        .await
                        .map_err(|_| transfer_io_error(ErrorPhase::Write, true))
                },
                ErrorPhase::Write,
                true,
            )
            .await?;
        Ok(transfer_result(
            request.key.clone(),
            bytes_transferred,
            digest,
        ))
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
        validate_key(&request.key)?;
        let remote = context
            .control
            .run(
                self.connect(connection, context),
                ErrorPhase::Connect,
                false,
            )
            .await?;
        let path = remote_path(&remote.root, &request.key);
        let result = context
            .control
            .run(
                async {
                    remote
                        .sftp
                        .remove_file(path)
                        .await
                        .map_err(|error| map_sftp_error(error, ErrorPhase::Commit, true))
                },
                ErrorPhase::Commit,
                true,
            )
            .await;
        match result {
            Ok(()) => Ok(DeleteResult {
                key: request.key.clone(),
                deleted: true,
            }),
            Err(error) if request.ignore_missing && error.category == ErrorCategory::NotFound => {
                Ok(DeleteResult {
                    key: request.key.clone(),
                    deleted: false,
                })
            }
            Err(error) => Err(error),
        }
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
