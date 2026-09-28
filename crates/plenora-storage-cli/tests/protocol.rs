//! Integration conformance for protocol.
use std::{
    collections::HashMap,
    process::{Command, Output},
};

use serde_json::Value;

struct Schemas(HashMap<String, Value>);

impl jsonschema::Retrieve for Schemas {
    fn retrieve(
        &self,
        uri: &jsonschema::Uri<String>,
    ) -> Result<Value, Box<dyn std::error::Error + Send + Sync>> {
        self.0
            .get(uri.as_str())
            .cloned()
            .ok_or_else(|| "unknown contract reference".into())
    }
}

fn validate_common(name: &str, value: &Value) {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contracts/upstream");
    let mut documents = HashMap::new();
    for file in [
        "cli-envelope-v2.schema.json",
        "error-v1.schema.json",
        "capabilities-v2.schema.json",
    ] {
        let document: Value =
            serde_json::from_slice(&std::fs::read(root.join(file)).expect("pinned schema"))
                .expect("JSON schema");
        documents.insert(
            document["$id"].as_str().expect("schema ID").to_owned(),
            document,
        );
    }
    let schema = documents[&format!("https://schemas.plenora.dev/{name}")].clone();
    let validator = jsonschema::draft202012::options()
        .with_retriever(Schemas(documents))
        .build(&schema)
        .expect("valid schema");
    assert!(
        validator.is_valid(value),
        "public output violates {name}: {value}"
    );
}

fn run(arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_plenora-storage"))
        .args(arguments)
        .output()
        .expect("CLI must start")
}

fn single_json_line(output: &Output) -> Value {
    assert!(output.stderr.is_empty(), "stderr must remain empty");
    let stdout = std::str::from_utf8(&output.stdout).expect("stdout must be UTF-8");
    assert!(stdout.ends_with('\n'));
    assert_eq!(stdout.lines().count(), 1, "stdout must contain one line");
    let value = serde_json::from_str(stdout).expect("stdout must contain one JSON document");
    validate_common("cli-envelope-v2.schema.json", &value);
    value
}

#[test]
fn capabilities_are_machine_readable_and_cli_only() {
    let output = run(&["--format", "json", "capabilities"]);
    assert!(output.status.success());
    let envelope = single_json_line(&output);
    validate_common("capabilities-v2.schema.json", &envelope["result"]);
    assert_eq!(envelope["protocol_version"], 2);
    assert_eq!(envelope["status"], "ok");
    assert_eq!(envelope["result"]["interfaces"][0]["kind"], "cli");
    let operations = envelope["result"]["operations"]
        .as_array()
        .expect("operations must be an array");
    assert_eq!(
        operations.len(),
        if cfg!(any(
            feature = "local",
            feature = "s3",
            feature = "sftp",
            feature = "ftp",
            feature = "ftps",
            feature = "azure",
            feature = "gcs",
            feature = "smb",
            feature = "webdav"
        )) {
            7
        } else {
            0
        }
    );
    assert!(operations.iter().all(|operation| {
        operation["status"] == "available" && operation["surfaces"] == serde_json::json!(["cli"])
    }));
}

#[test]
fn machine_version_is_one_json_line() {
    let output = run(&["--format", "json", "--version"]);
    assert!(output.status.success());
    let envelope = single_json_line(&output);
    assert_eq!(envelope["result"]["cli_protocol_version"], 2);
}

#[test]
fn mutation_policy_flags_are_explicit_values() {
    let output = run(&[
        "--format",
        "json",
        "put",
        "--connection",
        "missing.json",
        "--key",
        "object",
        "--input",
        "missing.bin",
        "--overwrite",
        "false",
    ]);
    assert_eq!(output.status.code(), Some(2));
    let envelope = single_json_line(&output);
    assert_eq!(envelope["status"], "error");
    assert_eq!(envelope["error"]["code"], "CLI_ARGUMENT_INVALID");

    let output = run(&[
        "--format",
        "json",
        "put",
        "--connection",
        "missing.json",
        "--key",
        "object",
        "--input",
        "missing.bin",
        "--overwrite",
        "false",
        "--publication-policy",
        "atomic-required",
    ]);
    assert_eq!(output.status.code(), Some(5));
    let envelope = single_json_line(&output);
    assert_eq!(envelope["error"]["code"], "CONNECTION_FILE_READ_FAILED");
    assert_eq!(envelope["error"]["category"], "not_found");
    assert_eq!(envelope["error"]["phase"], "read");
}

