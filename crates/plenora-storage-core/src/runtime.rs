//! Transport-neutral Runtime Binding 1.0 boundary. A final application can
//! wrap this type in its runtime `CapabilityHandler`; this module deliberately
//! does not depend on `runtime-tools`.

use std::{pin::Pin, time::Instant};

use async_trait::async_trait;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};

use crate::{
    ArtifactReference, ArtifactSinkReference, CAPABILITY_NAME, CancellationToken, CopyInput,
    DeleteInput, Engine, ErrorCategory, ErrorPhase, ExecutionControl, GetInput, ListInput,
    PutInput, RemoteEffect, RetryDisposition, SideEffect, StatInput, StorageError, StorageResult,
    TestInput, TransferResult, validate_operation_schema_version,
};

pub const RUNTIME_BINDING_VERSION: u32 = 1;
pub const JSON_CONTENT_TYPE: &str = "application/json";
pub const ERROR_CONTENT_TYPE: &str = "application/vnd.plenora.error+json";
pub const ERROR_CONTRACT: &str = "plenora-error-v1";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArtifactRole {
    None,
    Source,
    Sink,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RuntimeOperationDescriptor {
    pub operation: &'static str,
    pub version: u32,
    pub input_contract: &'static str,
    pub output_contract: &'static str,
    pub content_type: &'static str,
    pub side_effect: SideEffect,
    pub artifact_role: ArtifactRole,
    pub cancellation: bool,
    pub deadline: bool,
    pub idempotency_key: bool,
}

pub const RUNTIME_OPERATIONS: [RuntimeOperationDescriptor; 7] = [
    descriptor(
        "storage.test",
        "plenora-storage-test-input-v1",
        "plenora-storage-test-output-v1",
        SideEffect::None,
        ArtifactRole::None,
    ),
    descriptor(
        "storage.list",
        "plenora-storage-list-input-v1",
        "plenora-storage-list-output-v1",
        SideEffect::None,
        ArtifactRole::None,
    ),
    descriptor(
        "storage.stat",
        "plenora-storage-stat-input-v1",
        "plenora-storage-stat-output-v1",
        SideEffect::None,
        ArtifactRole::None,
    ),
    descriptor(
        "storage.get",
        "plenora-storage-get-input-v1",
        "plenora-storage-get-output-v1",
        SideEffect::Remote,
        ArtifactRole::Sink,
    ),
    descriptor(
        "storage.put",
        "plenora-storage-put-input-v1",
        "plenora-storage-put-output-v1",
        SideEffect::Remote,
        ArtifactRole::Source,
    ),
    descriptor(
        "storage.copy",
        "plenora-storage-copy-input-v1",
        "plenora-storage-copy-output-v1",
        SideEffect::Remote,
        ArtifactRole::None,
    ),
    descriptor(
        "storage.delete",
        "plenora-storage-delete-input-v1",
        "plenora-storage-delete-output-v1",
        SideEffect::Remote,
        ArtifactRole::None,
    ),
];

const fn descriptor(
    operation: &'static str,
    input_contract: &'static str,
    output_contract: &'static str,
    side_effect: SideEffect,
    artifact_role: ArtifactRole,
) -> RuntimeOperationDescriptor {
    RuntimeOperationDescriptor {
        operation,
        version: 1,
        input_contract,
        output_contract,
        content_type: JSON_CONTENT_TYPE,
        side_effect,
        artifact_role,
        cancellation: true,
        deadline: true,
        idempotency_key: false,
    }
}

#[derive(Clone, Copy, Debug)]
pub struct RuntimeRoute<'a> {
    pub capability_name: &'a str,
    pub capability_version: u32,
    pub operation: &'a str,
    pub operation_version: u32,
    pub input_contract: &'a str,
    pub content_type: &'a str,
    pub idempotency_key: Option<&'a str>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeInvocation {
    pub content_type: String,
    pub metadata: RuntimeRequestMetadata,
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeRequestMetadata {
    #[serde(rename = "plenora.message.id")]
    pub message_id: String,
    #[serde(
        rename = "plenora.message.causation_id",
        default,
        skip_serializing_if = "Option::is_none"
    )]
    pub causation_id: Option<String>,
    #[serde(rename = "plenora.capability.name")]
    pub capability_name: String,
    #[serde(rename = "plenora.capability.version")]
    pub capability_version: String,
    #[serde(rename = "plenora.capability.operation")]
    pub operation: String,
    #[serde(rename = "plenora.operation.version")]
    pub operation_version: String,
    #[serde(rename = "plenora.input.contract")]
    pub input_contract: String,
    #[serde(rename = "plenora.execution.deadline", default)]
    pub deadline: Option<String>,
    #[serde(rename = "plenora.idempotency.key", default)]
    pub idempotency_key: Option<String>,
    #[serde(rename = "plenora.trace.correlation_id")]
    pub correlation_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeResultEnvelope {
    pub content_type: String,
    pub metadata: RuntimeResultMetadata,
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeResultMetadata {
    #[serde(rename = "plenora.message.id")]
    pub message_id: String,
    #[serde(
        rename = "plenora.message.causation_id",
        default,
        skip_serializing_if = "Option::is_none"
    )]
    pub causation_id: Option<String>,
    #[serde(rename = "plenora.capability.operation")]
    pub operation: String,
    #[serde(rename = "plenora.operation.version")]
    pub operation_version: String,
    #[serde(rename = "plenora.output.contract")]
    pub output_contract: String,
    #[serde(rename = "plenora.trace.correlation_id")]
    pub correlation_id: String,
}

