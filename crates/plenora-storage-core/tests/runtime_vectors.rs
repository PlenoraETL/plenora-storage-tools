//! Runtime Vectors 1.0: the storage fixtures of the adopted `plenora-contracts`
//! revision, executed through the public runtime binding.
//!
//! The fixtures are copied byte for byte into `contracts/upstream/runtime-v1`
//! and pinned by SHA-256 below. Their payloads are illustrative: the scripted
//! `s3` provider returns the outcomes the fixtures describe, so these tests
//! prove admission, dispatch and result/error mapping, not provider behavior.
use std::{
    collections::{BTreeMap, HashMap},
    error::Error,
    fs,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
};

use async_trait::async_trait;
use jsonschema::{Retrieve, Uri};
use plenora_storage_core::{
    ArtifactReference, ArtifactResolver, ArtifactSink, ArtifactSinkReference, ArtifactSource,
    CancellationToken, CopyRequest, DeleteRequest, DeleteResult, ERROR_CONTENT_TYPE,
    ERROR_CONTRACT, Engine, EngineConfig, ExecutionId, GetRequest, ObjectMetadata,
    OperationContext, ProviderCapabilities, ProviderConnection, ProviderListRequest,
    ProviderListResult, PutRequest, RUNTIME_OPERATIONS, RuntimeBinding, RuntimeInvocation,
    RuntimeResultEnvelope, SecretResolver, StatRequest, StorageError, StorageProvider,
    StorageResult, TestResult, TransferResult,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};

/// Adopted contracts revision; must equal `contracts/upstream/source.json`.
const ADOPTED_REVISION: &str = "1e902dfaab5819c1d9ce785878d5b26dbeae48b3";

/// SHA-256 of each storage fixture in `vectors/runtime-v1` at the adopted
/// revision, computed from the upstream Git blobs.
const PINNED_VECTORS: [(&str, &str); 12] = [
    (
        "storage-copy-request.json",
        "03477805d6a46662c747e87eb0e70b02c37f6930074cd1b68c7cd7d0a3d0e671",
    ),
    (
        "storage-delete-request.json",
        "7bf78c79aeed240c5960dab024f905d567fec0caa2b57aaf4b91fe92d066eb8a",
    ),
    (
        "storage-get-partial-error.json",
        "c00a8f602723eb349494adaf11077b45dc8b9ad7548029f988b846769049dea2",
    ),
    (
        "storage-get-request.json",
        "d289989ba4aae6f309f1fcc8bae83c7b9a1f7d65e8a16ad564b678e9a67181b7",
    ),
    (
        "storage-get-success.json",
        "05f9d558e69ce0095ca3ac914dc12a8288882b8dd7b868ed195a57cff826f396",
    ),
    (
        "storage-list-request.json",
        "320ed4ed881f15842d0688901c6a68bd36bcbee098853faeb4c4981f212e7541",
    ),
    (
        "storage-list-success.json",
        "5c7dc162030a0f6c82e1e9342f081f461b1be4e2cff6f1d187cddf17e1691f7f",
    ),
    (
        "storage-put-request.json",
        "1eb229c37fc04a898a999e8fadae8d800074cbc4d625764bfc359b4bb59587c9",
    ),
    (
        "storage-put-success.json",
        "f83e30f04d6a1ce6b39ac0778a8e11046a5015940537ce5b3b7c13eae8c26341",
    ),
    (
        "storage-put-unknown-error.json",
        "36f902beb412ae5e763ea42b32b75b8a0307b4b6139ba602ca869bc606d2cbfe",
    ),
    (
        "storage-stat-request.json",
        "63db5b8ef1562ee506444e039cbb97a9b53cddeb13d4ed1bc76241cf152c87b6",
    ),
    (
        "storage-test-request.json",
        "2b6745f3286d4a09972995bf79e9e263ba54496cfa003ed483000168c3cd1b8b",
    ),
];

/// Normative text, structural schema and the common Python binding map
/// (read by the installed-SDK tests) copied from the same revision.
const PINNED_FILES: [(&str, &str); 3] = [
    (
        "runtime-vector-v1.schema.json",
        "3ec6148b8fb3db111ca0d1ff29283c3f8a46a7727c3051a777a123c102ca1f9e",
    ),
    (
        "RUNTIME-VECTORS-1.0.md",
        "d7f9c42b2b6d50ee844425b77cbce59db8bc6a945726b67635d62884aacabd34",
    ),
    (
        "python-sdk-v1.json",
        "ffc5680bb9a595ba9162693e75f70d664ebaadf684d4338da7d72ea00569fbd8",
    ),
];

const VECTOR_CREDENTIAL: &str = "secret://storage/vector";
const PUT_SOURCE: &str = "artifact://input/storage-put-vector";
const GET_SINK: &str = "artifact://output/storage-get-vector";
/// Byte length declared by every storage fixture.
const VECTOR_BYTES: usize = 17;
/// Bytes the scripted provider writes before the partial get fails.
const PARTIAL_BYTES: usize = 5;

fn contracts_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts")
}

fn upstream(name: &str) -> PathBuf {
    contracts_root().join("upstream").join(name)
}

