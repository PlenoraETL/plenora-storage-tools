use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{StorageError, StorageResult};

pub const OPERATION_SCHEMA_VERSION: u32 = 1;

/// Provider selection and non-secret connection configuration.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProviderConnection {
    pub provider: String,
    pub config_contract: String,
    pub config: Value,
    pub credential_ref: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct ListRequest {
    pub prefix: Option<String>,
    pub cursor: Option<String>,
    pub max_items: Option<usize>,
}

/// Provider-facing list request after the Engine has resolved an opaque cursor.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ProviderListRequest {
    pub prefix: Option<String>,
    pub start_after: Option<String>,
    pub max_items: Option<usize>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StatRequest {
    pub key: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GetRequest {
    pub key: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PutRequest {
    pub key: String,
    pub overwrite: bool,
    pub publication_policy: PublicationPolicy,
    pub content_type: Option<String>,
    pub content_length: Option<u64>,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeleteRequest {
    pub key: String,
    pub ignore_missing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CopyRequest {
    pub source_key: String,
    pub destination_key: String,
    pub overwrite: bool,
    pub publication_policy: PublicationPolicy,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PublicationPolicy {
    BestEffort,
    AtomicRequired,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactMetadata {
    pub content_type: Option<String>,
    pub size: Option<u64>,
    pub sha256: Option<String>,
}

impl ArtifactMetadata {
    pub fn validate(&self) -> StorageResult<()> {
        if self.content_type.as_ref().is_some_and(|value| {
            value.is_empty() || value.len() > 255 || value.contains(char::is_whitespace)
        }) || self.sha256.as_ref().is_some_and(|value| {
            value.len() != 64
                || !value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        }) {
            return Err(StorageError::invalid_configuration(
                "ARTIFACT_METADATA_INVALID",
                "artifact metadata is invalid or exceeds its public bounds",
            ));
        }
        Ok(())
    }
}

/// Opaque runtime-owned artifact reference. It is never a local path.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactReference {
    pub reference: String,
    pub metadata: ArtifactMetadata,
}

impl ArtifactReference {
    pub fn validate(&self) -> StorageResult<()> {
        let value = self.reference.as_str();
        if value.len() > 512
            || !value.starts_with("artifact://")
            || value.contains(char::is_whitespace)
            || value.contains('\\')
            || value.split('/').any(|segment| segment == "..")
        {
            return Err(StorageError::invalid_configuration(
                "ARTIFACT_REFERENCE_INVALID",
                "artifact reference must be an opaque artifact:// reference",
            ));
        }
        self.metadata.validate()?;
        Ok(())
    }
}

/// Artifact destination selected by a runtime consumer.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactSinkReference {
    pub reference: String,
    pub overwrite: bool,
    pub metadata: ArtifactMetadata,
}

impl ArtifactSinkReference {
    pub fn validate(&self) -> StorageResult<()> {
        ArtifactReference {
            reference: self.reference.clone(),
            metadata: self.metadata.clone(),
        }
        .validate()
    }
}

/// Serialized `storage.test` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TestInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
}

/// Serialized `storage.list` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: ListRequest,
}

/// Serialized `storage.stat` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StatInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: StatRequest,
}

/// Serialized `storage.get` input. The consumer resolves the opaque sink and
/// then passes the resulting writer to [`crate::Engine::get`].
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GetInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: GetRequest,
    pub artifact_sink: ArtifactSinkReference,
}

/// Serialized `storage.put` input. The consumer resolves the opaque source and
/// then passes the resulting reader to [`crate::Engine::put`].
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PutInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: PutRequest,
    pub artifact_source: ArtifactReference,
}

/// Serialized `storage.copy` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CopyInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: CopyRequest,
}

/// Serialized `storage.delete` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeleteInput {
    pub schema_version: u32,
    pub connection: ProviderConnection,
    #[serde(flatten)]
    pub request: DeleteRequest,
}

pub fn validate_operation_schema_version(schema_version: u32) -> StorageResult<()> {
    if schema_version != OPERATION_SCHEMA_VERSION {
        return Err(StorageError::invalid_configuration(
            "OPERATION_SCHEMA_VERSION_UNSUPPORTED",
            "storage operation schema version is unsupported",
        ));
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TestResult {
    pub provider: String,
    pub reachable: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ObjectMetadata {
    pub key: String,
    pub size: u64,
    pub last_modified: Option<String>,
    pub etag: Option<String>,
    pub version: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListResult {
    pub objects: Vec<ObjectMetadata>,
    pub truncated: bool,
    pub next_cursor: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderListResult {
    pub objects: Vec<ObjectMetadata>,
    pub truncated: bool,
    pub next_start_after: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct IntegrityMetadata {
    pub algorithm: String,
    pub value: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TransferResult {
    pub key: String,
    pub bytes_transferred: u64,
    pub checksum: IntegrityMetadata,
    pub artifact: ArtifactMetadata,
    pub etag: Option<String>,
    pub version: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeleteResult {
    pub key: String,
    pub deleted: bool,
}