pub type ArtifactSource = Pin<Box<dyn AsyncRead + Send + Unpin>>;
pub type ArtifactSink = Pin<Box<dyn AsyncWrite + Send + Unpin>>;

#[async_trait]
pub trait ArtifactResolver: Send + Sync {
    async fn open_source(&self, source: &ArtifactReference) -> StorageResult<ArtifactSource>;
    async fn open_sink(&self, sink: &ArtifactSinkReference) -> StorageResult<ArtifactSink>;
}

/// Application-owned authorization for protected secret references. Provider
/// adapters still receive secret material through their `CredentialResolver`;
///
/// a consumer normally backs both traits with the same secret authority.
pub trait SecretResolver: Send + Sync {
    fn authorize(&self, reference: &str) -> StorageResult<()>;
}

pub struct RuntimeBinding<'a> {
    engine: &'a Engine,
    artifacts: &'a dyn ArtifactResolver,
    secrets: &'a dyn SecretResolver,
}

impl<'a> RuntimeBinding<'a> {
    #[must_use]
    pub const fn new(
        engine: &'a Engine,
        artifacts: &'a dyn ArtifactResolver,
        secrets: &'a dyn SecretResolver,
    ) -> Self {
        Self {
            engine,
            artifacts,
            secrets,
        }
    }

    pub async fn invoke(
        &self,
        invocation: RuntimeInvocation,
        cancellation: CancellationToken,
    ) -> RuntimeResultEnvelope {
        let identity = RuntimeResultMetadata {
            message_id: public_uuid(&invocation.metadata.message_id),
            causation_id: invocation
                .metadata
                .causation_id
                .as_deref()
                .filter(|value| canonical_uuid(value))
                .map(str::to_owned),
            operation: RUNTIME_OPERATIONS
                .iter()
                .find(|item| item.operation == invocation.metadata.operation)
                .map_or("storage.unknown", |item| item.operation)
                .to_owned(),
            operation_version: invocation
                .metadata
                .operation_version
                .parse::<u32>()
                .map_or_else(|_| "0".to_owned(), |version| version.to_string()),
            output_contract: ERROR_CONTRACT.to_owned(),
            correlation_id: public_uuid(&invocation.metadata.correlation_id),
        };
        match self.invoke_inner(&invocation, cancellation).await {
            Ok((descriptor, payload)) => RuntimeResultEnvelope {
                content_type: descriptor.content_type.to_owned(),
                metadata: RuntimeResultMetadata {
                    output_contract: descriptor.output_contract.to_owned(),
                    ..identity
                },
                payload,
            },
            Err(error) => RuntimeResultEnvelope {
                content_type: ERROR_CONTENT_TYPE.to_owned(),
                metadata: identity,
                payload: serde_json::to_value(error).unwrap_or_else(|_| {
                    serde_json::json!({
                        "category": "internal",
                        "phase": "cleanup",
                        "remote_effect": "none",
                        "retry": {"kind": "never"},
                        "code": "ERROR_SERIALIZATION_FAILED",
                        "message": "terminal storage error serialization failed",
                        "provider": null,
                        "details": {}
                    })
                }),
            },
        }
    }

