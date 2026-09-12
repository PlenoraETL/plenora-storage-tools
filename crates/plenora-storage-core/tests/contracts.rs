use std::{
    collections::HashMap,
    error::Error,
    fs,
    path::{Path, PathBuf},
};

use jsonschema::{Retrieve, Uri};
use serde::Deserialize;
use serde_json::Value;

use plenora_storage_core::{
    ArtifactMetadata, ArtifactReference, ArtifactRole, ArtifactSinkReference, CapabilityDocument,
    CapabilityStatus, CopyInput, CopyRequest, DeleteInput, DeleteRequest, DeleteResult, ErrorPhase,
    GetInput, GetRequest, IntegrityMetadata, ListInput, ListRequest, ListResult,
    OPERATION_SCHEMA_VERSION, ObjectMetadata, ProviderCapabilities, ProviderConnection,
    PublicationPolicy, PutInput, PutRequest, RUNTIME_OPERATIONS, RemoteEffect, RetryDisposition,
    SideEffect, StatInput, StatRequest, StorageError, Surface, TestInput, TestResult,
    TransferResult, validate_object_key, validate_object_prefix,
};

#[derive(Clone)]
struct ContractRetriever {
    documents: HashMap<String, Value>,
}

impl Retrieve for ContractRetriever {
    fn retrieve(&self, uri: &Uri<String>) -> Result<Value, Box<dyn Error + Send + Sync>> {
        self.documents
            .get(uri.as_str())
            .cloned()
            .ok_or_else(|| format!("schema was not found: {uri}").into())
    }
}

#[derive(Deserialize)]
struct ExampleCollection {
    schema_version: u32,
    cases: Vec<ExampleCase>,
}

#[derive(Deserialize)]
struct ExampleCase {
    #[serde(default)]
    name: Option<String>,
    schema: String,
    instance: Value,
}

fn contracts_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts")
}

