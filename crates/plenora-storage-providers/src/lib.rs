//! Additional storage providers sharing the v1 operations and bounded transfers.
#![forbid(unsafe_code)]

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
pub type LocalProvider = Provider<Local>;
#[cfg(feature = "azure")]
pub type AzureProvider = Provider<Azure>;
#[cfg(feature = "gcs")]
pub type GcsProvider = Provider<Gcs>;
#[cfg(feature = "smb")]
pub type SmbProvider = Provider<Smb>;
#[cfg(feature = "webdav")]
pub type WebDavProvider = Provider<WebDav>;
