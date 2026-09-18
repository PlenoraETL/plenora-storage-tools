#![forbid(unsafe_code)]

use std::{
    panic::AssertUnwindSafe,
    path::{Path, PathBuf},
    process::ExitCode,
    sync::Arc,
    time::Instant,
};

use clap::{Args, Parser, Subcommand, ValueEnum, error::ErrorKind};
use futures_util::FutureExt;
use plenora_storage_core::{
    COMPONENT_ID, CopyRequest, DeleteRequest, Engine, EngineConfig, EnvironmentCredentialResolver,
    ErrorCategory, ErrorPhase, ExecutionControl, ListRequest, ProviderConnection,
    PublicationPolicy, RemoteEffect, RetryDisposition, StatRequest, StorageError, StorageResult,
    Surface,
};
use plenora_storage_engine::{PutFileOptions, get_to_file, put_from_file};
use serde_json::{Value, json};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::{fs, io::AsyncReadExt};

const CLI_PROTOCOL_VERSION: u32 = 2;

#[derive(Parser)]
#[command(name = "plenora-storage", disable_version_flag = true)]
// These are separate, global opt-ins so operators must authorize each relaxed
// security boundary explicitly on the command line.
#[allow(clippy::struct_excessive_bools)]
struct Cli {
    #[arg(long, global = true, value_enum)]
    format: Option<OutputFormat>,
    #[arg(long, global = true)]
    version: bool,
    #[arg(long, global = true)]
    deadline: Option<String>,
    #[arg(
        long,
        global = true,
        help = "Compatibility option; qualified v1 operations no longer require experimental opt-in"
    )]
    allow_experimental_contracts: bool,
    #[arg(long, global = true)]
    allow_insecure_http: bool,
    #[arg(long, global = true)]
    allow_insecure_ftp: bool,
    #[arg(long, global = true)]
    allow_private_network: bool,
    #[arg(long, global = true)]
    allow_unverified_ssh: bool,
    #[arg(long, global = true, default_value_t = 1_073_741_824)]
    max_transfer_bytes: u64,
    #[arg(long, global = true, default_value_t = 10_000)]
    max_list_items: usize,
    #[arg(long, global = true, default_value_t = plenora_storage_core::DEFAULT_MAX_BUFFERED_PUT_BYTES)]
    max_buffered_put_bytes: u64,
    #[command(subcommand)]
    command: Option<Command>,
}

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

#[derive(Subcommand)]
enum Command {
    Capabilities,
    Test(ConnectionArgs),
    List {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        prefix: Option<String>,
        #[arg(long)]
        cursor: Option<String>,
        #[arg(long)]
        max_items: Option<usize>,
        /// Follow pages in this process, bounded by --max-list-items.
        #[arg(long)]
        all: bool,
    },
    Stat {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        key: String,
    },
    Get {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        key: String,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, action = clap::ArgAction::Set, required = true)]
        overwrite: bool,
    },
    Put {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        key: String,
        #[arg(long)]
        input: PathBuf,
        #[arg(long, action = clap::ArgAction::Set, required = true)]
        overwrite: bool,
        #[arg(long, value_enum)]
        publication_policy: CliPublicationPolicy,
        #[arg(long)]
        content_type: Option<String>,
    },
    Copy {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        source_key: String,
        #[arg(long)]
        destination_key: String,
        #[arg(long, action = clap::ArgAction::Set, required = true)]
        overwrite: bool,
        #[arg(long, value_enum)]
        publication_policy: CliPublicationPolicy,
    },
    Delete {
        #[command(flatten)]
        connection: ConnectionArgs,
        #[arg(long)]
        key: String,
        #[arg(long, action = clap::ArgAction::Set, required = true)]
        ignore_missing: bool,
    },
}