fn load_schemas() -> (HashMap<String, Value>, HashMap<String, Value>) {
    let mut by_filename = HashMap::new();
    let mut by_uri = HashMap::new();
    let schema_dir = contracts_root().join("schemas");

    for entry in fs::read_dir(schema_dir).expect("schema directory must be readable") {
        let path = entry.expect("schema entry must be readable").path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let document: Value = serde_json::from_slice(
            &fs::read(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .unwrap_or_else(|error| panic!("{} is not JSON: {error}", path.display()));
        let identifier = document["$id"]
            .as_str()
            .unwrap_or_else(|| panic!("{} has no $id", path.display()))
            .to_owned();
        let filename = path
            .file_name()
            .and_then(|value| value.to_str())
            .expect("schema filename must be UTF-8")
            .to_owned();
        assert!(by_uri.insert(identifier, document.clone()).is_none());
        assert!(by_filename.insert(filename, document).is_none());
    }

    (by_filename, by_uri)
}

fn load_examples(path: &Path) -> ExampleCollection {
    serde_json::from_slice(
        &fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
    )
    .unwrap_or_else(|error| panic!("{} is invalid: {error}", path.display()))
}

fn validator_for(schema: &Value, documents: HashMap<String, Value>) -> jsonschema::Validator {
    jsonschema::draft202012::options()
        .with_retriever(ContractRetriever { documents })
        .should_validate_formats(true)
        .build(schema)
        .expect("schema must compile")
}

#[test]
fn every_component_owned_schema_is_valid_draft_2020_12() {
    let (schemas, _) = load_schemas();
    assert!(!schemas.is_empty());
    for (filename, schema) in schemas {
        if let Err(error) = jsonschema::meta::validate(&schema) {
            panic!("{filename} is not a valid JSON Schema: {error}");
        }
    }
}

#[test]
fn valid_examples_match_their_component_owned_schemas() {
    let (schemas, documents) = load_schemas();
    let examples = load_examples(
        &contracts_root()
            .join("examples")
            .join("valid")
            .join("storage-operations-v1.json"),
    );
    assert_eq!(examples.schema_version, 1);

    for case in examples.cases {
        let schema = schemas
            .get(&case.schema)
            .unwrap_or_else(|| panic!("example references unknown schema {}", case.schema));
        let validator = validator_for(schema, documents.clone());
        let errors = validator
            .iter_errors(&case.instance)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        assert!(
            errors.is_empty(),
            "valid example for {} failed: {errors:?}",
            case.schema
        );
    }
}

#[test]
fn invalid_examples_are_rejected_by_their_component_owned_schemas() {
    let (schemas, documents) = load_schemas();
    let examples = load_examples(
        &contracts_root()
            .join("examples")
            .join("invalid")
            .join("storage-operations-v1.json"),
    );
    assert_eq!(examples.schema_version, 1);

    for case in examples.cases {
        let schema = schemas
            .get(&case.schema)
            .unwrap_or_else(|| panic!("example references unknown schema {}", case.schema));
        let validator = validator_for(schema, documents.clone());
        assert!(
            !validator.is_valid(&case.instance),
            "invalid example '{}' unexpectedly matched {}",
            case.name.as_deref().unwrap_or("unnamed"),
            case.schema
        );
    }
}

fn common_definition_validator(
    name: &str,
    documents: HashMap<String, Value>,
) -> jsonschema::Validator {
    let schema = serde_json::json!({
        "$ref": format!(
            "https://schemas.plenora.dev/storage/plenora-storage-common-v1.schema.json#/$defs/{name}"
        )
    });
    validator_for(&schema, documents)
}

fn sample_connection() -> ProviderConnection {
    ProviderConnection {
        provider: "s3".to_owned(),
        config_contract: "plenora-storage-s3-connection-v1".to_owned(),
        config: serde_json::json!({"endpoint": "https://s3.example.invalid", "bucket": "example"}),
        credential_ref: "secret://storage/test".to_owned(),
    }
}

fn sample_artifact_metadata() -> ArtifactMetadata {
    ArtifactMetadata {
        content_type: Some("application/octet-stream".to_owned()),
        size: Some(17),
        sha256: Some("0123456789abcdef".repeat(4)),
    }
}

fn sample_transfer() -> TransferResult {
    TransferResult {
        key: "outgoing/a.bin".to_owned(),
        bytes_transferred: 17,
        checksum: IntegrityMetadata {
            algorithm: "sha256".to_owned(),
            value: "0123456789abcdef".repeat(4),
        },
        artifact: sample_artifact_metadata(),
        etag: None,
        version: None,
    }
}

fn sample_object() -> ObjectMetadata {
    ObjectMetadata {
        key: "incoming/a.bin".to_owned(),
        size: 17,
        last_modified: None,
        etag: None,
        version: None,
    }
}

/// The hand-written example vectors only prove the schemas are self-consistent.
/// This drives every public contract from the Rust types the surfaces actually
/// serialize, so a DTO can never drift away from the schema that describes it.
#[test]
fn serialized_rust_dtos_match_their_component_owned_schemas() {
    let (schemas, documents) = load_schemas();
    let connection = sample_connection();
    let cases: [(&str, Value); 14] = [
        (
            "plenora-storage-test-input-v1.schema.json",
            to_contract_value(&TestInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
            }),
        ),
        (
            "plenora-storage-list-input-v1.schema.json",
            to_contract_value(&ListInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
                request: ListRequest {
                    prefix: Some("incoming/".to_owned()),
                    cursor: None,
                    max_items: Some(100),
                },
            }),
        ),
        (
            "plenora-storage-stat-input-v1.schema.json",
            to_contract_value(&StatInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
                request: StatRequest {
                    key: "incoming/a.bin".to_owned(),
                },
            }),
        ),
        (
            "plenora-storage-get-input-v1.schema.json",
            to_contract_value(&GetInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
                request: GetRequest {
                    key: "incoming/a.bin".to_owned(),
                },
                artifact_sink: ArtifactSinkReference {
                    reference: "artifact://output/a".to_owned(),
                    overwrite: false,
                    metadata: sample_artifact_metadata(),
                },
            }),
        ),
        (
            "plenora-storage-put-input-v1.schema.json",
            to_contract_value(&PutInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
                request: PutRequest {
                    key: "outgoing/a.bin".to_owned(),
                    overwrite: false,
                    publication_policy: PublicationPolicy::AtomicRequired,
                    content_type: Some("application/octet-stream".to_owned()),
                    content_length: Some(17),
                    metadata: std::collections::BTreeMap::from([(
                        "source".to_owned(),
                        "vector".to_owned(),
                    )]),
                },
                artifact_source: ArtifactReference {
                    reference: "artifact://input/a".to_owned(),
                    metadata: sample_artifact_metadata(),
                },
            }),
        ),
        (
            "plenora-storage-copy-input-v1.schema.json",
            to_contract_value(&CopyInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection: connection.clone(),
                request: CopyRequest {
                    source_key: "incoming/a.bin".to_owned(),
                    destination_key: "outgoing/a.bin".to_owned(),
                    overwrite: true,
                    publication_policy: PublicationPolicy::BestEffort,
                },
            }),
        ),
        (
            "plenora-storage-delete-input-v1.schema.json",
            to_contract_value(&DeleteInput {
                schema_version: OPERATION_SCHEMA_VERSION,
                connection,
                request: DeleteRequest {
                    key: "outgoing/a.bin".to_owned(),
                    ignore_missing: false,
                },
            }),
        ),
        (
            "plenora-storage-test-output-v1.schema.json",
            to_contract_value(&TestResult {
                provider: "s3".to_owned(),
                reachable: true,
            }),
        ),
        (
            "plenora-storage-list-output-v1.schema.json",
            to_contract_value(&ListResult {
                objects: vec![sample_object()],
                truncated: true,
                next_cursor: Some(format!("cursor://{}", "0123456789abcdef".repeat(4))),
            }),
        ),
        (
            "plenora-storage-stat-output-v1.schema.json",
            to_contract_value(&sample_object()),
        ),
        (
            "plenora-storage-copy-output-v1.schema.json",
            to_contract_value(&sample_object()),
        ),
        (
            "plenora-storage-get-output-v1.schema.json",
            to_contract_value(&sample_transfer()),
        ),
        (
            "plenora-storage-put-output-v1.schema.json",
            to_contract_value(&sample_transfer()),
        ),
        (
            "plenora-storage-delete-output-v1.schema.json",
            to_contract_value(&DeleteResult {
                key: "outgoing/a.bin".to_owned(),
                deleted: true,
            }),
        ),
    ];

    for (schema_name, instance) in cases {
        let schema = schemas
            .get(schema_name)
            .unwrap_or_else(|| panic!("unknown schema {schema_name}"));
        let validator = validator_for(schema, documents.clone());
        let errors = validator
            .iter_errors(&instance)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        assert!(
            errors.is_empty(),
            "{schema_name} rejected its serialized Rust DTO: {errors:?}"
        );
    }
}

