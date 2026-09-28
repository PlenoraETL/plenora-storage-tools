//! Additional storage providers sharing the v1 operations and bounded transfers.
//!
//! Local, Azure Blob, GCS, SMB and `WebDAV` use streaming downloads and bounded
//! upload/copy buffers. Discovery describes supported request policies; protocol
//! support does not certify every server implementation or real cloud account.
//! Local access uses process permissions. Remote credentials are resolved when
//! an operation connects, and GCS token refresh remains the host's responsibility.
#![forbid(unsafe_code)]

// Entry points exist only in instrumented fuzz builds, never in product builds.
#[cfg(fuzzing)]
/// Parser entry points available only in instrumented builds.
pub mod parser_fuzz {
    #[cfg(feature = "azure")]
    /// Validate an Azure listing response without contacting a server.
    pub fn azure(data: &[u8]) {
        crate::azure_listing::fuzz_listing(data);
    }
    #[cfg(feature = "webdav")]
    /// Parse a DAV property response without contacting a server.
    pub fn webdav(data: &[u8]) {
        crate::webdav::fuzz_properties(data);
    }
}

#[cfg(feature = "azure")]
mod azure_listing;
#[cfg(feature = "azure")]
mod cloud;
mod common;
#[cfg(feature = "gcs")]
mod gcs;
#[cfg(any(feature = "azure", feature = "gcs", feature = "webdav"))]
mod http;
mod keys;
#[cfg(feature = "local")]
mod local;
#[cfg(feature = "smb")]
mod smb;
#[cfg(feature = "smb")]
mod smb_listing;
#[cfg(feature = "webdav")]
mod webdav;

#[cfg(feature = "azure")]
pub use cloud::{Azure, AzureConnectionConfig};
pub use common::{Provider, ProviderFactory};
#[cfg(feature = "gcs")]
pub use gcs::{Gcs, GcsConnectionConfig};
#[cfg(feature = "local")]
pub use local::{Local, LocalConnectionConfig};
#[cfg(feature = "smb")]
pub use smb::{Smb, SmbConnectionConfig};
#[cfg(feature = "webdav")]
pub use webdav::{WebDav, WebDavConnectionConfig};

#[cfg(feature = "local")]
/// Shared operation wrapper for the local filesystem backend.
pub type LocalProvider = Provider<Local>;
#[cfg(feature = "azure")]
/// Shared operation wrapper for the Azure Blob backend.
pub type AzureProvider = Provider<Azure>;
#[cfg(feature = "gcs")]
/// Shared operation wrapper for the GCS backend.
pub type GcsProvider = Provider<Gcs>;
#[cfg(feature = "smb")]
/// Shared operation wrapper for the encrypted SMB3 backend.
pub type SmbProvider = Provider<Smb>;
#[cfg(feature = "webdav")]
/// Shared operation wrapper for the qualified `WebDAV` backend.
pub type WebDavProvider = Provider<WebDav>;
