use std::collections::BTreeMap;

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

use crate::{StorageError, StorageResult};

/// Accepted version of storage operation envelopes.
pub const OPERATION_SCHEMA_VERSION: u32 = 1;

/// Reads an optional field whose contract allows omission but not `null`.
/// Serde would otherwise read `null` as absent, so a value that the contract
/// rejects would silently select the default. Pair with `#[serde(default)]`.
pub fn present_value<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

/// Reads a field whose contract requires the key but allows `null`. Without a
/// custom deserializer Serde treats a missing `Option` key as `null`.
fn required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

/// Provider selection and non-secret connection configuration.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProviderConnection {
    /// Stable provider identifier used for dispatch and capability discovery.
    pub provider: String,
    /// Versioned provider connection contract; must match the selected adapter.
    pub config_contract: String,
    /// Non-secret flat JSON object with scalar values; at most 32 properties.
    pub config: Value,
    /// Opaque protected credential reference resolved by the host; never inline secrets.
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
    ///
    /// # Errors
    /// Returns invalid-configuration when fields, references, metadata or public bounds violate the corresponding contract; performs no external work.
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
/// Engine list request with an optional engine-local continuation token.
pub struct ListRequest {
    /// Optional segment prefix; empty selects the whole namespace, not a text substring.
    pub prefix: Option<String>,
    /// Opaque engine-local continuation token bound to connection and list parameters.
    pub cursor: Option<String>,
    /// Optional page size bounded by engine policy; not a universal server scan limit.
    pub max_items: Option<usize>,
}

/// Provider-facing list request after the Engine has resolved an opaque cursor.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ProviderListRequest {
    /// Optional segment prefix; empty selects the whole namespace, not a text substring.
    pub prefix: Option<String>,
    /// Exclusive normalized key boundary resolved by the engine from its cursor.
    pub start_after: Option<String>,
    /// Optional page size bounded by engine policy; not a universal server scan limit.
    pub max_items: Option<usize>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Read metadata for a single normalized object key.