fn to_contract_value<T: serde::Serialize>(value: &T) -> Value {
    serde_json::to_value(value).expect("public DTO must serialize")
}

/// A document the contract accepts must be accepted by the Rust surface, and one
/// it rejects must be rejected there too. Without this, a provider can reject
/// keys the public schema declares valid.
#[test]
fn rust_validators_and_contract_definitions_agree_on_boundaries() {
    let (_, documents) = load_schemas();
    let longest = "x".repeat(4_096);
    let too_long = "x".repeat(4_097);
    // JSON Schema `maxLength` counts code points, so a multibyte key at the
    // bound is contract-valid even though it is far longer in bytes.
    let longest_multibyte = "è".repeat(4_096);
    let too_long_multibyte = "è".repeat(4_097);

    let key = common_definition_validator("key", documents.clone());
    for candidate in [
        "a",
        "folder/object.bin",
        "folder/.hidden",
        longest.as_str(),
        longest_multibyte.as_str(),
        too_long_multibyte.as_str(),
        "",
        "/absolute",
        "trailing/",
        "double//segment",
        "dot/./segment",
        "up/../segment",
        "..",
        ".",
        "back\\slash",
        too_long.as_str(),
    ] {
        assert_eq!(
            validate_object_key(candidate).is_ok(),
            key.is_valid(&Value::String(candidate.to_owned())),
            "object key '{candidate}' disagrees with plenora-storage-common-v1"
        );
    }

    let prefix = common_definition_validator("prefix", documents.clone());
    for candidate in [
        "",
        "incoming",
        "incoming/",
        "a/b/",
        longest.as_str(),
        longest_multibyte.as_str(),
        too_long_multibyte.as_str(),
        "/absolute",
        "double//segment",
        "dot/./segment",
        "../secret",
        "back\\slash",
        too_long.as_str(),
    ] {
        assert_eq!(
            validate_object_prefix(candidate).is_ok(),
            prefix.is_valid(&Value::String(candidate.to_owned())),
            "object prefix '{candidate}' disagrees with plenora-storage-common-v1"
        );
    }

    let reference = common_definition_validator("artifactReference", documents.clone());
    for candidate in [
        "artifact://a",
        "artifact://input/object-123",
        "artifact://",
        "artifact:///x",
        "artifact://_leading",
        "artifact://input/../private",
        "artifact://input/has space",
        r"C:\private\a.bin",
        "/private/a.bin",
    ] {
        let dto = ArtifactReference {
            reference: candidate.to_owned(),
            metadata: ArtifactMetadata::default(),
        };
        assert_eq!(
            dto.validate().is_ok(),
            reference.is_valid(&to_contract_value(&dto)),
            "artifact reference '{candidate}' disagrees with plenora-storage-common-v1"
        );
    }

    let metadata = common_definition_validator("artifactMetadata", documents);
    for candidate in [
        "application/octet-stream",
        "a/b",
        "abc",
        "",
        "application/",
        "/plain",
        "application/octet stream",
    ] {
        let dto = ArtifactMetadata {
            content_type: Some(candidate.to_owned()),
            size: None,
            sha256: None,
        };
        assert_eq!(
            dto.validate().is_ok(),
            metadata.is_valid(&to_contract_value(&dto)),
            "content type '{candidate}' disagrees with plenora-storage-common-v1"
        );
    }
}

