use std::{
    collections::{BTreeMap, HashMap},
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};

use async_trait::async_trait;
use plenora_storage_core::{
    ArtifactMetadata, ArtifactReference, ArtifactResolver, ArtifactSink, ArtifactSinkReference,
    ArtifactSource, CancellationToken, CopyRequest, DeleteRequest, DeleteResult,
    ERROR_CONTENT_TYPE, ERROR_CONTRACT, Engine, EngineConfig, ErrorCategory, ErrorPhase,
    ExecutionControl, GetRequest, IntegrityMetadata, JSON_CONTENT_TYPE, ListRequest,
    ObjectMetadata, OperationContext, ProviderCapabilities, ProviderConnection,
    ProviderListRequest, ProviderListResult, PutRequest, RUNTIME_OPERATIONS, RemoteEffect,
    RetryDisposition, RuntimeBinding, RuntimeInvocation, RuntimeRequestMetadata, SecretResolver,
    StatRequest, StorageError, StorageProvider, StorageResult, TestResult, TransferResult,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};

#[derive(Default)]
struct MemoryProvider {
    objects: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl MemoryProvider {
    fn metadata(key: &str, bytes: &[u8]) -> ObjectMetadata {
        ObjectMetadata {
            key: key.to_owned(),
            size: bytes.len() as u64,
            last_modified: None,
            etag: Some(format!("etag-{key}")),
            version: Some("provider-version-7".to_owned()),
        }
    }

    fn transfer(key: &str, bytes: &[u8], content_type: Option<String>) -> TransferResult {
        let sha256 = format!("{:x}", Sha256::digest(bytes));
        TransferResult {
            key: key.to_owned(),
            bytes_transferred: bytes.len() as u64,
            checksum: IntegrityMetadata {
                algorithm: "sha256".to_owned(),
                value: sha256.clone(),
            },
            artifact: ArtifactMetadata {
                content_type,
                size: Some(bytes.len() as u64),
                sha256: Some(sha256),
            },
            etag: Some(format!("etag-{key}")),
            version: Some("provider-version-7".to_owned()),
        }
    }
}

#[async_trait]
impl StorageProvider for MemoryProvider {
    fn id(&self) -> &'static str {
        "memory"
    }