fn vector(name: &str) -> Value {
    let path = upstream("runtime-v1").join(name);
    serde_json::from_slice(
        &fs::read(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
    )
    .unwrap_or_else(|error| panic!("{} is not JSON: {error}", path.display()))
}

fn sha256_file(path: &Path) -> String {
    hex::encode(Sha256::digest(
        fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
    ))
}

#[derive(Clone)]
struct Retriever(HashMap<String, Value>);

impl Retrieve for Retriever {
    fn retrieve(&self, uri: &Uri<String>) -> Result<Value, Box<dyn Error + Send + Sync>> {
        self.0
            .get(uri.as_str())
            .cloned()
            .ok_or_else(|| format!("unknown schema {uri}").into())
    }
}

/// Validators keyed by contract identifier: the component-owned storage
/// schemas, the common error schema and the runtime vector schema.
fn validators() -> HashMap<String, jsonschema::Validator> {
    let mut documents = HashMap::new();
    let mut paths = fs::read_dir(contracts_root().join("schemas"))
        .expect("schema directory")
        .map(|entry| entry.expect("schema entry").path())
        .collect::<Vec<_>>();
    paths.push(upstream("error-v1.schema.json"));
    paths.push(upstream("runtime-vector-v1.schema.json"));
    for path in paths {
        let document: Value = serde_json::from_slice(&fs::read(&path).expect("schema")).unwrap();
        let id = document["$id"].as_str().expect("schema $id").to_owned();
        documents.insert(id, document);
    }
    documents
        .iter()
        .map(|(id, schema)| {
            let name = id
                .rsplit('/')
                .next()
                .and_then(|file| file.strip_suffix(".schema.json"))
                .expect("schema file name")
                .to_owned();
            let validator = jsonschema::draft202012::options()
                .with_retriever(Retriever(documents.clone()))
                .should_validate_formats(true)
                .build(schema)
                .expect("schema compiles");
            (name, validator)
        })
        .collect()
}

fn assert_valid(
    validators: &HashMap<String, jsonschema::Validator>,
    contract: &str,
    value: &Value,
) {
    let validator = validators
        .get(contract)
        .unwrap_or_else(|| panic!("no schema for {contract}"));
    let errors = validator
        .iter_errors(value)
        .map(|error| error.to_string())
        .collect::<Vec<_>>();
    assert!(errors.is_empty(), "{contract}: {errors:?}");
}

/// Builds the invocation exactly as a transport would: content type, metadata
/// and payload of the fixture, nothing added.
fn invocation(vector: &Value) -> RuntimeInvocation {
    serde_json::from_value(json!({
        "content_type": vector["content_type"],
        "metadata": vector["metadata"],
        "payload": vector["payload"],
    }))
    .expect("request vector is a runtime invocation")
}

fn result_from(vector: &Value) -> RuntimeResultEnvelope {
    serde_json::from_value(json!({
        "content_type": vector["content_type"],
        "metadata": vector["metadata"],
        "payload": vector["payload"],
    }))
    .expect("result vector is a runtime result envelope")
}

#[derive(Default)]
struct Calls(Mutex<Vec<&'static str>>);

impl Calls {
    fn record(&self, call: &'static str) {
        self.0.lock().expect("calls lock").push(call);
    }

    fn take(&self) -> Vec<&'static str> {
        std::mem::take(&mut *self.0.lock().expect("calls lock"))
    }
}

/// `s3` provider scripted with the outcomes of the storage fixtures. It never
/// opens a network connection. `failing` selects the error fixtures of the
/// transfers: get writes part of the bytes and fails, put fails after reading.
#[derive(Default)]
struct ScriptedS3 {
    calls: Arc<Calls>,
    failing: bool,
    /// Error a put returns instead of the fixture outcomes, for proposed vectors.
    put_error: Option<StorageError>,
}

fn scripted_object(key: &str) -> ObjectMetadata {
    ObjectMetadata {
        key: key.to_owned(),
        size: VECTOR_BYTES as u64,
        last_modified: Some("2030-01-01T00:00:00Z".to_owned()),
        etag: Some("provider-etag-not-a-digest".to_owned()),
        version: Some("provider-version-17".to_owned()),
    }
}

#[async_trait]
impl StorageProvider for ScriptedS3 {
    fn id(&self) -> &'static str {
        "s3"
    }

    fn config_contract(&self) -> &'static str {
        "plenora-storage-s3-connection-v1"
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            provider: self.id().to_owned(),
            config_contract: self.config_contract().to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .map(str::to_owned)
                .to_vec(),
            attributes: BTreeMap::new(),
        }
    }

    async fn test(
        &self,
        _: &ProviderConnection,
        _: &OperationContext<'_>,
    ) -> StorageResult<TestResult> {
        self.calls.record("test");
        Ok(TestResult {
            provider: self.id().to_owned(),
            reachable: true,
        })
    }

    async fn list(
        &self,
        _: &ProviderConnection,
        request: &ProviderListRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<ProviderListResult> {
        self.calls.record("list");
        assert_eq!(request.prefix.as_deref(), Some("incoming/"));
        assert_eq!(request.max_items, Some(100));
        let objects: Vec<ObjectMetadata> = serde_json::from_value(
            vector("storage-list-success.json")["payload"]["objects"].clone(),
        )
        .expect("fixture objects");
        Ok(ProviderListResult {
            next_start_after: objects.last().map(|object| object.key.clone()),
            objects,
            truncated: true,
        })
    }

    async fn stat(
        &self,
        _: &ProviderConnection,
        request: &StatRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        self.calls.record("stat");
        assert_eq!(request.key, "incoming/vector.bin");
        Ok(scripted_object(&request.key))
    }

    async fn get(
        &self,
        _: &ProviderConnection,
        request: &GetRequest,
        sink: &mut (dyn AsyncWrite + Send + Unpin),
        _: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        self.calls.record("get");
        assert_eq!(request.key, "incoming/vector.bin");
        if self.failing {
            sink.write_all(&[0x5a; PARTIAL_BYTES])
                .await
                .expect("memory sink");
            return Err(fixture_error("storage-get-partial-error.json"));
        }
        sink.write_all(&[0x5a; VECTOR_BYTES])
            .await
            .expect("memory sink");
        Ok(
            serde_json::from_value(vector("storage-get-success.json")["payload"].clone())
                .expect("fixture transfer result"),
        )
    }

    async fn put(
        &self,
        _: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        _: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        self.calls.record("put");
        assert_eq!(request.key, "outgoing/vector.bin");
        let mut bytes = Vec::new();
        source.read_to_end(&mut bytes).await.expect("memory source");
        assert_eq!(bytes.len(), VECTOR_BYTES);
        assert_eq!(request.content_length, Some(VECTOR_BYTES as u64));
        assert!(!request.overwrite);
        if let Some(error) = &self.put_error {
            return Err(error.clone());
        }
        if self.failing {
            return Err(fixture_error("storage-put-unknown-error.json"));
        }
        Ok(
            serde_json::from_value(vector("storage-put-success.json")["payload"].clone())
                .expect("fixture transfer result"),
        )
    }

    async fn delete(
        &self,
        _: &ProviderConnection,
        request: &DeleteRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        self.calls.record("delete");
        assert_eq!(request.key, "outgoing/vector.bin");
        assert!(!request.ignore_missing);
        Ok(DeleteResult {
            key: request.key.clone(),
            deleted: true,
        })
    }

    async fn copy(
        &self,
        _: &ProviderConnection,
        request: &CopyRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        self.calls.record("copy");
        assert_eq!(request.source_key, "incoming/vector.bin");
        assert_eq!(request.destination_key, "outgoing/vector.bin");
        assert!(request.overwrite);
        Ok(scripted_object(&request.destination_key))
    }
}