#[cfg(feature = "local")]
#[tokio::test]
#[allow(
    clippy::too_many_lines,
    reason = "Compare Rust and CLI results for the same admission and file-fault matrix"
)]
async fn file_errors_match_rust_and_cli_without_publication() {
    use plenora_storage_core::{
        EngineConfig, EnvironmentCredentialResolver, ExecutionControl, ProviderConnection,
        PublicationPolicy,
    };
    use plenora_storage_engine::{PutFileOptions, build_engine, get_to_file, put_from_file};
    use std::{path::PathBuf, sync::Arc};

    struct Directory(PathBuf);
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    let directory = Directory(std::env::temp_dir().join(format!(
        "storage-artifact-errors-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )));
    std::fs::create_dir(&directory.0).unwrap();
    let storage = directory.0.join("storage");
    std::fs::create_dir(&storage).unwrap();
    let connection = ProviderConnection {
        provider: "local".to_owned(),
        config_contract: "plenora-storage-local-connection-v1".to_owned(),
        config: serde_json::json!({"root": storage}),
        credential_ref: "local:process".to_owned(),
    };
    let config = directory.0.join("connection.json");
    std::fs::write(&config, serde_json::to_vec(&connection).unwrap()).unwrap();
    let engine = build_engine(
        EngineConfig::default(),
        Arc::new(EnvironmentCredentialResolver),
    )
    .unwrap();
    let missing = directory.0.join("sentinel-private-missing");
    let output = missing.join("download");
    let control = ExecutionControl::default();
    let upload_error = put_from_file(
        &engine,
        &connection,
        PutFileOptions {
            key: "new".to_owned(),
            input: missing.clone(),
            overwrite: true,
            publication_policy: PublicationPolicy::AtomicRequired,
            content_type: None,
        },
        &control,
    )
    .await
    .unwrap_err();
    let download_error = get_to_file(
        &engine,
        &connection,
        "absent".to_owned(),
        &output,
        true,
        &control,
    )
    .await
    .unwrap_err();

    for (arguments, error, code, phase) in [
        (
            vec![
                "put",
                "--input",
                missing.to_str().unwrap(),
                "--publication-policy",
                "atomic-required",
            ],
            upload_error,
            "INPUT_METADATA_FAILED",
            "read",
        ),
        (
            vec!["get", "--output", output.to_str().unwrap()],
            download_error,
            "OUTPUT_STAGING_CREATE_FAILED",
            "prepare",
        ),
    ] {
        let mut command = vec!["--format", "json"];
        command.extend(arguments);
        command.extend([
            "--connection",
            config.to_str().unwrap(),
            "--key",
            "new",
            "--overwrite",
            "true",
        ]);
        let result = run(&command);
        assert_eq!(result.status.code(), Some(5));
        let envelope = single_json_line(&result);
        assert_eq!(envelope["error"], serde_json::to_value(error).unwrap());
        assert_eq!(envelope["error"]["code"], code);
        assert_eq!(envelope["error"]["category"], "not_found");
        assert_eq!(envelope["error"]["phase"], phase);
        assert_eq!(envelope["error"]["remote_effect"], "none");
        assert_eq!(envelope["error"]["retry"]["kind"], "never");
        assert!(!envelope.to_string().contains("sentinel-private"));
    }
    assert_eq!(std::fs::read_dir(storage).unwrap().count(), 0);
    assert!(!missing.exists());
}
