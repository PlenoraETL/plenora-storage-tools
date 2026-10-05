//! FTP adapter for `plenora-storage-core`.
//!
//! FTP and explicit FTPS share streaming transfers and publication checks.
//! Neither promises atomic publication or create-if-absent: unsupported policies
//! fail before upload. A lost final reply can leave the remote effect unknown.

#![forbid(unsafe_code)]

mod errors;
mod operations;
mod publication;
mod transfer;
use errors::{
    committed_verification_error, configuration_error, list_name_error, list_parse_error,
    list_scan_limit_error, map_ftp_auth_error, map_ftp_error, transfer_io_error,
    transfer_limit_error,
};
use transfer::{
    abort_transfer, copy_with_control, ensure_parent_directories, finish_upload, ftp_object_exists,
    list_limit, public_metadata, scan_directory, stat_file, transfer_result,
};
use validation::{
    parse_config, validate_file_metadata, validate_ftp_publication, validate_key, validate_prefix,
};
mod validation;

use std::{
    collections::BTreeMap,
    sync::Arc,
    time::{Duration, SystemTime},
};

use async_trait::async_trait;
use plenora_storage_core::{
    ArtifactMetadata, CopyRequest, CredentialResolver, DeleteRequest, DeleteResult, ErrorCategory,
    ErrorPhase, GetRequest, IntegrityMetadata, ObjectMetadata, OperationContext,
    ProviderCapabilities, ProviderConnection, ProviderListRequest, ProviderListResult,
    PublicationPolicy, PutRequest, RemoteEffect, RetryDisposition, StatRequest, StorageError,
    StorageProvider, StorageResult, TestResult, TransferResult, directory_may_contain,
    key_matches_prefix, resolve_network_target, validate_object_key, validate_object_prefix,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use suppaftp::{
    FtpError, Mode, Status,
    list::{File, ListParser, ParseError},
    tokio::{
        AsyncRustlsConnector, AsyncRustlsFtpStream as AsyncFtpStream, AsyncRustlsStream,
        TransferStream,
    },
    types::FileType,
};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{
    AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader,
};

/// Stable provider identifier for dispatch and capability discovery.
pub const PROVIDER_ID: &str = "ftp";
/// Stable identifier for explicit FTPS.
pub const FTPS_PROVIDER_ID: &str = "ftps";
/// Versioned connection contract for explicit FTPS.
pub const FTPS_CONFIG_CONTRACT: &str = "plenora-storage-ftps-connection-v1";
/// Versioned connection contract accepted by this adapter.
pub const CONFIG_CONTRACT: &str = "plenora-storage-ftp-connection-v1";

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Passive FTP data-channel negotiation; active mode is not supported.
pub enum FtpMode {
    #[default]
    /// Use PASV; ignore the server-supplied data address and retain the control peer.
    Passive,
    /// Use EPSV with the control peer address.
    ExtendedPassive,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Non-secret configuration shared by FTP and explicit FTPS.
pub struct FtpConnectionConfig {
    /// Server host or IP address, validated against the engine network policy before dialing.
    pub host: String,
    #[serde(default = "default_port")]
    /// Nonzero control-channel port; defaults to 21 for FTP and explicit FTPS.
    pub port: u16,
    #[serde(default = "default_root")]
    /// Remote working directory, 1–4096 characters, without NUL, backslashes or parent segments.
    /// Defaults to `.`; the adapter changes to this directory after authentication.
    pub root: String,
    #[serde(default)]
    /// Passive data-channel mode; defaults to Passive and pins the control peer address.
    pub mode: FtpMode,
    #[serde(default, deserialize_with = "present_string")]
    /// Additional PEM trust anchors for FTPS; rejected for plaintext FTP.
    /// The key may be omitted, but `null` is rejected rather than read as absent.
    pub tls_ca_pem: Option<String>,
}

/// The connection schema types `tls_ca_pem` as a string: Serde would read
/// `null` as an omitted key and silently keep only the default trust anchors.
fn present_string<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    String::deserialize(deserializer).map(Some)
}

const fn default_port() -> u16 {
    21
}

fn default_root() -> String {
    ".".to_owned()
}

/// FTP/FTPS adapter with passive data connections constrained to the control peer.
pub struct FtpProvider {
    credentials: Arc<dyn CredentialResolver>,
    secure: bool,
}

impl FtpProvider {
    /// Creates plaintext FTP; use [`Self::new_ftps`] for explicit FTPS.
    #[must_use]
    pub fn new(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self {
            credentials,
            secure: false,
        }
    }

    /// Explicit FTPS with certificate/hostname verification and private data channels.
    #[must_use]
    pub fn new_ftps(credentials: Arc<dyn CredentialResolver>) -> Self {
        Self {
            credentials,
            secure: true,
        }
    }

    async fn connect(
        &self,
        connection: &ProviderConnection,
        context: &OperationContext<'_>,
    ) -> StorageResult<FtpConnection> {
        self.validate_connection(connection, context.policy)?;
        let config = parse_config(connection)?;
        if !self.secure && !context.policy.allow_insecure_ftp {
            return Err(StorageError::invalid_configuration(
                "INSECURE_FTP_FORBIDDEN",
                "plain FTP requires explicit engine authorization",
            )
            .with_provider(PROVIDER_ID));
        }
        // The control connection dials these addresses, never the host name
        // again: a second resolution could reach an address the policy just
        // rejected.
        let addresses = resolve_network_target(
            &config.host,
            config.port,
            context.policy.allow_private_network,
        )
        .await
        .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let credential = self
            .credentials
            .resolve(&connection.credential_ref)
            .map_err(|error| error.with_provider(PROVIDER_ID))?;
        let username = credential.required("username")?.to_owned();
        let password = credential.required("password")?.to_owned();
        let mut ftp = AsyncFtpStream::connect(addresses.as_slice())
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        if self.secure {
            use rustls::pki_types::pem::PemObject;
            let mut roots = rustls::RootCertStore::empty();
            for certificate in rustls_native_certs::load_native_certs().certs {
                roots.add(certificate).map_err(|_| configuration_error())?;
            }
            if let Some(pem) = &config.tls_ca_pem {
                let mut count = 0;
                for certificate in rustls::pki_types::CertificateDer::pem_slice_iter(pem.as_bytes())
                {
                    roots
                        .add(certificate.map_err(|_| configuration_error())?)
                        .map_err(|_| configuration_error())?;
                    count += 1;
                }
                if count == 0 {
                    return Err(configuration_error());
                }
            }
            let tls = rustls::ClientConfig::builder()
                .with_root_certificates(roots)
                .with_no_client_auth();
            let connector =
                AsyncRustlsConnector::from(tokio_rustls::TlsConnector::from(Arc::new(tls)));
            ftp = ftp
                .into_secure(connector, &config.host)
                .await
                .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        }
        ftp.login(username, password)
            .await
            .map_err(map_ftp_auth_error)?;
        ftp.set_mode(match config.mode {
            FtpMode::Passive => Mode::Passive,
            FtpMode::ExtendedPassive => Mode::ExtendedPassive,
        });
        // A PASV reply carries a server-chosen address. Without this, the data
        // channel would be dialled at an address the network policy never saw,
        // which reopens on the data connection the bypass the control
        // connection just closed. Only the port from the reply is used.
        ftp.set_passive_nat_workaround(true);
        ftp.transfer_type(FileType::Binary)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Prepare, false))?;
        ftp.cwd(&config.root)
            .await
            .map_err(|error| map_ftp_error(error, ErrorPhase::Connect, false))?;
        Ok(FtpConnection { ftp })
    }
}

struct FtpConnection {
    ftp: AsyncFtpStream,
}

// Includes MLSD facts and a UTF-8 name. Bounding a single line also stops a
// server that never sends a newline from growing the buffer without limit.
const MAX_MLSD_LINE_BYTES: u64 = 32 * 1_024;

// Compiled only by cargo-fuzz; FTP and FTPS use these same parsers.
#[cfg(fuzzing)]
/// Exercise bounded FTP/FTPS listing parsing in instrumented builds.
pub async fn fuzz_listing(data: &[u8]) {
    use transfer::{parse_listing_entry, read_listing_line};
    let mut reader = data;
    while let Ok(Some(line)) = read_listing_line(&mut reader).await {
        assert!(line.len() <= MAX_MLSD_LINE_BYTES as usize);
        if let Ok(file) = parse_listing_entry(&line) {
            let _ = public_metadata(file.name().to_owned(), &file);
        }
    }
}

/// Independent budget for tearing down a data transfer after a failure.
///
/// The caller's deadline may already have expired, and `ABOR` plus its replies
/// must not keep a cancelled operation running without bound.
const CLEANUP_BUDGET: Duration = Duration::from_secs(10);

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
