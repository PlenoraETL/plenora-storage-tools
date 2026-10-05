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
    ERROR_CONTRACT, Engine, EngineConfig, GetRequest, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult, PutRequest,
    RUNTIME_OPERATIONS, RuntimeBinding, RuntimeInvocation, RuntimeResultEnvelope, SecretResolver,
    StatRequest, StorageError, StorageProvider, StorageResult, TestResult, TransferResult,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};

/// Adopted contracts revision; must equal `contracts/upstream/source.json`.
const ADOPTED_REVISION: &str = "f811f21f072b34896efdb6e110bee34d756153df";

/// SHA-256 of each storage fixture in `vectors/runtime-v1` at the adopted
/// revision, computed from the upstream Git blobs.
const PINNED_VECTORS: [(&str, &str); 6] = [
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
        "storage-put-unknown-error.json",
        "36f902beb412ae5e763ea42b32b75b8a0307b4b6139ba602ca869bc606d2cbfe",
    ),
];

/// Normative text and structural schema copied from the same revision.
const PINNED_FILES: [(&str, &str); 2] = [
    (
        "runtime-vector-v1.schema.json",
        "3ec6148b8fb3db111ca0d1ff29283c3f8a46a7727c3051a777a123c102ca1f9e",
    ),
    (
        "RUNTIME-VECTORS-1.0.md",
        "3bcf7fd904098f51959f501cb50e904e6a67c42c73ec3e399e18febf579b3980",
    ),
];

const VECTOR_CREDENTIAL: &str = "secret://storage/vector";
const PUT_SOURCE: &str = "artifact://input/storage-put-vector";
const GET_SINK: &str = "artifact://output/storage-get-vector";
/// Byte length declared by every storage fixture.
const VECTOR_BYTES: usize = 17;

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
/// opens a network connection.
#[derive(Default)]
struct ScriptedS3 {
    calls: Arc<Calls>,
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
        unreachable!("no storage.test fixture")
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
        _: &StatRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        unreachable!("no storage.stat fixture")
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
        Err(fixture_error())
    }

    async fn delete(
        &self,
        _: &ProviderConnection,
        _: &DeleteRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<DeleteResult> {
        unreachable!("no storage.delete fixture")
    }

    async fn copy(
        &self,
        _: &ProviderConnection,
        _: &CopyRequest,
        _: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        unreachable!("no storage.copy fixture")
    }
}

/// The error of `storage-put-unknown-error.json` without `execution_id`:
/// `plenora-error-v1` makes it optional and `StorageError` does not carry it.
fn fixture_error() -> StorageError {
    let mut payload = vector("storage-put-unknown-error.json")["payload"].clone();
    payload
        .as_object_mut()
        .expect("error payload object")
        .remove("execution_id");
    serde_json::from_value(payload).expect("fixture error is a storage error")
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
    let calls = Arc::new(Calls::default());
    let mut engine = Engine::new(EngineConfig::default());
    engine
        .register_provider(Arc::new(ScriptedS3 {
            calls: calls.clone(),
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

/// Result identity is the request identity; operation, version, output
/// contract and content type are those of the expected fixture.
fn assert_result_identity(result: &RuntimeResultEnvelope, request: &Value, expected: &Value) {
    let metadata = serde_json::to_value(&result.metadata).unwrap();
    for key in ["plenora.message.id", "plenora.trace.correlation_id"] {
        assert_eq!(metadata[key], request["metadata"][key], "{key}");
    }
    for key in [
        "plenora.capability.operation",
        "plenora.operation.version",
        "plenora.output.contract",
    ] {
        assert_eq!(metadata[key], expected["metadata"][key], "{key}");
    }
    assert_eq!(result.content_type, expected["content_type"]);
    let mut keys = metadata.as_object().unwrap().keys().collect::<Vec<_>>();
    let mut expected_keys = expected["metadata"]
        .as_object()
        .unwrap()
        .keys()
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
/// storage fixtures cover get, list and put, all advertised by the binding.
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

#[tokio::test]
async fn put_request_vector_maps_to_the_put_unknown_error_vector() {
    let (engine, host) = fixture();
    let request = vector("storage-put-request.json");
    let expected = vector("storage-put-unknown-error.json");
    let result = invoke(&engine, &host, invocation(&request)).await;
    assert_result_identity(&result, &request, &expected);
    // Every field of the fixture is preserved except the optional
    // `execution_id`, which this component does not produce.
    let mut expected_payload = expected["payload"].clone();
    expected_payload
        .as_object_mut()
        .unwrap()
        .remove("execution_id");
    assert_eq!(result.payload, expected_payload);
    assert_valid(&validators(), "error-v1", &result.payload);
    assert_eq!(host.calls.take(), ["authorize", "open_source", "put"]);
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
    for name in [
        "storage-get-request.json",
        "storage-list-request.json",
        "storage-put-request.json",
    ] {
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
                assert_eq!(result.payload["code"], "RUNTIME_ROUTE_INVALID", "{probe}");
                assert_eq!(result.payload["category"], "protocol");
                assert_eq!(result.payload["phase"], "validate");
                assert_eq!(result.payload["remote_effect"], "none");
                assert_eq!(
                    result.metadata.correlation_id,
                    request["metadata"]["plenora.trace.correlation_id"]
                );
                assert!(host.calls.take().is_empty(), "{probe} had effects");
                probes += 1;
            }
        }
    }
    assert_eq!(probes, 3 * (5 + 4 + 7 + 4 + 6 + 4));
}
