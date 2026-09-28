//! Command dispatch, bounded connection input and in-process pagination.

use super::{
    AsyncReadExt, CliOutcome, Command, CopyRequest, DeleteRequest, Engine, ErrorCategory,
    ErrorPhase, ExecutionControl, ListRequest, Path, PathBuf, ProviderConnection, PutFileOptions,
    RemoteEffect, RetryDisposition, StatRequest, StorageError, StorageResult, Surface, fs,
    get_to_file, operation_result, put_from_file, value_result,
};

#[allow(
    clippy::too_many_lines,
    reason = "Keep command-to-contract routing exhaustive in one match; I/O helpers are separate"
)]
pub async fn execute(
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
    let read_error = |error: std::io::Error| {
        StorageError::new(
            match error.kind() {
                std::io::ErrorKind::NotFound => ErrorCategory::NotFound,
                std::io::ErrorKind::PermissionDenied => ErrorCategory::Authorization,
                _ => ErrorCategory::Io,
            },
            ErrorPhase::Read,
            RemoteEffect::None,
            RetryDisposition::Never,
            "CONNECTION_FILE_READ_FAILED",
            "connection file could not be read",
        )
    };
    if !fs::metadata(path).await.map_err(read_error)?.is_file() {
        return Err(StorageError::invalid_configuration(
            "CONNECTION_NOT_REGULAR_FILE",
            "connection input must be a regular file",
        ));
    }
    let file = fs::File::open(path).await.map_err(read_error)?;
    let mut data = Vec::new();
    file.take(1_048_577)
        .read_to_end(&mut data)
        .await
        .map_err(read_error)?;
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
