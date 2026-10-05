//! Pure-Rust SMB2/3 client library with pipelined I/O.
//!
//! No C dependencies, no FFI. Pipelined reads/writes fill the credit window
//! so downloads run ~10-25x faster than sequential SMB clients.
//!
//! # Quick start
//!
//! ```rust,no_run
//! use smb2::{SmbClient, ClientConfig};
//!
//! # async fn example() -> Result<(), smb2::Error> {
//! let mut client = smb2::connect("192.168.1.100:445", "user", "pass").await?;
//!
//! // List shares
//! let shares = client.list_shares().await?;
//!
//! // Connect to a share
//! let mut share = client.connect_share("Documents").await?;
//!
//! // List files
//! let entries = client.list_directory(&mut share, "projects/").await?;
//! for entry in &entries {
//!     println!("{} ({} bytes)", entry.name, entry.size);
//! }
//!
//! // Read a file
//! let data = client.read_file(&mut share, "report.pdf").await?;
//! # Ok(())
//! # }
//! ```
//!
//! # Modules
//!
//! - [`client`] -- High-level API: [`SmbClient`], [`Tree`], [`Pipeline`].
//!   This is what most users need.
//! - [`error`] -- Error types and NTSTATUS mapping.
//! - [`msg`] -- Wire format message structs (advanced/internal use).
//! - [`name`] -- The private-use-area mapping that lets a name carrying `?`,
//!   `*`, `"` or another SMB2-illegal character exist on a share at all. Runs
//!   on every path automatically; the functions are public for a consumer that
//!   wants to see the literal wire form.
//! - [`pack`] -- Binary serialization primitives (advanced/internal use).
//! - [`transport`] -- Transport trait and TCP implementation (advanced/internal use).
//! - [`crypto`] -- Signing and encryption (advanced/internal use).
//! - [`auth`] -- NTLM authentication (advanced/internal use).
//! - [`rpc`] -- Named pipe RPC for share enumeration (advanced/internal use).
//! - [`types`] -- Protocol newtypes and flag types (advanced/internal use).

#![forbid(unsafe_code)]
// This vendored fork inherits the workspace lints (`[lints] workspace = true`):
// `unsafe_code`, `missing_docs`, every `clippy::all` lint, and the CI
// anti-panic gate (no `unwrap`, `expect`, `panic!`, `unreachable!`, `todo!`
// or `unimplemented!` in library code) apply in full.
//
// The pedantic and nursery lints below are the ones upstream code violates
// today; each stays enforced everywhere else in the workspace, and any lint
// not listed here is enforced on this crate too. They are documentation,
// naming and style rules over ~3,000 upstream sites, and rewriting them would
// turn every upstream merge into a rewrite with no behavioural gain.
//
// The `cast_*` lints are the exception to "style": a silent truncation in a
// wire-format conversion is a correctness bug. They are allowed here only
// pending a dedicated review of each conversion, tracked as a follow-up.
#![allow(
    clippy::assigning_clones,
    clippy::branches_sharing_code,
    clippy::cast_lossless,
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::equatable_if_let,
    clippy::format_push_string,
    clippy::if_not_else,
    clippy::ignored_unit_patterns,
    clippy::items_after_statements,
    clippy::manual_let_else,
    clippy::map_unwrap_or,
    clippy::match_same_arms,
    clippy::missing_const_for_fn,
    clippy::missing_errors_doc,
    clippy::missing_panics_doc,
    clippy::must_use_candidate,
    clippy::needless_collect,
    clippy::needless_continue,
    clippy::needless_pass_by_ref_mut,
    clippy::needless_pass_by_value,
    clippy::option_if_let_else,
    clippy::or_fun_call,
    clippy::redundant_clone,
    clippy::redundant_closure_for_method_calls,
    clippy::redundant_pub_crate,
    clippy::semicolon_if_nothing_returned,
    clippy::significant_drop_in_scrutinee,
    clippy::significant_drop_tightening,
    clippy::similar_names,
    clippy::single_match_else,
    clippy::too_long_first_doc_paragraph,
    clippy::too_many_lines,
    clippy::trivially_copy_pass_by_ref,
    clippy::uninlined_format_args,
    clippy::unnested_or_patterns,
    clippy::unreadable_literal,
    clippy::unused_async,
    clippy::unused_self,
    clippy::use_self
)]
// Additionally violated by upstream tests only.
#![cfg_attr(
    test,
    allow(
        clippy::bool_to_int_with_if,
        clippy::float_cmp,
        clippy::manual_assert,
        clippy::many_single_char_names,
        clippy::range_plus_one,
        clippy::unchecked_time_subtraction
    )
)]

pub mod auth;
mod bytes;
pub mod client;
pub mod crypto;
pub mod error;
pub mod msg;
pub mod name;
pub mod pack;
pub mod rpc;
mod sync;
#[cfg(feature = "testing")]
pub mod testing;
pub mod transport;
pub mod types;

#[cfg(feature = "fuzzing")]
pub mod fuzzing;

// ── Re-exports: the simple-case imports ────────────────────────────────

// Error types
pub use error::{Error, ErrorKind, Result};

/// Filename mapping for characters SMB2 does not allow on the wire.
pub use name::{decode_name, decode_path, encode_name, encode_path};

// High-level client
pub use client::{ClientConfig, SmbClient, connect};

// Streaming I/O
pub use client::stream::{FileDownload, FileReader, FileUpload, FileWriter, Progress};

// Server-side copy (FSCTL_SRV_COPYCHUNK): copy byte ranges between two files
// on the server without the data crossing the wire.
pub use client::copy::{
    CopyChunk, CopyChunkOutcome, CopyChunkResult, ResumeKey, ServerSideCopyLimits,
};

// Tree and file types
pub use client::tree::{
    DfsOrigin, DirectoryEntry, FileInfo, FsInfo, ListingTrace, QueryStep, Tree,
};

// Pipeline
pub use client::pipeline::{Op, OpResult, Pipeline};

// Connection-level types (useful for advanced users)
pub use client::connection::{
    CompoundOp, Frame, NegotiatedParams, ReconnectEvent, ReconnectObserver, ReconnectPolicy,
    SessionReviver,
};
pub use client::session::Session;

// Diagnostics: snapshot tree returned by `SmbClient::diagnostics()` /
// `Connection::diagnostics()`.
pub use client::diagnostics::{
    ClientInfo, ClientMetricsSnapshot, CompressionInfo, ConnectionDiagnostics, CreditInfo,
    DfsCacheEntry, Diagnostics, EncryptionInfo, MetricsSnapshot, NegotiatedSummary,
    SessionDiagnostics, SigningInfo,
};

// File watching
pub use client::watcher::{FileNotifyAction, FileNotifyEvent, Watcher};

// Share enumeration
pub use rpc::srvsvc::ShareInfo;

// Kerberos authentication
pub use auth::kerberos::{KerberosAuthenticator, KerberosCredentials};
