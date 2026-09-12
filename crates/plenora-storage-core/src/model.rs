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

/// Maximum number of `config` properties allowed by
/// `plenora-storage-connection-v1`.
pub const CONNECTION_CONFIG_MAX_PROPERTIES: usize = 32;

impl ProviderConnection {
    /// Enforces `plenora-storage-connection-v1` on the Rust surface.
    ///
    /// The Engine applies this to every operation so that the inline-secret ban
    /// holds for direct Rust callers, not only for payloads that happen to
    /// travel through the runtime binding or a JSON Schema validator.
    pub fn validate(&self) -> StorageResult<()> {
        if !is_provider_identifier(&self.provider) {
            return Err(connection_error(
                "STORAGE_CONNECTION_PROVIDER_INVALID",
                "storage connection provider identity is invalid",
            ));
        }
        if !is_config_contract_identifier(&self.config_contract) {
            return Err(connection_error(
                "STORAGE_CONNECTION_CONTRACT_INVALID",
                "storage connection configuration contract is invalid",
            ));
        }
        let Some(config) = self.config.as_object() else {
            return Err(connection_error(
                "STORAGE_CONNECTION_CONFIG_INVALID",
                "storage connection configuration must be a JSON object",
            ));
        };
        if config.len() > CONNECTION_CONFIG_MAX_PROPERTIES {
            return Err(connection_error(
                "STORAGE_CONNECTION_CONFIG_INVALID",
                "storage connection configuration exceeds its public property bound",
            ));
        }
        // Configuration is a flat map of scalars. Nesting would let a secret
        // hide below the level at which field names are inspected.
        if config
            .values()
            .any(|value| value.is_object() || value.is_array())
        {
            return Err(connection_error(
                "STORAGE_CONNECTION_CONFIG_INVALID",
                "storage connection configuration values must be scalars",
            ));
        }
        if config.keys().any(|name| is_secret_field_name(name)) {
            return Err(connection_error(
                "STORAGE_CONNECTION_INLINE_SECRET_FORBIDDEN",
                "storage connection configuration must reference secrets, never inline them",
            ));
        }
        if !is_credential_reference(&self.credential_ref) {
            return Err(connection_error(
                "STORAGE_CREDENTIAL_REFERENCE_INVALID",
                "storage credential reference must be an opaque protected reference",
            ));
        }
        Ok(())
    }
}

fn connection_error(code: &'static str, message: &'static str) -> StorageError {
    StorageError::invalid_configuration(code, message)
}

fn is_provider_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    // The charset is ASCII, so byte length and code points coincide here.
    value.len() <= 64
        && bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
        })
}

fn is_config_contract_identifier(value: &str) -> bool {
    let Some(remainder) = value.strip_prefix("plenora-") else {
        return false;
    };
    let Some((body, version)) = remainder.rsplit_once("-v") else {
        return false;
    };
    let versioned = !version.is_empty()
        && !version.starts_with('0')
        && version.bytes().all(|byte| byte.is_ascii_digit());
    let named = !body.is_empty()
        && body
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
    versioned && named
}

fn is_credential_reference(value: &str) -> bool {
    if !(5..=512).contains(&code_points(value)) {
        return false;
    }
    let Some((scheme, protected)) = value.split_once(':') else {
        return false;
    };
    let scheme_valid = (2..=32).contains(&scheme.len())
        && scheme.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(byte, b'+' | b'.' | b'-')
            }
        });
    let protected = protected.strip_prefix("//").unwrap_or(protected);
    scheme_valid
        && !protected.is_empty()
        && !protected.contains(char::is_whitespace)
        && !protected.contains('\\')
}

/// Mirrors the delimited `propertyNames` ban of
/// `plenora-storage-connection-v1`: a field is a secret when any underscore
/// delimited word, or pair of words, names credential material.
#[must_use]
pub fn is_secret_field_name(name: &str) -> bool {
    const SINGLE: [&str; 6] = [
        "password",
        "passwd",
        "credentials",
        "secret",
        "token",
        "authorization",
    ];
    const PAIRED: [(&str, &str); 3] = [("api", "key"), ("private", "key"), ("access", "key")];

    let normalized = name.to_ascii_lowercase();
    let words = normalized.split('_').collect::<Vec<_>>();
    words.iter().any(|word| SINGLE.contains(word))
        || words
            .windows(2)
            .any(|pair| PAIRED.contains(&(pair[0], pair[1])))
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
        if self
            .content_type
            .as_ref()
            .is_some_and(|value| !is_media_type(value))
            || self
                .sha256
                .as_ref()
                .is_some_and(|value| !is_lowercase_sha256(value))
        {
            return Err(StorageError::invalid_configuration(
                "ARTIFACT_METADATA_INVALID",
                "artifact metadata is invalid or exceeds its public bounds",
            ));
        }
        Ok(())
    }
}

/// Mirrors the `artifactMetadata.content_type` pattern of
/// `plenora-storage-common-v1`: a `type/subtype` media type with no parameters.
fn is_media_type(value: &str) -> bool {
    fn is_token(part: &str) -> bool {
        !part.is_empty()
            && part.bytes().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(
                        byte,
                        b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
                    )
            })
    }
    (3..=255).contains(&code_points(value))
        && value
            .split_once('/')
            .is_some_and(|(kind, subtype)| is_token(kind) && is_token(subtype))
}