    fn config_contract(&self) -> &'static str {
        "plenora-storage-memory-connection-v1"
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: self.id().to_owned(),
            config_contract: self.config_contract().to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .map(str::to_owned)
                .to_vec(),
            attributes: BTreeMap::from([
                ("create_if_absent_atomic".to_owned(), "true".to_owned()),
                ("atomic_publication".to_owned(), "true".to_owned()),
            ]),
        }
    }

    async fn test(
        &self,
        _connection: &ProviderConnection,
        _context: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        Ok(TestResult {
            provider: self.id().to_owned(),
            reachable: true,
        })
    }

    async fn list(
        &self,
        _connection: &ProviderConnection,
        request: &ProviderListRequest,
        _context: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
        let objects = self.objects.lock().expect("objects lock");
        let mut selected = objects
            .iter()
            .filter(|(key, _)| {
                request
                    .prefix
                    .as_ref()
                    .is_none_or(|prefix| key.starts_with(prefix))
            })
            .filter(|(key, _)| {
                request
                    .start_after
                    .as_ref()
                    .is_none_or(|start| *key > start)
            })
            .map(|(key, bytes)| Self::metadata(key, bytes))
            .collect::<Vec<_>>();
        drop(objects);
        let limit = request.max_items.unwrap_or(100);
        let truncated = selected.len() > limit;
        selected.truncate(limit);
        let next_start_after = truncated.then(|| {
            selected
                .last()
                .expect("a truncated page cannot be empty")
                .key
                .clone()
        });
        Ok(ProviderListResult {
            objects: selected,
            truncated,
            next_start_after,
        })
    }

    async fn stat(
        &self,
        _connection: &ProviderConnection,
        request: &StatRequest,
        _context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let objects = self.objects.lock().expect("objects lock");
        let bytes = objects.get(&request.key).cloned().ok_or_else(not_found)?;
        drop(objects);
        Ok(Self::metadata(&request.key, &bytes))
    }

    async fn get(
        &self,
        _connection: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        _context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let bytes = self
            .objects
            .lock()
            .expect("objects lock")
            .get(&request.key)
            .cloned()
            .ok_or_else(not_found)?;
        sink.write_all(&bytes).await.map_err(io_error)?;
        Ok(Self::transfer(
            &request.key,
            &bytes,
            Some("application/octet-stream".to_owned()),
        ))
    }

    async fn put(
        &self,
        _connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let mut bytes = Vec::new();
        source.read_to_end(&mut bytes).await.map_err(io_error)?;
        if !request.overwrite
            && self
                .objects
                .lock()
                .expect("objects lock")
                .contains_key(&request.key)
        {
            return Err(StorageError::new(
                ErrorCategory::Conflict,
                ErrorPhase::Prepare,
                RemoteEffect::None,
                RetryDisposition::Never,
                "OBJECT_EXISTS",
                "object exists",
            ));
        }
        self.objects
            .lock()
            .expect("objects lock")
            .insert(request.key.clone(), bytes.clone());
        if request.key == "slow.bin" {
            context
                .control
                .run(
                    async {
                        tokio::time::sleep(Duration::from_secs(5)).await;
                        Ok(())
                    },
                    ErrorPhase::Commit,
                    true,
                )
                .await?;
        }
        Ok(Self::transfer(
            &request.key,
            &bytes,
            request.content_type.clone(),
        ))
    }

    async fn delete(
        &self,
        _connection: &ProviderConnection,
        request: &DeleteRequest,
        _context: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        let deleted = self
            .objects
            .lock()
            .expect("objects lock")
            .remove(&request.key)
            .is_some();
        if !deleted && !request.ignore_missing {
            return Err(not_found());
        }
        Ok(DeleteResult {
            key: request.key.clone(),
            deleted,
        })
    }

    async fn copy(
        &self,
        _connection: &ProviderConnection,
        request: &CopyRequest,
        _context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let mut objects = self.objects.lock().expect("objects lock");
        if !request.overwrite && objects.contains_key(&request.destination_key) {
            return Err(StorageError::new(
                ErrorCategory::Conflict,
                ErrorPhase::Prepare,
                RemoteEffect::None,
                RetryDisposition::Never,
                "OBJECT_EXISTS",
                "object exists",
            ));
        }
        let bytes = objects
            .get(&request.source_key)
            .cloned()
            .ok_or_else(not_found)?;
        objects.insert(request.destination_key.clone(), bytes.clone());
        drop(objects);
        Ok(Self::metadata(&request.destination_key, &bytes))
    }
}

fn not_found() -> StorageError {
    StorageError::new(
        ErrorCategory::NotFound,
        ErrorPhase::Read,
        RemoteEffect::None,
        RetryDisposition::Never,
        "OBJECT_NOT_FOUND",
        "object not found",
    )
}

fn io_error(_: std::io::Error) -> StorageError {
    StorageError::new(
        ErrorCategory::Io,
        ErrorPhase::Write,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "TEST_IO",
        "test I/O failed",
    )
}

struct BytesReader {
    bytes: Vec<u8>,
    offset: usize,
}

impl AsyncRead for BytesReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let remaining = &self.bytes[self.offset..];
        let count = remaining.len().min(buffer.remaining());
        buffer.put_slice(&remaining[..count]);
        self.offset += count;
        Poll::Ready(Ok(()))
    }
}

struct SharedWriter(Arc<Mutex<Vec<u8>>>);