    async fn invoke_inner(
        &self,
        invocation: &RuntimeInvocation,
        cancellation: CancellationToken,
    ) -> StorageResult<(&'static RuntimeOperationDescriptor, Value)> {
        if !canonical_uuid(&invocation.metadata.message_id)
            || !canonical_uuid(&invocation.metadata.correlation_id)
            || invocation
                .metadata
                .causation_id
                .as_deref()
                .is_some_and(|value| !canonical_uuid(value))
        {
            return Err(StorageError::new(
                ErrorCategory::Protocol,
                ErrorPhase::Validate,
                RemoteEffect::None,
                RetryDisposition::Never,
                "RUNTIME_IDENTITY_INVALID",
                "runtime identities must be canonical lowercase hyphenated UUIDs",
            ));
        }
        validate_payload_security(&invocation.payload)?;
        let capability_version = parse_version(&invocation.metadata.capability_version)?;
        let operation_version = parse_version(&invocation.metadata.operation_version)?;
        let descriptor = validate_runtime_route(RuntimeRoute {
            capability_name: &invocation.metadata.capability_name,
            capability_version,
            operation: &invocation.metadata.operation,
            operation_version,
            input_contract: &invocation.metadata.input_contract,
            content_type: &invocation.content_type,
            idempotency_key: invocation.metadata.idempotency_key.as_deref(),
        })?;
        let control = runtime_control(invocation.metadata.deadline.as_deref(), cancellation)?;
        control.check(ErrorPhase::Validate, false)?;

        let result = match descriptor.operation {
            "storage.test" => {
                let input: TestInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                self.authorize_connection(&input.connection)?;
                serialize_result(self.engine.test(&input.connection, &control).await?)?
            }
            "storage.list" => {
                let input: ListInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                self.authorize_connection(&input.connection)?;
                serialize_result(
                    self.engine
                        .list(&input.connection, &input.request, &control)
                        .await?,
                )?
            }
            "storage.stat" => {
                let input: StatInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                self.authorize_connection(&input.connection)?;
                serialize_result(
                    self.engine
                        .stat(&input.connection, &input.request, &control)
                        .await?,
                )?
            }
            "storage.get" => {
                let input: GetInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                input.artifact_sink.validate()?;
                self.authorize_connection(&input.connection)?;
                // Opening a sink can create or truncate a consumer-owned
                // destination, so every local admission check runs first.
                self.engine
                    .preflight(&input.connection, &[&input.request.key])?;
                // Checked before the call so that a request cancelled while
                // still local reports no effect, while a cancellation during
                // the open reports the ambiguous one the resolver may have
                // produced.
                control.check(ErrorPhase::Prepare, false)?;
                let mut sink = control
                    .run(
                        self.artifacts.open_sink(&input.artifact_sink),
                        ErrorPhase::Prepare,
                        true,
                    )
                    .await?;
                let result = self
                    .engine
                    .get(&input.connection, &input.request, &mut sink, &control)
                    .await
                    // The provider only knows about its own effects. Opening
                    // the artifact may already have created or truncated it,
                    // even if the provider fails before writing any bytes.
                    .map_err(|error| {
                        error
                            .with_outcome(RemoteEffect::Unknown, RetryDisposition::RequiresRecovery)
                    })?;
                // Integrity is checked before the sink is finalized: a sink that
                // publishes on shutdown must not commit bytes that do not match
                // the declared metadata. The sink already holds bytes, so the
                // published state of the artifact is ambiguous.
                validate_transfer_metadata(
                    &input.artifact_sink.metadata,
                    &result,
                    RemoteEffect::Unknown,
                )?;
                control
                    .run(
                        async { sink.shutdown().await.map_err(|_| artifact_finalize_error()) },
                        ErrorPhase::Commit,
                        true,
                    )
                    .await?;
                serialize_result(result)?
            }
            "storage.put" => {
                let mut input: PutInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                input.artifact_source.validate()?;
                apply_put_metadata(&mut input)?;
                self.authorize_connection(&input.connection)?;
                self.engine
                    .preflight(&input.connection, &[&input.request.key])?;
                let mut source = control
                    .run(
                        self.artifacts.open_source(&input.artifact_source),
                        ErrorPhase::Prepare,
                        false,
                    )
                    .await?;
                let result = self
                    .engine
                    .put(&input.connection, &input.request, &mut source, &control)
                    .await?;
                // The upload succeeded, so the object is committed with content
                // that does not match the declared metadata. Reporting an
                // ambiguous outcome here would understate a known one.
                validate_transfer_metadata(
                    &input.artifact_source.metadata,
                    &result,
                    RemoteEffect::Committed,
                )?;
                serialize_result(result)?
            }
            "storage.copy" => {
                let input: CopyInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                self.authorize_connection(&input.connection)?;
                serialize_result(
                    self.engine
                        .copy(&input.connection, &input.request, &control)
                        .await?,
                )?
            }
            "storage.delete" => {
                let input: DeleteInput = decode_input(&invocation.payload)?;
                validate_operation_schema_version(input.schema_version)?;
                self.authorize_connection(&input.connection)?;
                serialize_result(
                    self.engine
                        .delete(&input.connection, &input.request, &control)
                        .await?,
                )?
            }
            _ => return Err(route_error("runtime storage operation is unsupported")),
        };
        Ok((descriptor, result))
    }

