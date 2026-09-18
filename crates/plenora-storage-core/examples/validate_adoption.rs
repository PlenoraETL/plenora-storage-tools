//! Developer-only structural validation of an artifact adoption manifest.
use std::{error::Error, fs, path::PathBuf};

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = std::env::args_os().skip(1);
    let schema_path = PathBuf::from(args.next().ok_or("schema path is required")?);
    let document_path = PathBuf::from(args.next().ok_or("manifest path is required")?);
    let schema: serde_json::Value = serde_json::from_slice(&fs::read(schema_path)?)?;
    let document: serde_json::Value = serde_json::from_slice(&fs::read(document_path)?)?;
    let validator = jsonschema::draft202012::new(&schema)?;
    for error in validator.iter_errors(&document) {
        eprintln!("{error}");
    }
    if !validator.is_valid(&document) {
        return Err("invalid adoption manifest".into());
    }
    println!("adoption manifest v4: valid");
    Ok(())
}