impl AsyncWrite for SharedWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        _context: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        self.0.lock().expect("sink lock").extend_from_slice(bytes);
        Poll::Ready(Ok(bytes.len()))
    }

    fn poll_flush(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(
        self: Pin<&mut Self>,
        _context: &mut Context<'_>,
    ) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

#[derive(Default)]
struct MemoryArtifacts {
    sources: Mutex<HashMap<String, Vec<u8>>>,
    sinks: Mutex<HashMap<String, Arc<Mutex<Vec<u8>>>>>,
}

impl MemoryArtifacts {
    fn source(&self, reference: &str, bytes: &[u8]) {
        self.sources
            .lock()
            .expect("sources lock")
            .insert(reference.to_owned(), bytes.to_vec());
    }

    fn sink_bytes(&self, reference: &str) -> Vec<u8> {
        self.sinks
            .lock()
            .expect("sinks lock")
            .get(reference)
            .expect("sink exists")
            .lock()
            .expect("sink bytes lock")
            .clone()
    }
}

#[async_trait]
impl ArtifactResolver for MemoryArtifacts {
    async fn open_source(&self, source: &ArtifactReference) -> StorageResult<ArtifactSource> {
        let bytes = self
            .sources
            .lock()
            .expect("sources lock")
            .get(&source.reference)
            .cloned()
            .ok_or_else(not_found)?;
        Ok(Box::pin(BytesReader { bytes, offset: 0 }))
    }

    async fn open_sink(&self, sink: &ArtifactSinkReference) -> StorageResult<ArtifactSink> {
        let mut sinks = self.sinks.lock().expect("sinks lock");
        if sinks.contains_key(&sink.reference) && !sink.overwrite {
            return Err(StorageError::new(
                ErrorCategory::Conflict,
                ErrorPhase::Prepare,
                RemoteEffect::None,
                RetryDisposition::Never,
                "ARTIFACT_EXISTS",
                "artifact exists",
            ));
        }
        let bytes = Arc::new(Mutex::new(Vec::new()));
        sinks.insert(sink.reference.clone(), bytes.clone());
        drop(sinks);
        Ok(Box::pin(SharedWriter(bytes)))
    }
}

struct TestSecrets;

impl SecretResolver for TestSecrets {
    fn authorize(&self, reference: &str) -> StorageResult<()> {
        if reference == "secret://storage/test" {
            Ok(())
        } else {
            Err(StorageError::new(
                ErrorCategory::Authorization,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "SECRET_DENIED",
                "secret reference denied",
            ))
        }
    }
}

fn engine() -> Engine {
    let mut engine = Engine::new(EngineConfig {
        allow_experimental_contracts: true,
        ..EngineConfig::default()
    });
    engine
        .register_provider(Arc::new(MemoryProvider::default()))
        .expect("register provider");
    engine
}

fn connection() -> Value {
    json!({
        "provider": "memory",
        "config_contract": "plenora-storage-memory-connection-v1",
        "config": {},
        "credential_ref": "secret://storage/test"
    })
}

fn invocation(operation: &str, payload: Value) -> RuntimeInvocation {
    let descriptor = RUNTIME_OPERATIONS
        .iter()
        .find(|descriptor| descriptor.operation == operation)
        .expect("operation descriptor");
    RuntimeInvocation {
        content_type: JSON_CONTENT_TYPE.to_owned(),
        metadata: RuntimeRequestMetadata {
            message_id: format!("message-{operation}"),
            capability_name: "plenora.storage-tools".to_owned(),
            capability_version: "1".to_owned(),
            operation: operation.to_owned(),
            operation_version: "1".to_owned(),
            input_contract: descriptor.input_contract.to_owned(),
            deadline: None,
            idempotency_key: None,
            correlation_id: "correlation-7".to_owned(),
        },
        payload,
    }
}

fn artifact_metadata(bytes: &[u8]) -> Value {
    json!({
        "content_type": "application/octet-stream",
        "size": bytes.len(),
        "sha256": format!("{:x}", Sha256::digest(bytes))
    })
}

#[tokio::test]
async fn runtime_binding_executes_all_seven_operations_and_artifacts() {
    let engine = engine();
    let artifacts = MemoryArtifacts::default();
    let secrets = TestSecrets;
    let binding = RuntimeBinding::new(&engine, &artifacts, &secrets);
    let bytes = b"runtime-storage";
    artifacts.source("artifact://source/runtime", bytes);

    let test = binding
        .invoke(
            invocation(
                "storage.test",
                json!({"schema_version": 1, "connection": connection()}),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&test, "plenora-storage-test-output-v1");
    assert_eq!(test.payload["reachable"], true);

    let put = binding
        .invoke(
            invocation(
                "storage.put",
                json!({
                    "schema_version": 1,
                    "connection": connection(),
                    "key": "objects/a.bin",
                    "artifact_source": {"reference": "artifact://source/runtime", "metadata": artifact_metadata(bytes)},
                    "overwrite": false,
                    "publication_policy": "atomic_required",
                    "content_type": null,
                    "content_length": null,
                    "metadata": {}
                }),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&put, "plenora-storage-put-output-v1");
    assert_eq!(
        put.payload["artifact"]["sha256"],
        artifact_metadata(bytes)["sha256"]
    );
    assert_ne!(put.payload["etag"], put.payload["version"]);
    assert_ne!(put.payload["etag"], put.payload["checksum"]["value"]);

    let stat = binding
        .invoke(
            invocation(
                "storage.stat",
                json!({"schema_version": 1, "connection": connection(), "key": "objects/a.bin"}),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&stat, "plenora-storage-stat-output-v1");

    let list = binding
        .invoke(
            invocation(
                "storage.list",
                json!({"schema_version": 1, "connection": connection(), "prefix": "objects/", "cursor": null, "max_items": 1}),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&list, "plenora-storage-list-output-v1");
    assert_eq!(list.payload["objects"].as_array().map(Vec::len), Some(1));

    let get = binding
        .invoke(
            invocation(
                "storage.get",
                json!({
                    "schema_version": 1,
                    "connection": connection(),
                    "key": "objects/a.bin",
                    "artifact_sink": {"reference": "artifact://sink/runtime", "overwrite": false, "metadata": artifact_metadata(bytes)}
                }),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&get, "plenora-storage-get-output-v1");
    assert_eq!(artifacts.sink_bytes("artifact://sink/runtime"), bytes);

    let copy = binding
        .invoke(
            invocation(
                "storage.copy",
                json!({
                    "schema_version": 1,
                    "connection": connection(),
                    "source_key": "objects/a.bin",
                    "destination_key": "objects/b.bin",
                    "overwrite": false,
                    "publication_policy": "atomic_required"
                }),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&copy, "plenora-storage-copy-output-v1");

    let delete = binding
        .invoke(
            invocation(
                "storage.delete",
                json!({"schema_version": 1, "connection": connection(), "key": "objects/b.bin", "ignore_missing": false}),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_success(&delete, "plenora-storage-delete-output-v1");
    assert_eq!(delete.payload["deleted"], true);
}

#[tokio::test]
async fn runtime_route_and_security_mismatches_fail_closed() {
    let engine = engine();
    let artifacts = MemoryArtifacts::default();
    let binding = RuntimeBinding::new(&engine, &artifacts, &TestSecrets);
    let base = invocation(
        "storage.test",
        json!({"schema_version": 1, "connection": connection()}),
    );
    let mut invalid = Vec::new();
    let mut route = base.clone();
    route.metadata.capability_name = "other".to_owned();
    invalid.push(route);
    let mut route = base.clone();
    route.metadata.operation_version = "2".to_owned();
    invalid.push(route);
    let mut route = base.clone();
    route.metadata.input_contract = "wrong".to_owned();
    invalid.push(route);
    let mut route = base.clone();
    route.content_type = "text/plain".to_owned();
    invalid.push(route);
    let mut route = base.clone();
    route.metadata.idempotency_key = Some("unsupported".to_owned());
    invalid.push(route);
    for item in invalid {
        let result = binding.invoke(item, CancellationToken::new()).await;
        assert_error(&result, "RUNTIME_ROUTE_INVALID");
    }

    let inline = invocation(
        "storage.test",
        json!({"schema_version": 1, "connection": {"provider": "memory", "config_contract": "x", "config": {"Password": "inline"}, "credential_ref": "secret://storage/test"}}),
    );
    assert_error(
        &binding.invoke(inline, CancellationToken::new()).await,
        "RUNTIME_PAYLOAD_SECURITY_VIOLATION",
    );
    let local = invocation(
        "storage.get",
        json!({"schema_version": 1, "connection": connection(), "key": "a", "artifact_sink": {"reference": "C:\\private\\a.bin", "overwrite": false, "metadata": {"content_type": null, "size": null, "sha256": null}}}),
    );
    assert_error(
        &binding.invoke(local, CancellationToken::new()).await,
        "RUNTIME_PAYLOAD_SECURITY_VIOLATION",
    );
}

#[tokio::test]
async fn runtime_deadline_and_cancellation_preserve_ambiguous_remote_effect() {
    let engine = engine();
    let artifacts = MemoryArtifacts::default();
    let binding = RuntimeBinding::new(&engine, &artifacts, &TestSecrets);
    artifacts.source("artifact://source/slow", b"slow");
    let slow = || {
        invocation(
            "storage.put",
            json!({
                "schema_version": 1,
                "connection": connection(),
                "key": "slow.bin",
                "artifact_source": {"reference": "artifact://source/slow", "metadata": artifact_metadata(b"slow")},
                "overwrite": true,
                "publication_policy": "atomic_required",
                "content_type": null,
                "content_length": null,
                "metadata": {}
            }),
        )
    };

    let pre_cancelled = CancellationToken::new();
    pre_cancelled.cancel();
    let result = binding.invoke(slow(), pre_cancelled).await;
    assert_error_effect(&result, "CANCELLED", "none", "safe");

    let cancelled = CancellationToken::new();
    let cancel_after_start = cancelled.clone();
    let (result, ()) = tokio::join!(binding.invoke(slow(), cancelled), async move {
        tokio::time::sleep(Duration::from_millis(25)).await;
        cancel_after_start.cancel();
    });
    assert_error_effect(&result, "CANCELLED", "unknown", "requires_recovery");

    let mut expired = slow();
    expired.metadata.deadline = Some("2000-01-01T00:00:00Z".to_owned());
    let result = binding.invoke(expired, CancellationToken::new()).await;
    assert_error_effect(&result, "TIMEOUT", "none", "safe");

    let mut future = slow();
    future.metadata.deadline = Some(
        (OffsetDateTime::now_utc() + time::Duration::milliseconds(40))
            .format(&Rfc3339)
            .expect("format deadline"),
    );
    let result = binding.invoke(future, CancellationToken::new()).await;
    assert_error_effect(&result, "TIMEOUT", "unknown", "requires_recovery");
}

#[tokio::test]
async fn runtime_serializes_terminal_provider_errors_as_plenora_error_v1() {
    let engine = engine();
    let artifacts = MemoryArtifacts::default();
    let binding = RuntimeBinding::new(&engine, &artifacts, &TestSecrets);
    let result = binding
        .invoke(
            invocation(
                "storage.stat",
                json!({"schema_version": 1, "connection": connection(), "key": "missing"}),
            ),
            CancellationToken::new(),
        )
        .await;
    assert_error(&result, "OBJECT_NOT_FOUND");
    assert_eq!(result.metadata.output_contract, ERROR_CONTRACT);
}

#[tokio::test]
async fn list_cursor_is_opaque_bounded_and_scoped_to_connection_and_parameters() {
    let engine = engine();
    let provider = ProviderConnection {
        provider: "memory".to_owned(),
        config_contract: "plenora-storage-memory-connection-v1".to_owned(),
        config: json!({}),
        credential_ref: "secret://storage/test".to_owned(),
    };
    let source = MemoryArtifacts::default();
    source.source("artifact://source/a", b"a");
    let binding = RuntimeBinding::new(&engine, &source, &TestSecrets);
    for key in ["p/a", "p/b"] {
        let mut request = invocation(
            "storage.put",
            json!({
                "schema_version": 1, "connection": connection(), "key": key,
                "artifact_source": {"reference": "artifact://source/a", "metadata": artifact_metadata(b"a")},
                "overwrite": false, "publication_policy": "atomic_required", "content_type": null,
                "content_length": null, "metadata": {}
            }),
        );
        request.metadata.message_id = format!("put-{key}");
        assert_eq!(
            binding
                .invoke(request, CancellationToken::new())
                .await
                .content_type,
            JSON_CONTENT_TYPE
        );
    }
    let control = ExecutionControl::default();
    let first = engine
        .list(
            &provider,
            &ListRequest {
                prefix: Some("p/".to_owned()),
                cursor: None,
                max_items: Some(1),
            },
            &control,
        )
        .await
        .expect("first page");
    let cursor = first.next_cursor.expect("opaque next cursor");
    assert!(cursor.starts_with("cursor://"));
    assert!(cursor.len() <= 512);
    let mismatch = engine
        .list(
            &provider,
            &ListRequest {
                prefix: Some("other/".to_owned()),
                cursor: Some(cursor),
                max_items: Some(1),
            },
            &control,
        )
        .await
        .expect_err("cursor reuse with another scope must fail");
    assert_eq!(mismatch.code, "LIST_CURSOR_SCOPE_MISMATCH");
}

fn assert_success(result: &plenora_storage_core::RuntimeResultEnvelope, contract: &str) {
    assert_eq!(result.content_type, JSON_CONTENT_TYPE);
    assert_eq!(result.metadata.output_contract, contract);
    assert_eq!(result.metadata.correlation_id, "correlation-7");
}

fn assert_error(result: &plenora_storage_core::RuntimeResultEnvelope, code: &str) {
    assert_eq!(result.content_type, ERROR_CONTENT_TYPE);
    assert_eq!(result.metadata.output_contract, ERROR_CONTRACT);
    assert_eq!(result.metadata.correlation_id, "correlation-7");
    assert_eq!(result.payload["code"], code);
}

fn assert_error_effect(
    result: &plenora_storage_core::RuntimeResultEnvelope,
    code: &str,
    effect: &str,
    retry: &str,
) {
    assert_error(result, code);
    assert_eq!(result.payload["remote_effect"], effect);
    assert_eq!(result.payload["retry"]["kind"], retry);
}