    /// Validates the whole public connection before the application's secret
    /// authority is consulted, so a malformed or secret-bearing connection never
    /// reaches it.
    fn authorize_connection(&self, connection: &crate::ProviderConnection) -> StorageResult<()> {
        connection.validate()?;
        self.secrets.authorize(&connection.credential_ref)
    }
}

fn decode_input<T: DeserializeOwned>(payload: &Value) -> StorageResult<T> {
    serde_json::from_value(payload.clone()).map_err(|_| {
        StorageError::invalid_configuration(
            "RUNTIME_PAYLOAD_INVALID",
            "runtime payload does not match the declared storage input contract",
        )
    })
}

fn serialize_result<T: Serialize>(result: T) -> StorageResult<Value> {
    serde_json::to_value(result).map_err(|_| {
        StorageError::new(
            ErrorCategory::Internal,
            ErrorPhase::Cleanup,
            RemoteEffect::None,
            RetryDisposition::Never,
            "RUNTIME_RESULT_SERIALIZATION_FAILED",
            "storage runtime result serialization failed",
        )
    })
}

fn parse_version(value: &str) -> StorageResult<u32> {
    value
        .parse()
        .map_err(|_| route_error("runtime version is invalid"))
}

fn runtime_control(
    deadline: Option<&str>,
    cancellation: CancellationToken,
) -> StorageResult<ExecutionControl> {
    let mut control = ExecutionControl::new(cancellation);
    if let Some(deadline) = deadline {
        let parsed = OffsetDateTime::parse(deadline, &Rfc3339).map_err(|_| {
            StorageError::invalid_configuration(
                "RUNTIME_DEADLINE_INVALID",
                "runtime deadline must be an RFC 3339 timestamp",
            )
        })?;
        let now = OffsetDateTime::now_utc();
        let instant = if parsed <= now {
            Instant::now()
        } else {
            Instant::now()
                + std::time::Duration::try_from(parsed - now).map_err(|_| {
                    StorageError::invalid_configuration(
                        "RUNTIME_DEADLINE_INVALID",
                        "runtime deadline is outside the supported range",
                    )
                })?
        };
        control = control.with_deadline(instant);
    }
    Ok(control)
}

fn validate_payload_security(payload: &Value) -> StorageResult<()> {
    fn visit(key: Option<&str>, value: &Value) -> bool {
        match value {
            Value::Object(object) => object.iter().any(|(child_key, child)| {
                crate::is_secret_field_name(child_key) || visit(Some(child_key), child)
            }),
            Value::Array(items) => items.iter().any(|child| visit(key, child)),
            Value::String(text) => {
                key.is_some_and(|field| field == "reference") && is_local_path(text)
            }
            _ => false,
        }
    }
    if visit(None, payload) {
        return Err(StorageError::invalid_configuration(
            "RUNTIME_PAYLOAD_SECURITY_VIOLATION",
            "runtime payload contains inline credentials or a private local path",
        ));
    }
    Ok(())
}