/// The Rust surface must apply the inline-secret ban and the reference shape of
/// `plenora-storage-connection-v1` itself, not only when a JSON Schema validator
/// happens to run.
#[test]
fn connection_validation_agrees_with_the_public_connection_schema() {
    let (schemas, documents) = load_schemas();
    let schema = schemas
        .get("plenora-storage-connection-v1.schema.json")
        .expect("connection schema must exist");
    let validator = validator_for(schema, documents);
    let base = sample_connection();

    let mut candidates = vec![base.clone()];
    for config in [
        serde_json::json!({"db_password": "inline"}),
        serde_json::json!({"service_token": "inline"}),
        serde_json::json!({"secret_key": "inline"}),
        serde_json::json!({"customer_access_key_id": "inline"}),
        serde_json::json!({"Authorization": "inline"}),
        serde_json::json!({"endpoint": "https://s3.example.invalid"}),
        serde_json::json!({"port": 22, "atomic_rename": true, "host_key_sha256": null}),
        // Nesting would let a secret hide below the level at which field names
        // are inspected, so the contract keeps configuration flat.
        serde_json::json!({"auth": {"password": "inline"}}),
        serde_json::json!({"hosts": ["a", "b"]}),
    ] {
        candidates.push(ProviderConnection {
            config,
            ..base.clone()
        });
    }
    for credential_ref in [
        "do-not-persist",
        "env:PLENORA_MINIO_CREDENTIALS",
        "secret://storage/test",
        "S3:upper",
        "a:b",
    ] {
        candidates.push(ProviderConnection {
            credential_ref: credential_ref.to_owned(),
            ..base.clone()
        });
    }
    for provider in ["s3", "S3", "3s", ""] {
        candidates.push(ProviderConnection {
            provider: provider.to_owned(),
            ..base.clone()
        });
    }
    for config_contract in [
        "plenora-storage-s3-connection-v1",
        "x",
        "plenora-storage-s3-connection-v0",
    ] {
        candidates.push(ProviderConnection {
            config_contract: config_contract.to_owned(),
            ..base.clone()
        });
    }

    for candidate in candidates {
        assert_eq!(
            candidate.validate().is_ok(),
            validator.is_valid(&to_contract_value(&candidate)),
            "connection {candidate:?} disagrees with plenora-storage-connection-v1"
        );
    }
}