pub struct StatRequest {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Download one object into a caller-owned byte sink.
pub struct GetRequest {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Upload parameters validated before consuming the caller-owned source.
pub struct PutRequest {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
    /// Whether an existing destination may be replaced; unsupported exclusivity is rejected.
    pub overwrite: bool,
    /// Required visibility guarantee; unsupported policies fail before mutation.
    pub publication_policy: PublicationPolicy,
    /// Optional media type without parameters, validated before transfer.
    pub content_type: Option<String>,
    /// Optional declared source length in bytes; transfer must match when supplied.
    pub content_length: Option<u64>,
    #[serde(default)]
    /// Operation metadata validated against the corresponding public contract.
    pub metadata: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Delete one normalized object key with an explicit missing-object policy.
pub struct DeleteRequest {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
    /// Return a successful non-deletion when the object is already absent.
    pub ignore_missing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Copy within one connection; cross-provider copying is not supported.
pub struct CopyRequest {
    /// Normalized relative key to read within the same provider connection.
    pub source_key: String,
    /// Normalized relative key to publish within the same provider connection.
    pub destination_key: String,
    /// Whether an existing destination may be replaced; unsupported exclusivity is rejected.
    pub overwrite: bool,
    /// Required visibility guarantee; unsupported policies fail before mutation.
    pub publication_policy: PublicationPolicy,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Visibility guarantee required when publishing an upload or copy.
pub enum PublicationPolicy {
    /// Use available publication semantics without promising atomic replacement.
    BestEffort,
    /// Reject unless the provider and deployment can guarantee atomic publication.
    AtomicRequired,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Optional content type, byte length and integrity declared by an artifact owner.
///
/// `plenora-storage-common-v1` requires all three keys and allows `null` for an
/// unknown value; a missing key is rejected rather than read as `null`.
pub struct ArtifactMetadata {
    #[serde(deserialize_with = "required_nullable")]
    /// Optional media type without parameters, validated before transfer.
    pub content_type: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    /// Object or artifact length in bytes.
    pub size: Option<u64>,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional lowercase hexadecimal SHA-256 digest, distinct from `ETag` and version ID.
    pub sha256: Option<String>,
}

impl ArtifactMetadata {
    ///
    /// # Errors
    /// Returns invalid-configuration when fields, references, metadata or public bounds violate the corresponding contract; performs no external work.
    /// Validate the public reference and metadata before resolver or provider access.
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
    /// Host-resolved artifact URI; validated before the resolver is invoked.
    pub reference: String,
    /// Operation metadata validated against the corresponding public contract.
    pub metadata: ArtifactMetadata,
}

impl ArtifactReference {
    ///
    /// # Errors
    /// Returns invalid-configuration when fields, references, metadata or public bounds violate the corresponding contract; performs no external work.
    /// Validate the public reference and metadata before resolver or provider access.
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
    /// Host-resolved artifact URI; validated before the resolver is invoked.
    pub reference: String,
    /// Whether an existing destination may be replaced; unsupported exclusivity is rejected.
    pub overwrite: bool,
    /// Operation metadata validated against the corresponding public contract.
    pub metadata: ArtifactMetadata,
}

impl ArtifactSinkReference {
    ///
    /// # Errors
    /// Returns invalid-configuration when fields, references, metadata or public bounds violate the corresponding contract; performs no external work.
    /// Validate the public reference and metadata before resolver or provider access.
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
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
}

/// Serialized `storage.list` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
    pub request: ListRequest,
}

/// Serialized `storage.stat` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StatInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
    pub request: StatRequest,
}

/// Serialized `storage.get` input. The consumer resolves the opaque sink and
/// then passes the resulting writer to [`crate::Engine::get`].
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GetInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
    pub request: GetRequest,
    /// Host-owned destination reference and its publication/metadata constraints.
    pub artifact_sink: ArtifactSinkReference,
}

/// Serialized `storage.put` input. The consumer resolves the opaque source and
/// then passes the resulting reader to [`crate::Engine::put`].
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PutInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
    pub request: PutRequest,
    /// Host-owned source reference and its declared integrity metadata.
    pub artifact_source: ArtifactReference,
}

/// Serialized `storage.copy` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CopyInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
    pub request: CopyRequest,
}

/// Serialized `storage.delete` input used at JSON and runtime boundaries.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeleteInput {
    /// Version of the serialized envelope; validated against the operation contract.
    pub schema_version: u32,
    /// Provider selection, non-secret configuration and opaque credential reference.
    pub connection: ProviderConnection,
    #[serde(flatten)]
    /// Operation-specific parameters after envelope deserialization.
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
///
/// # Errors
/// Returns invalid-configuration for empty or oversized keys, forbidden separators, control characters, empty, dot or parent segments.
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
///
/// # Errors
/// Returns invalid-configuration for a nonempty prefix that is not a valid normalized object key.
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

///
/// # Errors
/// Returns invalid-configuration when the supplied version differs from the supported operation schema.
/// Require the supported operation envelope version before dispatch.
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
/// Successful connection probe; not a blanket compatibility certification.
pub struct TestResult {
    /// Stable provider identifier used for dispatch and capability discovery.
    pub provider: String,
    /// Whether the provider probe completed successfully for this connection.
    pub reachable: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Provider metadata; opaque validators remain distinct from content digests.
pub struct ObjectMetadata {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
    /// Object or artifact length in bytes.
    pub size: u64,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional provider timestamp in RFC 3339 format; not synthesized when unavailable.
    pub last_modified: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional opaque provider entity tag; not a SHA-256 digest.
    pub etag: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional opaque provider object version identifier, distinct from a checksum.
    pub version: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Engine list page and an optional process-local continuation token.
pub struct ListResult {
    /// Metadata page ordered by normalized object key.
    pub objects: Vec<ObjectMetadata>,
    /// Whether further matching entries may be obtained with the continuation boundary.
    pub truncated: bool,
    #[serde(deserialize_with = "required_nullable")]
    /// Opaque continuation token valid only in this live engine and original list scope.
    pub next_cursor: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
/// Provider page with an exclusive key boundary, before cursor encoding.
pub struct ProviderListResult {
    /// Metadata page ordered by normalized object key.
    pub objects: Vec<ObjectMetadata>,
    /// Whether further matching entries may be obtained with the continuation boundary.
    pub truncated: bool,
    /// Exclusive key boundary for the next provider page when truncated.
    pub next_start_after: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Algorithm and encoded digest for bytes actually traversed.
pub struct IntegrityMetadata {
    /// Integrity algorithm identifier, currently SHA-256 for streamed transfers.
    pub algorithm: String,
    /// Encoded digest computed from bytes actually traversed, not from provider `ETags`.
    pub value: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Transfer accounting and integrity after the sink or remote operation succeeds.
pub struct TransferResult {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
    /// Number of bytes accepted by the transfer path.
    pub bytes_transferred: u64,
    /// Integrity of bytes traversed by this transfer.
    pub checksum: IntegrityMetadata,
    /// Metadata derived from the bytes traversed by this transfer.
    pub artifact: ArtifactMetadata,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional opaque provider entity tag; not a SHA-256 digest.
    pub etag: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    /// Optional opaque provider object version identifier, distinct from a checksum.
    pub version: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Outcome of the requested deletion, including an allowed absent object.
pub struct DeleteResult {
    /// Normalized relative object key; empty, dot and parent segments are rejected.
    pub key: String,
    /// Whether an object was removed; false is possible with `ignore_missing`.
    pub deleted: bool,
}

#[cfg(test)]
#[path = "model_tests.rs"]
mod tests;
