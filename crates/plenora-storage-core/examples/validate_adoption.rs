//! Developer-only structural validation of an artifact adoption manifest.
use std::{error::Error, fs, path::PathBuf};

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = std::env::args_os().skip(1);
    let schema_path = PathBuf::from(args.next().ok_or("schema path is required")?);
    let document_path = PathBuf::from(args.next().ok_or("manifest path is required")?);
    // Decision 0021: schema errors and I/O exceptions can contain input data.
    let schema_bytes = fs::read(&schema_path).map_err(|_| "adoption schema unreadable")?;
    let document_bytes = fs::read(&document_path).map_err(|_| "adoption manifest unreadable")?;
    let schema = parse(&schema_bytes)?;
    let document = parse(&document_bytes)?;
    validate(&schema, &document)?;
    println!("adoption manifest v4: valid");
    Ok(())
}

fn parse(bytes: &[u8]) -> Result<serde_json::Value, Box<dyn Error>> {
    plenora_storage_core::validate_json(bytes)?;
    serde_json::from_slice(bytes).map_err(|_| "adoption document is not JSON".into())
}

fn validate(schema: &serde_json::Value, document: &serde_json::Value) -> Result<(), &'static str> {
    let validator = jsonschema::draft202012::new(schema).map_err(|_| "adoption schema invalid")?;
    if !validator.is_valid(document) {
        return Err("invalid adoption manifest");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{parse, validate};
    use serde_json::json;

    #[test]
    fn schema_and_manifest_parsing_rejects_duplicate_members() {
        for document in [
            r#"{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}"#,
            r#"{"items":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}"#,
        ] {
            let error = parse(document.as_bytes()).unwrap_err().to_string();
            assert!(error.contains("duplicate members"));
            assert!(!error.contains("PRIVATE"));
        }
        assert!(parse(br#"{"items":[{"key":1},{"key":2}]}"#).is_ok());
    }

    #[test]
    fn schema_and_instance_failures_never_return_input_data() {
        assert_eq!(
            validate(&json!({"type": "PRIVATE_SENTINEL"}), &json!({})),
            Err("adoption schema invalid")
        );
        assert_eq!(
            validate(&json!({"type": "integer"}), &json!("PRIVATE_SENTINEL")),
            Err("invalid adoption manifest")
        );
        assert_eq!(validate(&json!({"type": "integer"}), &json!(1)), Ok(()));
    }
}
