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
    ArtifactMetadata, ArtifactReference, ArtifactRole, CapabilityDocument, CapabilityStatus,
    ErrorPhase, ProviderCapabilities, RUNTIME_OPERATIONS, RemoteEffect, RetryDisposition,
    SideEffect, StorageError, Surface,
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
