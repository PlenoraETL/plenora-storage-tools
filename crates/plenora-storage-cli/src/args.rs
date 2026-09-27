//! Command arguments and explicit host authorizations.

use super::{CliPublicationPolicy, OutputFormat};
use clap::{Args, Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "plenora-storage", disable_version_flag = true)]
// These are separate, global opt-ins so operators must authorize each relaxed
// security boundary explicitly on the command line.
#[allow(clippy::struct_excessive_bools)]
pub struct Cli {
    #[arg(long, global = true, value_enum)]
    pub format: Option<OutputFormat>,
    #[arg(long, global = true)]
    pub version: bool,
    #[arg(long, global = true)]
    pub deadline: Option<String>,
    #[arg(
        long,
        global = true,
        help = "Compatibility option; qualified v1 operations no longer require experimental opt-in"
    )]
    pub allow_experimental_contracts: bool,
    #[arg(long, global = true)]
    pub allow_insecure_http: bool,
    #[arg(long, global = true)]
    pub allow_insecure_ftp: bool,
    #[arg(long, global = true)]
    pub allow_private_network: bool,
    #[arg(long, global = true)]
    pub allow_unverified_ssh: bool,
    #[arg(long, global = true, default_value_t = 1_073_741_824)]
    pub max_transfer_bytes: u64,
    #[arg(long, global = true, default_value_t = 10_000)]
    pub max_list_items: usize,
    #[arg(long, global = true, default_value_t = plenora_storage_core::DEFAULT_MAX_BUFFERED_PUT_BYTES)]
    pub max_buffered_put_bytes: u64,
    #[command(subcommand)]
    pub command: Option<Command>,
}

#[derive(Subcommand)]
pub enum Command {
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
pub struct ConnectionArgs {
    #[arg(long)]
    pub connection: PathBuf,
}
