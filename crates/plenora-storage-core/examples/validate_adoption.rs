//! Developer-only structural validation of an artifact adoption manifest.
use std::{error::Error, fs, path::PathBuf};

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = std::env::args_os().skip(1);
    let schema_path = PathBuf::from(args.next().ok_or("schema path is required")?);
    let document_path = PathBuf::from(args.next().ok_or("manifest path is required")?);
    // Decision 0021: schema errors and I/O exceptions can contain input data.
    let schema: serde_json::Value =
        serde_json::from_slice(&fs::read(schema_path).map_err(|_| "adoption schema unreadable")?)
            .map_err(|_| "adoption schema is not JSON")?;
    let document: serde_json::Value = serde_json::from_slice(
        &fs::read(document_path).map_err(|_| "adoption manifest unreadable")?,
    )
    .map_err(|_| "adoption manifest is not JSON")?;
    validate(&schema, &document)?;
    println!("adoption manifest v4: valid");
    Ok(())
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
    use super::validate;
    use serde_json::json;

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
