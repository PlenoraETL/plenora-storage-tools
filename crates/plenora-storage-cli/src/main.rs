//! Storage CLI entry point and command dispatch.
#![forbid(unsafe_code)]

use std::{
    panic::AssertUnwindSafe,
    path::{Path, PathBuf},
    process::ExitCode,
    sync::Arc,
    time::Instant,
};

use clap::{Parser, ValueEnum, error::ErrorKind};
use futures_util::FutureExt;
use plenora_storage_core::{
    CopyRequest, DeleteRequest, Engine, EngineConfig, EnvironmentCredentialResolver, ErrorCategory,
    ErrorPhase, ExecutionControl, ListRequest, ProviderConnection, PublicationPolicy, RemoteEffect,
    RetryDisposition, StatRequest, StorageError, StorageResult, Surface,
};
use plenora_storage_engine::{PutFileOptions, get_to_file, put_from_file};
use serde_json::json;
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::{fs, io::AsyncReadExt};

mod args;
mod commands;
use commands::execute;
mod output;

use args::{Cli, Command};
use output::{CliOutcome, emit_error, emit_success, operation_result, value_result};

const CLI_PROTOCOL_VERSION: u32 = 2;

#[derive(Clone, Copy, Debug, ValueEnum)]
enum OutputFormat {
    Json,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum CliPublicationPolicy {
    BestEffort,
    AtomicRequired,
}

impl From<CliPublicationPolicy> for PublicationPolicy {
    fn from(value: CliPublicationPolicy) -> Self {
        match value {
            CliPublicationPolicy::BestEffort => Self::BestEffort,
            CliPublicationPolicy::AtomicRequired => Self::AtomicRequired,
        }
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    std::panic::set_hook(Box::new(|_| {}));
    AssertUnwindSafe(run())
        .catch_unwind()
        .await
        .unwrap_or_else(|_| {
            emit_error(
                "internal",
                "plenora-cli-error-v1",
                StorageError::new(
                    ErrorCategory::Internal,
                    ErrorPhase::Cleanup,
                    RemoteEffect::Unknown,
                    RetryDisposition::RequiresRecovery,
                    "UNHANDLED_PANIC",
                    "storage command failed unexpectedly",
                ),
            )
        })
}

async fn run() -> ExitCode {
    let mut cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) if matches!(error.kind(), ErrorKind::DisplayHelp) => {
            let _ = error.print();
            return ExitCode::SUCCESS;
        }
        Err(_) => {
            return emit_error(
                "cli-parse",
                "plenora-cli-error-v1",
                StorageError::invalid_configuration(
                    "CLI_ARGUMENT_INVALID",
                    "command-line arguments are invalid",
                ),
            );
        }
    };

    if cli.format.is_none() {
        return emit_error(
            "cli-parse",
            "plenora-cli-error-v1",
            StorageError::invalid_configuration(
                "CLI_FORMAT_REQUIRED",
                "machine invocation requires --format json",
            ),
        );
    }

    if cli.version {
        if cli.command.is_some() {
            return emit_error(
                "version",
                "plenora-cli-error-v1",
                StorageError::invalid_configuration(
                    "CLI_ARGUMENT_CONFLICT",
                    "--version cannot be combined with a command",
                ),
            );
        }
        return emit_success(
            "version",
            "plenora-storage-version-output-v1",
            json!({
                "component_version": env!("CARGO_PKG_VERSION"),
                "cli_protocol_version": CLI_PROTOCOL_VERSION,
            }),
        );
    }

    let Some(command) = cli.command.take() else {
        return emit_error(
            "cli-parse",
            "plenora-cli-error-v1",
            StorageError::invalid_configuration("CLI_COMMAND_REQUIRED", "a command is required"),
        );
    };

    let engine = match build_engine(&cli) {
        Ok(engine) => engine,
        Err(error) => return emit_error("engine-init", "plenora-cli-error-v1", error),
    };
    let control = match execution_control(cli.deadline.as_deref()) {
        Ok(control) => control,
        Err(error) => return emit_error("cli-parse", "plenora-cli-error-v1", error),
    };
    #[cfg(unix)]
    {
        let cancellation = control.cancellation.clone();
        tokio::spawn(async move {
            if let Ok(mut signal) =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            {
                signal.recv().await;
                cancellation.cancel();
            }
        });
    }
    let cancellation = control.cancellation.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            cancellation.cancel();
        }
    });

    let outcome = execute(&engine, command, &control, cli.max_list_items).await;
    engine.close();
    match outcome {
        Ok((command, contract, result)) => emit_success(command, contract, result),
        Err((command, contract, error)) => emit_error(command, contract, *error),
    }
}

fn build_engine(cli: &Cli) -> StorageResult<Engine> {
    plenora_storage_engine::build_engine_with_upload_strategy(
        EngineConfig {
            allow_experimental_contracts: cli.allow_experimental_contracts,
            allow_insecure_http: cli.allow_insecure_http,
            allow_insecure_ftp: cli.allow_insecure_ftp,
            allow_private_network: cli.allow_private_network,
            allow_unverified_ssh: cli.allow_unverified_ssh,
            max_transfer_bytes: cli.max_transfer_bytes,
            max_list_items: cli.max_list_items,
            max_buffered_put_bytes: cli.max_buffered_put_bytes,
        },
        Arc::new(EnvironmentCredentialResolver),
        if cli.spool_uploads {
            plenora_storage_engine::UploadStrategy::PrivateFile
        } else {
            plenora_storage_engine::UploadStrategy::Buffered
        },
    )
}

fn execution_control(deadline: Option<&str>) -> StorageResult<ExecutionControl> {
    let control = ExecutionControl::default();
    let Some(deadline) = deadline else {
        return Ok(control);
    };
    let deadline = OffsetDateTime::parse(deadline, &Rfc3339).map_err(|_| {
        StorageError::invalid_configuration(
            "DEADLINE_INVALID",
            "deadline must be an RFC 3339 timestamp",
        )
    })?;
    let now = OffsetDateTime::now_utc();
    let instant = if deadline <= now {
        Instant::now()
    } else {
        let duration: std::time::Duration = (deadline - now).try_into().map_err(|_| {
            StorageError::invalid_configuration("DEADLINE_INVALID", "deadline is out of range")
        })?;
        Instant::now().checked_add(duration).ok_or_else(|| {
            StorageError::invalid_configuration("DEADLINE_INVALID", "deadline is out of range")
        })?
    };
    Ok(control.with_deadline(instant))
}

#[cfg(test)]
#[path = "main_api_inventory_tests.rs"]
mod api_inventory;