/// The error of a fixture as a provider of this component returns it: every
/// axis of the unmodified payload, but no `execution_id`, which no provider
/// or engine of this component produces.
fn fixture_error(name: &str) -> StorageError {
    let mut error: StorageError = serde_json::from_value(vector(name)["payload"].clone())
        .expect("fixture error is a storage error");
    error.execution_id = None;
    error
}

struct MemoryWriter(Arc<Mutex<Vec<u8>>>);

impl AsyncWrite for MemoryWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        self.0.lock().expect("sink lock").extend_from_slice(bytes);
        Poll::Ready(Ok(bytes.len()))
    }

    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

struct MemoryReader(Vec<u8>);

impl AsyncRead for MemoryReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let count = self.0.len().min(buffer.remaining());
        let rest = self.0.split_off(count);
        buffer.put_slice(&self.0);
        self.0 = rest;
        Poll::Ready(Ok(()))
    }
}

/// Host resolvers that record every access, so that a rejected route can be
/// shown to have reached neither artifacts nor secrets.
#[derive(Default)]
struct Host {
    calls: Arc<Calls>,
    sink: Arc<Mutex<Vec<u8>>>,
}

#[async_trait]
impl ArtifactResolver for Host {
    async fn open_source(&self, source: &ArtifactReference) -> StorageResult<ArtifactSource> {
        self.calls.record("open_source");
        assert_eq!(source.reference, PUT_SOURCE);
        Ok(Box::pin(MemoryReader(vec![0x5a; VECTOR_BYTES])))
    }

    async fn open_sink(&self, sink: &ArtifactSinkReference) -> StorageResult<ArtifactSink> {
        self.calls.record("open_sink");
        assert_eq!(sink.reference, GET_SINK);
        Ok(Box::pin(MemoryWriter(self.sink.clone())))
    }
}

impl SecretResolver for Host {
    fn authorize(&self, reference: &str) -> StorageResult<()> {
        self.calls.record("authorize");
        assert_eq!(reference, VECTOR_CREDENTIAL);
        Ok(())
    }
}

/// Engine with the scripted provider; provider and host share one call log.
fn fixture() -> (Engine, Host) {
    scripted(false)
}

/// Engine whose scripted transfers produce the error fixtures.
fn failing_fixture() -> (Engine, Host) {
    scripted(true)
}

fn scripted(failing: bool) -> (Engine, Host) {
    scripted_with(failing, None)
}

fn scripted_with(failing: bool, put_error: Option<StorageError>) -> (Engine, Host) {
    let calls = Arc::new(Calls::default());
    let mut engine = Engine::new(EngineConfig::default());
    engine
        .register_provider(Arc::new(ScriptedS3 {
            calls: calls.clone(),
            failing,
            put_error,
        }))
        .expect("register provider");
    (
        engine,
        Host {
            calls,
            sink: Arc::default(),
        },
    )
}

async fn invoke(engine: &Engine, host: &Host, request: RuntimeInvocation) -> RuntimeResultEnvelope {
    RuntimeBinding::new(engine, host, host)
        .invoke(request, CancellationToken::new())
        .await
}

