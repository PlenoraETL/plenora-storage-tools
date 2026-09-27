//! JSON envelopes and exit codes shared by every command.

use super::CLI_PROTOCOL_VERSION;
use plenora_storage_core::{
    COMPONENT_ID, ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError,
    StorageResult,
};
use serde_json::{Value, json};
use std::process::ExitCode;

pub type CliOutcome =
    Result<(&'static str, &'static str, Value), (&'static str, &'static str, Box<StorageError>)>;

pub fn operation_result<T: serde::Serialize>(
    command: &'static str,
    contract: &'static str,
    result: StorageResult<T>,
) -> CliOutcome {
    match result {
        Ok(result) => value_result(command, contract, result),
        Err(error) => Err((command, "plenora-cli-error-v1", Box::new(error))),
    }
}

pub fn value_result<T: serde::Serialize>(
    command: &'static str,
    contract: &'static str,
    result: T,
) -> CliOutcome {
    serde_json::to_value(result)
        .map(|value| (command, contract, value))
        .map_err(|_| {
            (
                command,
                "plenora-cli-error-v1",
                Box::new(StorageError::new(
                    ErrorCategory::Internal,
                    ErrorPhase::Cleanup,
                    RemoteEffect::None,
                    RetryDisposition::Never,
                    "RESULT_SERIALIZATION_FAILED",
                    "public result serialization failed",
                )),
            )
        })
}

pub fn emit_success(command: &str, contract: &str, result: Value) -> ExitCode {
    let envelope = json!({
        "status": "ok",
        "protocol_version": CLI_PROTOCOL_VERSION,
        "component": COMPONENT_ID,
        "component_version": env!("CARGO_PKG_VERSION"),
        "contract": contract,
        "command": command,
        "result": result,
    });
    println!("{envelope}");
    ExitCode::SUCCESS
}

pub fn emit_error(command: &str, contract: &str, error: StorageError) -> ExitCode {
    let exit_code = error_exit_code(error.category);
    let envelope = json!({
        "status": "error",
        "protocol_version": CLI_PROTOCOL_VERSION,
        "component": COMPONENT_ID,
        "component_version": env!("CARGO_PKG_VERSION"),
        "contract": contract,
        "command": command,
        "error": error,
    });
    println!("{envelope}");
    ExitCode::from(exit_code)
}

pub const fn error_exit_code(category: ErrorCategory) -> u8 {
    match category {
        ErrorCategory::InvalidConfiguration => 2,
        ErrorCategory::Unsupported => 3,
        ErrorCategory::ResourceLimit => 4,
        ErrorCategory::Io
        | ErrorCategory::NotFound
        | ErrorCategory::Conflict
        | ErrorCategory::Protocol
        | ErrorCategory::Authentication
        | ErrorCategory::Authorization
        | ErrorCategory::Timeout
        | ErrorCategory::Transient => 5,
        ErrorCategory::Execution => 6,
        ErrorCategory::Cancelled => 130,
        ErrorCategory::Internal => 70,
    }
}