#[derive(Args)]
struct ConnectionArgs {
    #[arg(long)]
    connection: PathBuf,
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
    let cli = match Cli::try_parse() {
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

    let Some(command) = cli.command else {
        return emit_error(
            "cli-parse",
            "plenora-cli-error-v1",
            StorageError::invalid_configuration("CLI_COMMAND_REQUIRED", "a command is required"),
        );
    };

    let engine = match plenora_storage_engine::build_engine(
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
    ) {
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

type CliOutcome =
    Result<(&'static str, &'static str, Value), (&'static str, &'static str, Box<StorageError>)>;

async fn execute(
    engine: &Engine,
    command: Command,
    control: &ExecutionControl,
    list_budget: usize,
) -> CliOutcome {
    match command {
        Command::Capabilities => value_result(
            "capabilities",
            "plenora-capabilities-v2",
            engine.capabilities_for(Surface::Cli),
        ),
        Command::Test(args) => {
            let connection = connection_or_error("test", args.connection, control).await?;
            operation_result(
                "test",
                "plenora-storage-test-output-v1",
                engine.test(&connection, control).await,
            )
        }
        Command::List {
            connection,
            prefix,
            cursor,
            max_items,
            all,
        } => {
            let connection = connection_or_error("list", connection.connection, control).await?;
            operation_result(
                "list",
                "plenora-storage-list-output-v1",
                list_for_cli(
                    engine,
                    &connection,
                    ListRequest {
                        prefix,
                        cursor,
                        max_items,
                    },
                    control,
                    all,
                    list_budget,
                )
                .await,
            )
        }
        Command::Stat { connection, key } => {
            let connection = connection_or_error("stat", connection.connection, control).await?;
            operation_result(
                "stat",
                "plenora-storage-stat-output-v1",
                engine
                    .stat(&connection, &StatRequest { key }, control)
                    .await,
            )
        }
        Command::Get {
            connection,
            key,
            output,
            overwrite,
        } => {
            let connection = connection_or_error("get", connection.connection, control).await?;
            let result = get_to_file(engine, &connection, key, &output, overwrite, control).await;
            operation_result("get", "plenora-storage-get-output-v1", result)
        }
        Command::Put {
            connection,
            key,
            input,
            overwrite,
            publication_policy,
            content_type,
        } => {
            let connection = connection_or_error("put", connection.connection, control).await?;
            let result = put_from_file(
                engine,
                &connection,
                PutFileOptions {
                    key,
                    input,
                    overwrite,
                    publication_policy: publication_policy.into(),
                    content_type,
                },
                control,
            )
            .await;
            operation_result("put", "plenora-storage-put-output-v1", result)
        }
        Command::Copy {
            connection,
            source_key,
            destination_key,
            overwrite,
            publication_policy,
        } => {
            let connection = connection_or_error("copy", connection.connection, control).await?;
            operation_result(
                "copy",
                "plenora-storage-copy-output-v1",
                engine
                    .copy(
                        &connection,
                        &CopyRequest {
                            source_key,
                            destination_key,
                            overwrite,
                            publication_policy: publication_policy.into(),
                        },
                        control,
                    )
                    .await,
            )
        }
        Command::Delete {
            connection,
            key,
            ignore_missing,
        } => {
            let connection = connection_or_error("delete", connection.connection, control).await?;
            operation_result(
                "delete",
                "plenora-storage-delete-output-v1",
                engine
                    .delete(
                        &connection,
                        &DeleteRequest {
                            key,
                            ignore_missing,
                        },
                        control,
                    )
                    .await,
            )
        }
    }
}

async fn list_for_cli(
    engine: &Engine,
    connection: &ProviderConnection,
    mut request: ListRequest,
    control: &ExecutionControl,
    all: bool,
    budget: usize,
) -> StorageResult<plenora_storage_core::ListResult> {
    if request.cursor.is_some() {
        return Err(StorageError::invalid_configuration(
            "CLI_CURSOR_SESSION_REQUIRED",
            "list cursors belong to one Engine; use list --all to follow pages in one invocation",
        ));
    }
    let mut objects = Vec::new();
    loop {
        let page = engine.list(connection, &request, control).await?;
        if page.objects.len() > budget.saturating_sub(objects.len()) {
            return Err(StorageError::new(
                ErrorCategory::ResourceLimit,
                ErrorPhase::Read,
                RemoteEffect::None,
                RetryDisposition::Never,
                "CLI_LIST_LIMIT_EXCEEDED",
                "complete listing exceeds --max-list-items; narrow the prefix or increase the limit",
            ));
        }
        if page.truncated && !all {
            return Err(StorageError::invalid_configuration(
                "CLI_LIST_PAGINATION_REQUIRED",
                "listing has more pages; use list --all to follow them in one invocation",
            ));
        }
        if page.truncated && (page.objects.is_empty() || page.next_cursor.is_none()) {
            return Err(StorageError::new(
                ErrorCategory::Protocol,
                ErrorPhase::Read,
                RemoteEffect::None,
                RetryDisposition::Never,
                "LIST_PAGE_INVALID",
                "truncated listing did not advance",
            ));
        }
        objects.extend(page.objects);
        if !page.truncated {
            return Ok(plenora_storage_core::ListResult {
                objects,
                truncated: false,
                next_cursor: None,
            });
        }
        request.cursor = page.next_cursor;
    }
}

async fn connection_or_error(
    command: &'static str,
    path: PathBuf,
    control: &ExecutionControl,
) -> Result<ProviderConnection, (&'static str, &'static str, Box<StorageError>)> {
    control
        .run(load_connection(&path), ErrorPhase::Read, false)
        .await
        .map_err(|error| (command, "plenora-cli-error-v1", Box::new(error)))
}

async fn load_connection(path: &Path) -> StorageResult<ProviderConnection> {
    let read_error = || {
        StorageError::new(
            ErrorCategory::Io,
            ErrorPhase::Read,
            RemoteEffect::None,
            RetryDisposition::Never,
            "CONNECTION_FILE_READ_FAILED",
            "connection file could not be read",
        )
    };
    if !fs::metadata(path)
        .await
        .map_err(|_| read_error())?
        .is_file()
    {
        return Err(StorageError::invalid_configuration(
            "CONNECTION_NOT_REGULAR_FILE",
            "connection input must be a regular file",
        ));
    }
    let file = fs::File::open(path).await.map_err(|_| read_error())?;
    let mut data = Vec::new();
    file.take(1_048_577)
        .read_to_end(&mut data)
        .await
        .map_err(|_| read_error())?;
    if data.len() > 1_048_576 {
        return Err(StorageError::new(
            ErrorCategory::ResourceLimit,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "CONNECTION_FILE_TOO_LARGE",
            "connection file exceeds the 1 MiB limit",
        ));
    }
    serde_json::from_slice(&data).map_err(|_| {
        StorageError::invalid_configuration(
            "CONNECTION_DOCUMENT_INVALID",
            "connection file is not a valid storage connection document",
        )
    })
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

fn operation_result<T: serde::Serialize>(
    command: &'static str,
    contract: &'static str,
    result: StorageResult<T>,
) -> CliOutcome {
    match result {
        Ok(result) => value_result(command, contract, result),
        Err(error) => Err((command, "plenora-cli-error-v1", Box::new(error))),
    }
}

fn value_result<T: serde::Serialize>(
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

fn emit_success(command: &str, contract: &str, result: Value) -> ExitCode {
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

fn emit_error(command: &str, contract: &str, error: StorageError) -> ExitCode {
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

const fn error_exit_code(category: ErrorCategory) -> u8 {
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
