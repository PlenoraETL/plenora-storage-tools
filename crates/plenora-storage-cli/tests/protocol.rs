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
    assert_eq!(operations.len(), 7);
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
}