/// RT-012: the result is a new message whose causation is the request and
/// whose correlation is the request's; operation, version, output contract and
/// content type are those of the expected fixture. The fixtures carry no
/// causation (it is optional), so it is the only key added to theirs.
fn assert_result_identity(result: &RuntimeResultEnvelope, request: &Value, expected: &Value) {
    let metadata = serde_json::to_value(&result.metadata).unwrap();
    assert_ne!(
        metadata["plenora.message.id"],
        request["metadata"]["plenora.message.id"]
    );
    assert_eq!(
        metadata["plenora.message.causation_id"],
        request["metadata"]["plenora.message.id"]
    );
    assert_eq!(
        metadata["plenora.trace.correlation_id"],
        request["metadata"]["plenora.trace.correlation_id"]
    );
    for key in [
        "plenora.capability.operation",
        "plenora.operation.version",
        "plenora.output.contract",
    ] {
        assert_eq!(metadata[key], expected["metadata"][key], "{key}");
    }
    assert_eq!(result.content_type, expected["content_type"]);
    let mut keys = metadata.as_object().unwrap().keys().collect::<Vec<_>>();
    let causation = "plenora.message.causation_id".to_owned();
    let mut expected_keys = expected["metadata"]
        .as_object()
        .unwrap()
        .keys()
        .chain([&causation])
        .collect::<Vec<_>>();
    keys.sort();
    expected_keys.sort();
    assert_eq!(keys, expected_keys, "result metadata keys");
}

#[test]
fn storage_vectors_are_the_pinned_copies_of_the_adopted_revision() {
    let source: Value =
        serde_json::from_slice(&fs::read(upstream("source.json")).expect("source.json")).unwrap();
    assert_eq!(source["revision"], ADOPTED_REVISION);

    let mut present = fs::read_dir(upstream("runtime-v1"))
        .expect("vector directory")
        .map(|entry| {
            entry
                .expect("vector entry")
                .file_name()
                .into_string()
                .unwrap()
        })
        .collect::<Vec<_>>();
    present.sort_unstable();
    let pinned = PINNED_VECTORS.map(|(name, _)| name.to_owned()).to_vec();
    assert_eq!(present, pinned, "every copied fixture must be pinned");

    for (name, digest) in PINNED_VECTORS {
        assert_eq!(
            sha256_file(&upstream("runtime-v1").join(name)),
            digest,
            "{name}"
        );
    }
    for (name, digest) in PINNED_FILES {
        assert_eq!(sha256_file(&upstream(name)), digest, "{name}");
    }
}

/// RUNTIME-VECTORS-1.0 §5: every fixture of an advertised operation. The
/// storage fixtures cover the seven operations, all advertised by the binding.
#[test]
fn storage_vectors_match_runtime_and_component_schemas() {
    let validators = validators();
    for (name, _) in PINNED_VECTORS {
        let fixture = vector(name);
        assert_valid(&validators, "runtime-vector-v1", &fixture);
        let operation = fixture["metadata"]["plenora.capability.operation"]
            .as_str()
            .expect("operation");
        let descriptor = RUNTIME_OPERATIONS
            .iter()
            .find(|descriptor| descriptor.operation == operation)
            .unwrap_or_else(|| panic!("{name}: storage operation is not advertised"));
        let contract = match fixture["kind"].as_str() {
            Some("request") => {
                assert_eq!(
                    fixture["metadata"]["plenora.input.contract"],
                    descriptor.input_contract
                );
                assert_eq!(fixture["content_type"], descriptor.content_type);
                invocation(&fixture);
                descriptor.input_contract
            }
            Some("success") => {
                assert_eq!(
                    fixture["metadata"]["plenora.output.contract"],
                    descriptor.output_contract
                );
                assert_eq!(fixture["content_type"], descriptor.content_type);
                result_from(&fixture);
                descriptor.output_contract
            }
            Some("error") => {
                assert_eq!(
                    fixture["metadata"]["plenora.output.contract"],
                    ERROR_CONTRACT
                );
                assert_eq!(fixture["content_type"], ERROR_CONTENT_TYPE);
                result_from(&fixture);
                "error-v1"
            }
            other => panic!("{name}: unexpected kind {other:?}"),
        };
        assert_valid(&validators, contract, &fixture["payload"]);
    }
}

#[tokio::test]
async fn get_request_vector_produces_the_get_success_vector() {
    let (engine, host) = fixture();
    let request = vector("storage-get-request.json");
    let expected = vector("storage-get-success.json");
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    assert_eq!(result.payload, expected["payload"]);
    assert_eq!(host.sink.lock().unwrap().len(), VECTOR_BYTES);
    assert_eq!(host.calls.take(), ["authorize", "open_sink", "get"]);
}

/// `plenora-error-v1` error fixtures deserialize unmodified, `execution_id`
/// included, and serialize back to the same document.
#[test]
fn error_vectors_are_storage_errors_including_the_execution_id() {
    for name in [
        "storage-get-partial-error.json",
        "storage-put-unknown-error.json",
    ] {
        let payload = vector(name)["payload"].clone();
        let error: StorageError = serde_json::from_value(payload.clone())
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(
            error.execution_id.as_ref().map(ExecutionId::as_str),
            Some("storage-vector-execution-1"),
            "{name}"
        );
        assert_eq!(serde_json::to_value(&error).unwrap(), payload, "{name}");
    }
}

/// The fixture's `execution_id` is optional in `plenora-error-v1` and is not
/// produced by this component, so it is the only key absent from the result.
fn without_execution_id(payload: &Value) -> Value {
    let mut payload = payload.clone();
    payload
        .as_object_mut()
        .unwrap()
        .remove("execution_id")
        .expect("fixture carries an execution_id");
    payload
}

#[tokio::test]
async fn put_request_vector_produces_the_put_success_vector() {
    let (engine, host) = fixture();
    let request = vector("storage-put-request.json");
    let expected = vector("storage-put-success.json");
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    assert_eq!(result.payload, expected["payload"]);
    assert_eq!(host.calls.take(), ["authorize", "open_source", "put"]);
}

