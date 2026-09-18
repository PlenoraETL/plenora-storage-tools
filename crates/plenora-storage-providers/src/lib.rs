//! Additional storage providers sharing the v1 operations and bounded transfers.
#![forbid(unsafe_code)]

mod azure_listing;
mod cloud;
mod common;
mod gcs;
mod http;
mod local;
mod smb;
mod smb_listing;
mod webdav;

pub use cloud::{Azure, AzureConnectionConfig};
pub use common::{Provider, ProviderFactory};
pub use gcs::{Gcs, GcsConnectionConfig};
pub use local::{Local, LocalConnectionConfig};
pub use smb::{Smb, SmbConnectionConfig};
pub use webdav::{WebDav, WebDavConnectionConfig};

pub type LocalProvider = Provider<Local>;
pub type AzureProvider = Provider<Azure>;
pub type GcsProvider = Provider<Gcs>;
pub type SmbProvider = Provider<Smb>;
pub type WebDavProvider = Provider<WebDav>;