fn is_local_path(value: &str) -> bool {
    let bytes = value.as_bytes();
    (bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\'))
        || value.starts_with(['/', '\\'])
        || value.to_ascii_lowercase().starts_with("file:")
        || value
            .replace('\\', "/")
            .split('/')
            .any(|segment| segment == "..")
}

fn apply_put_metadata(input: &mut PutInput) -> StorageResult<()> {
    let metadata = &input.artifact_source.metadata;
    if input
        .request
        .content_type
        .as_ref()
        .zip(metadata.content_type.as_ref())
        .is_some_and(|(request, artifact)| request != artifact)
        || input
            .request
            .content_length
            .zip(metadata.size)
            .is_some_and(|(request, artifact)| request != artifact)
    {
        return Err(StorageError::invalid_configuration(
            "ARTIFACT_METADATA_MISMATCH",
            "artifact source metadata conflicts with the storage put request",
        ));
    }
    if input.request.content_type.is_none() {
        input
            .request
            .content_type
            .clone_from(&metadata.content_type);
    }
    if input.request.content_length.is_none() {
        input.request.content_length = metadata.size;
    }
    Ok(())
}

fn validate_transfer_metadata(
    expected: &crate::ArtifactMetadata,
    result: &TransferResult,
    remote_effect: RemoteEffect,
) -> StorageResult<()> {
    let mismatch = expected
        .size
        .is_some_and(|size| size != result.bytes_transferred)
        || expected
            .sha256
            .as_ref()
            .is_some_and(|sha256| sha256 != &result.checksum.value)
        || expected
            .content_type
            .as_ref()
            .zip(result.artifact.content_type.as_ref())
            .is_some_and(|(expected, actual)| expected != actual);
    if mismatch {
        return Err(StorageError::new(
            ErrorCategory::Conflict,
            ErrorPhase::Commit,
            remote_effect,
            if remote_effect == RemoteEffect::None {
                RetryDisposition::Never
            } else {
                RetryDisposition::RequiresRecovery
            },
            "ARTIFACT_INTEGRITY_MISMATCH",
            "artifact size, content type or SHA-256 differs from declared metadata",
        ));
    }
    Ok(())
}

fn artifact_finalize_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Io,
        ErrorPhase::Commit,
        RemoteEffect::Unknown,
        RetryDisposition::RequiresRecovery,
        "ARTIFACT_SINK_FINALIZE_FAILED",
        "artifact sink finalization failed with an ambiguous publication outcome",
    )
}

/// Validates routing before a consumer-owned adapter resolves credentials or
/// artifacts and before any storage operation can start.
pub fn validate_runtime_route(
    route: RuntimeRoute<'_>,
) -> StorageResult<&'static RuntimeOperationDescriptor> {
    if route.capability_name != CAPABILITY_NAME
        || route.capability_version != RUNTIME_BINDING_VERSION
    {
        return Err(route_error("runtime capability identity is unsupported"));
    }
    let descriptor = RUNTIME_OPERATIONS
        .iter()
        .find(|candidate| candidate.operation == route.operation)
        .ok_or_else(|| route_error("runtime storage operation is unsupported"))?;
    let version_matches = route.operation_version == descriptor.version;
    let contract_matches = route.input_contract == descriptor.input_contract;
    let content_type_matches = route.content_type == descriptor.content_type;
    if !(version_matches && contract_matches && content_type_matches) {
        return Err(route_error(
            "runtime operation version, input contract or content type is unsupported",
        ));
    }
    if route.idempotency_key.is_some() {
        return Err(route_error(
            "storage v1 operations do not accept idempotency keys",
        ));
    }
    Ok(descriptor)
}

fn route_error(message: &'static str) -> StorageError {
    StorageError::new(
        ErrorCategory::Protocol,
        ErrorPhase::Validate,
        RemoteEffect::None,
        RetryDisposition::Never,
        "RUNTIME_ROUTE_INVALID",
        message,
    )
}

fn canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}

fn public_uuid(value: &str) -> String {
    if canonical_uuid(value) {
        value.to_owned()
    } else {
        "00000000-0000-0000-0000-000000000000".to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_routes_fail_closed_before_invocation() {
        let valid = RuntimeRoute {
            capability_name: CAPABILITY_NAME,
            capability_version: 1,
            operation: "storage.put",
            operation_version: 1,
            input_contract: "plenora-storage-put-input-v1",
            content_type: JSON_CONTENT_TYPE,
            idempotency_key: None,
        };
        assert_eq!(
            validate_runtime_route(valid).map(|item| item.artifact_role),
            Ok(ArtifactRole::Source)
        );
        assert!(
            validate_runtime_route(RuntimeRoute {
                idempotency_key: Some("unsupported"),
                ..valid
            })
            .is_err()
        );
        assert!(
            validate_runtime_route(RuntimeRoute {
                input_contract: "plenora-storage-put-input-v2",
                ..valid
            })
            .is_err()
        );
    }
}