#[tokio::test]
async fn put_request_vector_maps_to_the_put_unknown_error_vector() {
    let (engine, host) = failing_fixture();
    let request = vector("storage-put-request.json");
    let expected = vector("storage-put-unknown-error.json");
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    assert_eq!(result.payload, without_execution_id(&expected["payload"]));
    assert_valid(&validators(), "error-v1", &result.payload);
    assert_eq!(host.calls.take(), ["authorize", "open_source", "put"]);
}

/// A get that fails after part of the transfer reached the host sink reports
/// the fixture's `partial`/`never`: the remote object is not mutated by a get,
/// and the sink holds a known, unfinalized prefix.
#[tokio::test]
async fn get_request_vector_maps_to_the_get_partial_error_vector() {
    let (engine, host) = failing_fixture();
    let request = vector("storage-get-request.json");
    let expected = vector("storage-get-partial-error.json");
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    assert_eq!(result.payload, without_execution_id(&expected["payload"]));
    assert_valid(&validators(), "error-v1", &result.payload);
    assert_eq!(host.sink.lock().unwrap().len(), PARTIAL_BYTES);
    assert_eq!(host.calls.take(), ["authorize", "open_sink", "get"]);
}

/// Request fixtures without a result fixture: the result keeps the request
/// identity and the operation's output contract, and its payload is valid
/// against the component-owned output schema.
async fn assert_request_vector_succeeds(name: &str, calls: &[&str]) -> Value {
    let (engine, host) = fixture();
    let request = vector(name);
    let operation = request["metadata"]["plenora.capability.operation"]
        .as_str()
        .unwrap();
    let descriptor = RUNTIME_OPERATIONS
        .iter()
        .find(|descriptor| descriptor.operation == operation)
        .unwrap();
    let expected = json!({
        "content_type": descriptor.content_type,
        "metadata": {
            "plenora.message.id": request["metadata"]["plenora.message.id"],
            "plenora.capability.operation": operation,
            "plenora.operation.version": request["metadata"]["plenora.operation.version"],
            "plenora.output.contract": descriptor.output_contract,
            "plenora.trace.correlation_id": request["metadata"]["plenora.trace.correlation_id"],
        },
    });
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    assert_valid(&validators(), descriptor.output_contract, &result.payload);
    assert_eq!(host.calls.take(), calls, "{name}");
    result.payload
}

#[tokio::test]
async fn test_stat_copy_and_delete_request_vectors_produce_valid_results() {
    let payload =
        assert_request_vector_succeeds("storage-test-request.json", &["authorize", "test"]).await;
    assert_eq!(payload, json!({"provider": "s3", "reachable": true}));
    let payload =
        assert_request_vector_succeeds("storage-stat-request.json", &["authorize", "stat"]).await;
    assert_eq!(payload["key"], "incoming/vector.bin");
    let payload =
        assert_request_vector_succeeds("storage-copy-request.json", &["authorize", "copy"]).await;
    assert_eq!(payload["key"], "outgoing/vector.bin");
    let payload =
        assert_request_vector_succeeds("storage-delete-request.json", &["authorize", "delete"])
            .await;
    assert_eq!(
        payload,
        json!({"key": "outgoing/vector.bin", "deleted": true})
    );
}

/// The fixture cursor was issued by another engine. Cursors are engine-local
/// (`Engine::list`), so it is refused before the provider is reached; the
/// same request without the foreign cursor yields the success fixture with a
/// cursor issued by this engine.
#[tokio::test]
async fn list_request_vector_refuses_a_foreign_cursor_and_produces_the_list_success_vector() {
    let validators = validators();
    let (engine, host) = fixture();
    let request = vector("storage-list-request.json");
    let expected = vector("storage-list-success.json");

    let refused = invoke(&engine, &host, invocation(&request)).await;
    assert_eq!(refused.content_type, ERROR_CONTENT_TYPE);
    assert_eq!(refused.payload["code"], "LIST_CURSOR_INVALID_OR_EXPIRED");
    assert_eq!(refused.payload["remote_effect"], "none");
    assert_valid(&validators, "error-v1", &refused.payload);
    assert_eq!(host.calls.take(), ["authorize"]);

    let mut first_page = request.clone();
    first_page["payload"]
        .as_object_mut()
        .unwrap()
        .remove("cursor");
    let result = invoke(&engine, &host, invocation(&first_page)).await;
    assert_result_identity(&result, &request, &expected);
    assert_valid(
        &validators,
        "plenora-storage-list-output-v1",
        &result.payload,
    );
    let cursor = result.payload["next_cursor"].as_str().expect("next cursor");
    assert_ne!(cursor, expected["payload"]["next_cursor"]);
    let mut payload = result.payload.clone();
    payload["next_cursor"] = expected["payload"]["next_cursor"].clone();
    assert_eq!(payload, expected["payload"]);
    assert_eq!(host.calls.take(), ["authorize", "list"]);
}

/// An advertised operation other than the request's.
fn other_operation(request: &Value) -> &'static str {
    let operation = request["metadata"]["plenora.capability.operation"]
        .as_str()
        .unwrap();
    RUNTIME_OPERATIONS
        .iter()
        .find(|descriptor| descriptor.operation != operation)
        .unwrap()
        .operation
}

/// The request fixtures, one per storage operation.
const REQUEST_VECTORS: [&str; 7] = [
    "storage-copy-request.json",
    "storage-delete-request.json",
    "storage-get-request.json",
    "storage-list-request.json",
    "storage-put-request.json",
    "storage-stat-request.json",
    "storage-test-request.json",
];