fn is_lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
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
        if !is_artifact_reference(&self.reference) {
            return Err(StorageError::invalid_configuration(
                "ARTIFACT_REFERENCE_INVALID",
                "artifact reference must be an opaque artifact:// reference",
            ));
        }
        self.metadata.validate()?;
        Ok(())
    }
}

/// Mirrors the `artifactReference.reference` bounds and pattern of
/// `plenora-storage-common-v1`.
fn is_artifact_reference(value: &str) -> bool {
    let Some(opaque) = value.strip_prefix("artifact://") else {
        return false;
    };
    (12..=512).contains(&code_points(value))
        && opaque
            .bytes()
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && opaque.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(
                    byte,
                    b'.' | b'_'
                        | b'~'
                        | b'!'
                        | b'$'
                        | b'&'
                        | b'\''
                        | b'('
                        | b')'
                        | b'*'
                        | b'+'
                        | b','
                        | b';'
                        | b'='
                        | b':'
                        | b'@'
                        | b'/'
                        | b'?'
                        | b'#'
                        | b'%'
                        | b'-'
                )
        })
        && !opaque.split('/').any(|segment| segment == "..")
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

/// Maximum object key and prefix length, in Unicode code points.
///
/// JSON Schema `maxLength` counts code points, so measuring bytes here would
/// reject multibyte documents the public contract accepts.
pub const OBJECT_KEY_MAX_CHARS: usize = 4_096;

/// Counts a string the way JSON Schema `maxLength` does.
fn code_points(value: &str) -> usize {
    value.chars().count()
}

/// Enforces the `key` definition of `plenora-storage-common-v1`.
///
/// Every adapter shares this so that a document the public contract accepts is
/// accepted by all providers, and one it rejects is rejected by all of them.
pub fn validate_object_key(key: &str) -> StorageResult<()> {
    let valid = (1..=OBJECT_KEY_MAX_CHARS).contains(&code_points(key))
        && !key.contains('\\')
        && !key.contains('\0')
        && !key.starts_with('/')
        && !key.ends_with('/')
        && key
            .split('/')
            .all(|segment| !segment.is_empty() && !matches!(segment, "." | ".."));
    if valid {
        Ok(())
    } else {
        Err(StorageError::invalid_configuration(
            "OBJECT_KEY_INVALID",
            "storage object key must be a normalized relative path",
        ))
    }
}

/// Enforces the `prefix` definition of `plenora-storage-common-v1`. A prefix
/// may be empty and may end with `/`, but is otherwise a normalized key.
pub fn validate_object_prefix(prefix: &str) -> StorageResult<()> {
    let body = prefix.strip_suffix('/').unwrap_or(prefix);
    let valid = code_points(prefix) <= OBJECT_KEY_MAX_CHARS
        && !prefix.contains('\\')
        && !prefix.contains('\0')
        && !prefix.starts_with('/')
        && (body.is_empty()
            || body
                .split('/')
                .all(|segment| !segment.is_empty() && !matches!(segment, "." | "..")));
    if valid {
        Ok(())
    } else {
        Err(StorageError::invalid_configuration(
            "OBJECT_PREFIX_INVALID",
            "storage object prefix is invalid",
        ))
    }
}

/// True when `key` lies under `prefix`.
///
/// A prefix selects whole path segments. This is the only semantics every
/// provider can honour: the S3 object store lists a segment-aligned prefix, so a
/// literal string match would make `prefix = "in"` select `incoming/a.bin` on
/// the filesystem providers and nothing at all on S3.
#[must_use]
pub fn key_matches_prefix(key: &str, prefix: &str) -> bool {
    let boundary = prefix.trim_end_matches('/');
    boundary.is_empty() || key.starts_with(&format!("{boundary}/"))
}

/// True when some key below `directory_key` could still match `prefix`, so a
/// traversing provider knows whether entering the directory can yield a result.
#[must_use]
pub fn directory_may_contain(directory_key: &str, prefix: &str) -> bool {
    let boundary = prefix.trim_end_matches('/');
    boundary.is_empty()
        || directory_key == boundary
        || directory_key.starts_with(&format!("{boundary}/"))
        || boundary.starts_with(&format!("{directory_key}/"))
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

#[cfg(test)]
mod tests {
    use super::{directory_may_contain, key_matches_prefix};

    #[test]
    fn prefixes_select_whole_path_segments_on_every_provider() {
        assert!(key_matches_prefix("incoming/a.bin", "incoming/"));
        assert!(key_matches_prefix("incoming/a.bin", "incoming"));
        assert!(key_matches_prefix("incoming/deep/a.bin", "incoming"));
        assert!(key_matches_prefix("anything", ""));
        // A literal string match would select this; the object store providers
        // would not, so neither does the public semantics.
        assert!(!key_matches_prefix("incomingother/a.bin", "incoming"));
        assert!(!key_matches_prefix("incoming", "incoming"));
        assert!(!key_matches_prefix("other/a.bin", "incoming"));
    }

    #[test]
    fn traversal_only_enters_directories_that_can_still_match() {
        assert!(directory_may_contain("incoming", "incoming/deep/"));
        assert!(directory_may_contain("incoming/deep", "incoming/"));
        assert!(directory_may_contain("incoming", "incoming"));
        assert!(directory_may_contain("anything", ""));
        assert!(!directory_may_contain("incomingother", "incoming"));
        assert!(!directory_may_contain("other", "incoming"));
    }
}
