use super::*;
use clap::CommandFactory;

fn describe(mut command: clap::Command) -> Value {
    command.build();
    let arguments: Vec<_> = command.get_arguments().map(|arg| {
            let count = arg.get_num_args().unwrap_or_default();
            json!({
                "id": arg.get_id().as_str(), "long": arg.get_long(), "short": arg.get_short(),
                "aliases": arg.get_aliases(), "index": arg.get_index(),
                "required": arg.is_required_set(), "global": arg.is_global_set(),
                "action": format!("{:?}", arg.get_action()),
                "parser": format!("{:?}", arg.get_value_parser()),
                "min_values": count.min_values(), "max_values": count.max_values(),
                "defaults": arg.get_default_values().iter().map(|value| value.to_string_lossy()).collect::<Vec<_>>(),
                "values": arg.get_value_parser().possible_values().map(|values| values.map(|value| value.get_name().to_owned()).collect::<Vec<_>>()),
            })
        }).collect();
    let subcommands: Vec<_> = command.get_subcommands().cloned().map(describe).collect();
    json!({"name": command.get_name(), "arguments": arguments, "subcommands": subcommands})
}

#[test]
fn cli_public_api_matches_baseline() {
    let categories = [
        ErrorCategory::InvalidConfiguration,
        ErrorCategory::Unsupported,
        ErrorCategory::NotFound,
        ErrorCategory::Conflict,
        ErrorCategory::Authentication,
        ErrorCategory::Authorization,
        ErrorCategory::Timeout,
        ErrorCategory::Cancelled,
        ErrorCategory::ResourceLimit,
        ErrorCategory::Io,
        ErrorCategory::Protocol,
        ErrorCategory::Transient,
        ErrorCategory::Execution,
        ErrorCategory::Internal,
    ];
    let exits: serde_json::Map<String, Value> = categories
        .into_iter()
        .map(|category| {
            (
                serde_json::to_value(category)
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .to_owned(),
                json!(error_exit_code(category)),
            )
        })
        .collect();
    let actual = json!({"schema_version": 1, "protocol_version": CLI_PROTOCOL_VERSION,
            "command": describe(Cli::command()), "error_exit_codes": exits});
    if let Some(path) = std::env::var_os("PLENORA_API_SNAPSHOT_OUTPUT") {
        std::fs::write(path, serde_json::to_string_pretty(&actual).unwrap() + "\n").unwrap();
    } else {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../api/cli.json");
        let expected: Value =
            serde_json::from_slice(&std::fs::read(path).expect("CLI API baseline")).unwrap();
        assert_eq!(
            actual, expected,
            "CLI public API changed; inspect a candidate snapshot before updating the baseline"
        );
    }
}

#[test]
fn cli_inventory_detects_required_flag_and_parser_changes() {
    let baseline = describe(Cli::command());
    let optional_overwrite = Cli::command().mut_subcommand("put", |command| {
        command.mut_arg("overwrite", |arg| arg.required(false))
    });
    let changed_limit = Cli::command().mut_arg("max_transfer_bytes", |arg| {
        arg.value_parser(clap::value_parser!(u32))
    });
    assert_ne!(baseline, describe(optional_overwrite));
    assert_ne!(baseline, describe(changed_limit));
}