/// Invalid values for each routing key of a request fixture: empty, foreign,
/// case-changed, belonging to another operation, or a non-canonical version.
fn routing_mutations(request: &Value) -> [(&'static str, Vec<String>); 5] {
    let text = |key: &str| request["metadata"][key].as_str().unwrap().to_owned();
    let operation = text("plenora.capability.operation");
    let contract = text("plenora.input.contract");
    let other = RUNTIME_OPERATIONS
        .iter()
        .find(|descriptor| descriptor.operation != operation)
        .unwrap();
    let owned = |values: &[&str]| values.iter().map(|value| (*value).to_owned()).collect();
    [
        (
            "plenora.capability.name",
            owned(&[
                "",
                "plenora.rest-tools",
                "PLENORA.STORAGE-TOOLS",
                "plenora.storage-tools ",
            ]),
        ),
        (
            "plenora.capability.version",
            owned(&["", "0", "2", "01", "+1", "1.0", " 1"]),
        ),
        (
            "plenora.capability.operation",
            vec![
                String::new(),
                "storage.unknown".to_owned(),
                operation.to_uppercase(),
                other.operation.to_owned(),
            ],
        ),
        (
            "plenora.operation.version",
            owned(&["", "0", "2", "01", "+1", "1.0"]),
        ),
        (
            "plenora.input.contract",
            vec![
                String::new(),
                other.input_contract.to_owned(),
                contract.replace("-v1", "-v2"),
                contract.to_uppercase(),
            ],
        ),
    ]
}

/// RUNTIME-VECTORS-1.0 §5: capability, operation, operation version and input
/// contract routing that is missing or invalid fails closed. A missing key is
/// rejected by the invocation DTO; an invalid value is rejected by the binding
/// as `protocol`/`validate`/`none` before secrets, artifacts or providers.
#[tokio::test]
async fn routing_mutations_of_every_request_vector_fail_closed() {
    let (engine, host) = fixture();
    let mut probes = 0;
    for name in REQUEST_VECTORS {
        let request = vector(name);
        for (key, invalid_values) in routing_mutations(&request) {
            let mut missing = request.clone();
            missing["metadata"].as_object_mut().unwrap().remove(key);
            assert!(
                serde_json::from_value::<RuntimeInvocation>(json!({
                    "content_type": missing["content_type"],
                    "metadata": missing["metadata"],
                    "payload": missing["payload"],
                }))
                .is_err(),
                "{name}: missing {key} accepted"
            );
            probes += 1;
            for invalid in invalid_values {
                let mut mutated = request.clone();
                mutated["metadata"][key] = json!(invalid);
                let result = invoke(&engine, &host, invocation(&mutated)).await;
                let probe = format!("{name}: {key}={invalid:?}");
                assert_eq!(result.content_type, ERROR_CONTENT_TYPE, "{probe}");
                assert_eq!(result.metadata.output_contract, ERROR_CONTRACT);
                // Rule R1 (pending ratification): well-formed but unannounced
                // is `unsupported`, malformed or non-canonical is `protocol`.
                let well_formed = match key {
                    "plenora.capability.name" => invalid == "plenora.rest-tools",
                    "plenora.capability.operation" => {
                        invalid == "storage.unknown" || invalid == other_operation(&request)
                    }
                    "plenora.input.contract" => {
                        !invalid.is_empty() && invalid == invalid.to_lowercase()
                    }
                    _ => invalid == "2",
                };
                let (code, category) = if well_formed {
                    ("RUNTIME_ROUTE_UNSUPPORTED", "unsupported")
                } else {
                    ("RUNTIME_ROUTE_INVALID", "protocol")
                };
                assert_eq!(result.payload["code"], code, "{probe}");
                assert_eq!(result.payload["category"], category, "{probe}");
                assert_eq!(result.payload["phase"], "validate");
                assert_eq!(result.payload["remote_effect"], "none");
                assert_eq!(result.payload["retry"]["kind"], "never");
                assert_eq!(
                    result.metadata.correlation_id.as_deref(),
                    request["metadata"]["plenora.trace.correlation_id"].as_str()
                );
                // Rule R2: a non-canonical operation version is omitted, never "0".
                if key == "plenora.operation.version" {
                    assert_eq!(
                        result.metadata.operation_version.as_deref(),
                        (invalid == "2").then_some("2"),
                        "{probe}"
                    );
                }
                assert!(host.calls.take().is_empty(), "{probe} had effects");
                probes += 1;
            }
        }
    }
    assert_eq!(probes, REQUEST_VECTORS.len() * (5 + 4 + 7 + 4 + 6 + 4));
}

// --- Proposed, not yet normative -------------------------------------------
//
// The rejection probes (`vectors/runtime-probes-v1`) and the storage cleanup
// error vectors of `plenora-contracts` pull request 21, copied byte for byte
// from its commit into `contracts/upstream/proposed` and pinned by SHA-256.
// They ratify the common runtime matrix (RT-016 to RT-023, ERR-014, ERR-015)
// but are not part of the adopted revision yet; when they reach `main` they
// move to the adopted copies.

const PROPOSED_REVISION: &str = "4890d27c120b3819bbadf6560ba9e730dfcb57aa";

const PROPOSED_FILES: [(&str, &str); 24] = [
    (
        "runtime-probe-v1.schema.json",
        "5188813894c59c67c5c9b1aab551c6c783e13b021e5a4896c033d1636e705c47",
    ),
    (
        "runtime-probes-v1/data-run-input-contract-malformed.json",
        "67723dae7b7e3306354a9db7548b8ec282d47524a9683632f81a93cd1f5e9115",
    ),
    (
        "runtime-probes-v1/data-run-input-contract-mismatch.json",
        "6df1dd05888fd98e73ee41620089e2df380c4e79fa072193823cbdd3127731a2",
    ),
    (
        "runtime-probes-v1/database-read-capability-name-other.json",
        "3532bb55733be7eed228d8fc3870d44a30dfde4b21df9f441565f14104169ad3",
    ),
    (
        "runtime-probes-v1/database-read-correlation-uppercase.json",
        "f4b6e801ed2b44a0de9cfc425f9f6579b49e2d01558bde8f5d90d26d3bcbe86c",
    ),
    (
        "runtime-probes-v1/io-read-binding-version-leading-zero.json",
        "8eb5f77415c630ea070243b60f81d8b01c04a3a0d32975f66e0cc7f46373627c",
    ),
    (
        "runtime-probes-v1/io-read-binding-version-number.json",
        "1f6f692f550a3293750cdc088bbd30172109347f2e584bb47e3b1b56019ed8c2",
    ),
    (
        "runtime-probes-v1/io-read-binding-version-unsupported.json",
        "f89c987df5e706616a9de33a45941fdb5e3c967b062b5f92a5d4c6d8232e6d42",
    ),
    (
        "runtime-probes-v1/io-read-deadline-expired.json",
        "e7fca723678ec7c0ac08888f37c64508f6231de4599201575151a1df0099fd32",
    ),
    (
        "runtime-probes-v1/io-read-deadline-negative-zero.json",
        "57e579e151c99c19a2a3de662233561388e36929be058a162d3a269d0631d8ec",
    ),
    (
        "runtime-probes-v1/io-read-deadline-offset.json",
        "e72e2842d23e5f0337f70a3d280291c4f45c769a2dd22975ea3a7ef0ef29feb1",
    ),
    (
        "runtime-probes-v1/io-read-message-id-braces.json",
        "06f53ccf50de9826207594102cbdf1a25a73f8a959e3d4248fe6c8e4b586c394",
    ),
    (
        "runtime-probes-v1/io-read-message-id-missing.json",
        "d7105072c272c3a96b3a5dee584fb2ef95a36f8d674155f98e50eeb0a9ed4b8d",
    ),
    (
        "runtime-probes-v1/io-read-operation-version-leading-zero.json",
        "647d373b5bb60f840d2fe5e9016e4ba17c7047a1392cfee395b52e618b41efd5",
    ),
    (
        "runtime-probes-v1/io-read-operation-version-missing.json",
        "a0438ca43545719ba800c816e6028c20833e41f75a0752987b6a6a2fb1f17e7f",
    ),
    (
        "runtime-probes-v1/io-read-operation-version-number.json",
        "bf090d9e77b4470543552d4a2ee816e860e8015c614a45c180bf6543b01afb19",
    ),
    (
        "runtime-probes-v1/io-read-operation-version-unknown.json",
        "35b98de94a3f4ee0125dd55d71d773b65c1f011a2a3ff011bf8113592771b2cc",
    ),
    (
        "runtime-probes-v1/rest-upload-idempotency-key-empty.json",
        "7c45f463cb43c6bd103136f05088c5bcad621ce5f0f8ffaea7b5e1d20c50695a",
    ),
    (
        "runtime-probes-v1/rest-upload-idempotency-key-null.json",
        "f82d67d9bac0d7f7c00277596f6311fd6a3b4fe731c56f897b4c0681642a0571",
    ),
    (
        "runtime-probes-v1/storage-get-idempotency-key-unsupported.json",
        "41e7dafb43c76183b167f688bdf43bf1742f3ba4d8a5332d203d7a5ebeeca035",
    ),
    (
        "runtime-probes-v1/storage-get-operation-malformed.json",
        "e40f680345cdffd03f5011a11d6e989503c3cab4ecfc06fdec24e91279e6ae68",
    ),
    (
        "runtime-probes-v1/storage-get-operation-unknown.json",
        "6ce399132fe3984304f48aa85f710c9442c692b759a5becfae85d9ee62abb1c9",
    ),
    (
        "runtime-v1/storage-put-cleanup-local-error.json",
        "daaa35140b64c37dccfde003fca66c2bfc8a3fef570736dcf86e1a27f6677b2a",
    ),
    (
        "runtime-v1/storage-put-cleanup-remote-error.json",
        "a4e66ac74e20039b27135a68d517e568ca1ac0df2fb7f6b062ba180b1e26d34b",
    ),
];

fn proposed(name: &str) -> PathBuf {
    upstream("proposed").join(name)
}

fn proposed_json(name: &str) -> Value {
    serde_json::from_slice(&fs::read(proposed(name)).expect("proposed file")).expect("JSON")
}

#[test]
fn proposed_files_are_the_pinned_copies_of_pull_request_21() {
    let source = proposed_json("source.json");
    assert_eq!(source["revision"], PROPOSED_REVISION);
    assert_eq!(source["status"], "proposed, not yet normative");
    let mut present = Vec::new();
    for directory in ["runtime-probes-v1", "runtime-v1"] {
        for entry in fs::read_dir(proposed(directory)).expect("proposed directory") {
            let name = entry.expect("entry").file_name().into_string().unwrap();
            present.push(format!("{directory}/{name}"));
        }
    }
    present.push("runtime-probe-v1.schema.json".to_owned());
    present.sort_unstable();
    let mut pinned = PROPOSED_FILES.map(|(name, _)| name.to_owned()).to_vec();
    pinned.sort_unstable();
    assert_eq!(present, pinned, "every proposed file must be pinned");
    for (name, digest) in PROPOSED_FILES {
        assert_eq!(sha256_file(&proposed(name)), digest, "{name}");
    }
}

/// Applies a probe's single metadata mutation to a request vector.
fn mutated(request: &Value, mutation: &Value) -> Value {
    let mut request = json!({
        "content_type": request["content_type"],
        "metadata": request["metadata"],
        "payload": request["payload"],
    });
    let metadata = request["metadata"].as_object_mut().unwrap();
    if let Some(key) = mutation["remove"].as_str() {
        metadata.remove(key);
    }
    if let Some(set) = mutation["set"].as_object() {
        for (key, value) in set {
            metadata.insert(key.clone(), value.clone());
        }
    }
    request
}

/// Probes whose base is not a storage request are transposed onto
/// `storage-get-request.json`: the same mutation, and the expected routing
/// values of the base replaced by the storage request's unless the mutation
/// set that key.
fn transposed_expectation(probe: &Value, storage: &Value) -> Value {
    let mut expected = probe["expected"]["metadata"].clone();
    let set = probe["mutation"]["set"].as_object();
    for key in [
        "plenora.capability.operation",
        "plenora.operation.version",
        "plenora.trace.correlation_id",
    ] {
        let mutated_key = set.is_some_and(|set| set.contains_key(key));
        if let Some(value) = expected.get_mut(key)
            && !mutated_key
        {
            *value = storage["metadata"][key].clone();
        }
    }
    expected
}

#[tokio::test]
async fn proposed_rejection_probes_produce_their_expected_results() {
    let probe_schema = jsonschema::draft202012::options()
        .build(&proposed_json("runtime-probe-v1.schema.json"))
        .expect("probe schema compiles");
    let storage_get = vector("storage-get-request.json");
    let (engine, host) = fixture();
    let mut probes = fs::read_dir(proposed("runtime-probes-v1"))
        .expect("probes")
        .map(|entry| entry.expect("probe").file_name().into_string().unwrap())
        .collect::<Vec<_>>();
    probes.sort_unstable();
    assert_eq!(probes.len(), 21);
    for name in probes {
        let probe = proposed_json(&format!("runtime-probes-v1/{name}"));
        assert!(probe_schema.is_valid(&probe), "{name}");
        let base_name = probe["base"].as_str().unwrap();
        let (base, expected_metadata) = if base_name.starts_with("storage-") {
            (vector(base_name), probe["expected"]["metadata"].clone())
        } else {
            (
                storage_get.clone(),
                transposed_expectation(&probe, &storage_get),
            )
        };
        let request = mutated(&base, &probe["mutation"]);
        let result = RuntimeBinding::new(&engine, &host, &host)
            .invoke_json(request.clone(), CancellationToken::new())
            .await;
        assert_eq!(
            result.content_type, probe["expected"]["content_type"],
            "{name}"
        );
        for axis in ["category", "phase", "remote_effect", "retry"] {
            assert_eq!(
                result.payload[axis], probe["expected"]["error"][axis],
                "{name}: {axis}"
            );
        }
        let mut metadata = serde_json::to_value(&result.metadata).unwrap();
        let metadata = metadata.as_object_mut().unwrap();
        // RT-020: always a new message identity; the causation, when present,
        // is the request's canonical message identity.
        let message_id = metadata.remove("plenora.message.id").unwrap();
        assert_ne!(
            message_id, request["metadata"]["plenora.message.id"],
            "{name}"
        );
        let request_id = request["metadata"]["plenora.message.id"].as_str();
        let canonical_request_id = request_id
            .is_some_and(|id| id.len() == 36 && id == id.to_lowercase() && !id.starts_with('{'));
        match metadata.remove("plenora.message.causation_id") {
            Some(causation) => assert_eq!(causation.as_str(), request_id, "{name}"),
            None => assert!(!canonical_request_id, "{name}: causation omitted"),
        }
        assert_eq!(Value::Object(metadata.clone()), expected_metadata, "{name}");
        assert!(host.calls.take().is_empty(), "{name} reached a resolver");
    }
}

/// ERR-015 (proposed): a proven publication followed by a failed cleanup is
/// `committed` in `cleanup`, `never` with a local residue and
/// `requires_recovery` with a remote one. The binding preserves both.
#[tokio::test]
async fn proposed_cleanup_vectors_are_preserved_by_the_binding() {
    for name in [
        "runtime-v1/storage-put-cleanup-local-error.json",
        "runtime-v1/storage-put-cleanup-remote-error.json",
    ] {
        let expected = proposed_json(name);
        let mut error: StorageError = serde_json::from_value(expected["payload"].clone())
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        error.execution_id = None;
        let (engine, host) = scripted_with(false, Some(error));
        let request = vector("storage-put-request.json");
        let result = invoke(&engine, &host, invocation(&request)).await;
        assert_eq!(result.content_type, ERROR_CONTENT_TYPE, "{name}");
        // `details` is optional in `plenora-error-v1` and its absence means
        // no details; this component always serializes the (empty) object.
        let mut payload = without_execution_id(&expected["payload"]);
        payload
            .as_object_mut()
            .unwrap()
            .entry("details")
            .or_insert_with(|| json!({}));
        assert_eq!(result.payload, payload, "{name}");
        assert_valid(&validators(), "error-v1", &result.payload);
    }
}