#[test]
fn checked_in_docker_connections_match_their_provider_schemas() {
    let (schemas, documents) = load_schemas();
    let workspace = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");

    for (filename, provider_schema) in [
        (
            "minio-connection.json",
            "plenora-storage-s3-connection-v1.schema.json",
        ),
        (
            "sftp-connection.json",
            "plenora-storage-sftp-connection-v1.schema.json",
        ),
        (
            "ftp-connection.json",
            "plenora-storage-ftp-connection-v1.schema.json",
        ),
    ] {
        let path = workspace.join("docker").join(filename);
        let connection: ProviderConnection = serde_json::from_slice(
            &fs::read(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .unwrap_or_else(|error| panic!("{} is not a connection: {error}", path.display()));
        connection
            .validate()
            .unwrap_or_else(|error| panic!("{} failed Rust validation: {error}", path.display()));
        let schema = schemas
            .get(provider_schema)
            .unwrap_or_else(|| panic!("missing schema {provider_schema}"));
        let validator = validator_for(schema, documents.clone());
        let errors = validator
            .iter_errors(&connection.config)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        assert!(
            errors.is_empty(),
            "{} does not match {provider_schema}: {errors:?}",
            path.display()
        );
    }
}

#[test]
fn checked_in_docker_connections_match_the_public_connection_schema() {
    let (schemas, documents) = load_schemas();
    let schema = schemas
        .get("plenora-storage-connection-v1.schema.json")
        .expect("connection schema must exist");
    let validator = validator_for(schema, documents);
    let workspace = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");

    for filename in [
        "minio-connection.json",
        "sftp-connection.json",
        "ftp-connection.json",
    ] {
        let path = workspace.join("docker").join(filename);
        let connection: Value = serde_json::from_slice(
            &fs::read(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .unwrap_or_else(|error| panic!("{} is invalid JSON: {error}", path.display()));
        let errors = validator
            .iter_errors(&connection)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        assert!(errors.is_empty(), "{} failed: {errors:?}", path.display());
    }
}

#[test]
fn capability_attributes_match_the_component_owned_schema() {
    let (schemas, documents) = load_schemas();
    let schema = schemas
        .get("plenora-storage-capability-attributes-v1.schema.json")
        .expect("capability attribute schema must exist");
    let validator = validator_for(schema, documents);
    let capability = CapabilityDocument::new(
        Surface::Runtime,
        vec![ProviderCapabilities {
            provider: "test".to_owned(),
            config_contract: "plenora-storage-test-connection-v1".to_owned(),
            operations: ["test", "list", "stat", "get", "put", "copy", "delete"]
                .map(str::to_owned)
                .to_vec(),
            attributes: std::collections::BTreeMap::new(),
        }],
    );

    assert_eq!(capability.interfaces.len(), 1);
    assert_eq!(capability.interfaces[0].kind, Surface::Runtime);
    for operation in capability.operations {
        assert_eq!(operation.status, CapabilityStatus::Experimental);
        assert_eq!(operation.surfaces, [Surface::Runtime]);
        assert!(validator.is_valid(&Value::Object(operation.attributes.into_iter().collect())));
    }
}

#[test]
fn rust_binding_and_runtime_descriptors_cover_the_same_operations() {
    let binding: Value = serde_json::from_slice(
        &fs::read(contracts_root().join("bindings").join("rust-v1.json"))
            .expect("Rust binding must be readable"),
    )
    .expect("Rust binding must be JSON");
    let exports = binding["operations"]
        .as_object()
        .expect("Rust binding operations must be an object");

    assert_eq!(exports.len(), RUNTIME_OPERATIONS.len());
    for descriptor in RUNTIME_OPERATIONS {
        let selector = format!("{}@{}", descriptor.operation, descriptor.version);
        let export = exports
            .get(&selector)
            .and_then(Value::as_str)
            .unwrap_or_else(|| panic!("missing Rust export for {selector}"));
        assert!(export.starts_with("plenora_storage_core::Engine::"));
        assert_eq!(descriptor.content_type, "application/json");
        assert!(descriptor.cancellation && descriptor.deadline);
        assert!(!descriptor.idempotency_key);
    }

    let get = RUNTIME_OPERATIONS
        .iter()
        .find(|item| item.operation == "storage.get")
        .expect("get descriptor must exist");
    assert_eq!(get.side_effect, SideEffect::Remote);
    assert_eq!(get.artifact_role, ArtifactRole::Sink);
}

#[test]
fn public_security_and_ambiguous_error_invariants_fail_closed() {
    for reference in [
        r"C:\private\input.bin",
        "/private/input.bin",
        "artifact://input/../private",
        "artifact://input/has space",
    ] {
        assert!(
            ArtifactReference {
                reference: reference.to_owned(),
                metadata: ArtifactMetadata::default(),
            }
            .validate()
            .is_err()
        );
    }
    assert!(
        ArtifactReference {
            reference: "artifact://input/object-123".to_owned(),
            metadata: ArtifactMetadata::default(),
        }
        .validate()
        .is_ok()
    );

    let timeout = StorageError::timeout(ErrorPhase::Commit, true);
    assert_eq!(timeout.remote_effect, RemoteEffect::Unknown);
    assert_eq!(timeout.retry, RetryDisposition::RequiresRecovery);
    let cancelled = StorageError::cancelled(ErrorPhase::Write, true);
    assert_eq!(cancelled.remote_effect, RemoteEffect::Unknown);
    assert_eq!(cancelled.retry, RetryDisposition::RequiresRecovery);
}
