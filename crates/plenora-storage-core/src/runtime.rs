//! Transport-neutral Runtime Binding 1.0 boundary. A final application can
//! wrap this type in its runtime `CapabilityHandler`; this module deliberately
//! does not depend on `runtime-tools`.

use std::pin::Pin;

use async_trait::async_trait;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};

use crate::runtime_admission::{
    CountingSink, parse_version, result_identity, route_error, runtime_control,
    sink_failure_outcome, validate_grammar, validate_runtime_route,
};
use crate::{
    ArtifactReference, ArtifactSinkReference, CancellationToken, CopyInput, DeleteInput, Engine,
    ErrorCategory, ErrorPhase, GetInput, ListInput, PutInput, RemoteEffect, RetryDisposition,
    SideEffect, StatInput, StorageError, StorageResult, TestInput, TransferResult,
    model::present_value, validate_operation_schema_version,
};

/// Supported transport-neutral runtime binding version.
pub const RUNTIME_BINDING_VERSION: u32 = 1;
/// Media type for JSON operation envelopes.
pub const JSON_CONTENT_TYPE: &str = "application/json";
/// Media type for a terminal Plenora error envelope.
pub const ERROR_CONTENT_TYPE: &str = "application/vnd.plenora.error+json";
/// Versioned public error contract used by runtime dispatch.
pub const ERROR_CONTRACT: &str = "plenora-error-v1";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Artifact stream direction required by an operation.
pub enum ArtifactRole {
    /// No artifact stream is required.
    None,
    /// Resolve an input byte stream before upload.
    Source,
    /// Resolve an output byte stream before download.
    Sink,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Static dispatch contract and controls for one runtime operation.
pub struct RuntimeOperationDescriptor {
    /// Stable storage operation selector.
    pub operation: &'static str,
    /// Version of the interface or operation contract.
    pub version: u32,
    /// Versioned request contract required for the selected operation.
    pub input_contract: &'static str,
    /// Versioned response contract, or the error contract on failure.
    pub output_contract: &'static str,
    /// Optional media type without parameters, validated before transfer.
    pub content_type: &'static str,
    /// Whether execution can change external state.
    pub side_effect: SideEffect,
    /// Whether dispatch resolves an input stream, an output stream, or neither.
    pub artifact_role: ArtifactRole,
    /// Cooperative cancellation support.
    pub cancellation: bool,
    /// Deadline support.
    pub deadline: bool,
    /// Idempotency key support; false means the control must be rejected.
    pub idempotency_key: bool,
}

/// Complete static runtime operation inventory used for admission and routing.
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
/// Borrowed dispatch selectors validated before payload execution.
pub struct RuntimeRoute<'a> {
    /// Storage capability selector; checked before opening artifacts.
    pub capability_name: &'a str,
    /// Required version of the runtime storage capability.
    pub capability_version: u32,
    /// Stable storage operation selector.
    pub operation: &'a str,
    /// Required version of the selected operation.
    pub operation_version: u32,
    /// Versioned request contract required for the selected operation.
    pub input_contract: &'a str,
    /// Optional media type without parameters, validated before transfer.
    pub content_type: &'a str,
    /// Optional host idempotency key; currently rejected because these operations do not support it.
    pub idempotency_key: Option<&'a str>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Transport-neutral invocation envelope with no inline file contents.
pub struct RuntimeInvocation {
    /// Optional media type without parameters, validated before transfer.
    pub content_type: String,
    /// Operation metadata validated against the corresponding public contract.
    pub metadata: RuntimeRequestMetadata,
    /// Operation JSON envelope; file contents are carried only by artifact streams.
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
/// Identity, routing and execution controls from the host message.
///
/// Keys this binding version does not reserve are ignored, as Runtime Binding
/// 1.0 §9 treats added optional metadata; a renamed reserved key is therefore
/// not an alias of the reserved one.
pub struct RuntimeRequestMetadata {
    #[serde(rename = "plenora.message.id")]
    /// Canonical UUID identifying the invocation or result message.
    pub message_id: String,
    #[serde(
        rename = "plenora.message.causation_id",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// Optional canonical UUID linking this message to its cause; omitted when
    /// absent, and `null` is rejected rather than read as absent.
    pub causation_id: Option<String>,
    #[serde(rename = "plenora.capability.name")]
    /// Storage capability selector; checked before opening artifacts.
    pub capability_name: String,
    #[serde(rename = "plenora.capability.version")]
    /// Required version of the runtime storage capability.
    pub capability_version: String,
    #[serde(rename = "plenora.capability.operation")]
    /// Stable storage operation selector.
    pub operation: String,
    #[serde(rename = "plenora.operation.version")]
    /// Required version of the selected operation.
    pub operation_version: String,
    #[serde(rename = "plenora.input.contract")]
    /// Versioned request contract required for the selected operation.
    pub input_contract: String,
    #[serde(
        rename = "plenora.execution.deadline",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// Optional RFC 3339 deadline, translated into execution control before dispatch.
    /// `null` is rejected: reading it as absent would start without a deadline.
    pub deadline: Option<String>,
    #[serde(
        rename = "plenora.execution.idempotency_key",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// Optional host idempotency key (Runtime Binding 1.0 §4). No storage v1
    /// operation supports it, so a present key is rejected as `unsupported`
    /// (RT-006); `null` is rejected rather than read as absent.
    pub idempotency_key: Option<String>,
    #[serde(rename = "plenora.trace.correlation_id")]
    /// Canonical UUID retained across invocation and terminal result.
    pub correlation_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Terminal operation or redacted error envelope with preserved correlation.
pub struct RuntimeResultEnvelope {
    /// Optional media type without parameters, validated before transfer.
    pub content_type: String,
    /// Operation metadata validated against the corresponding public contract.
    pub metadata: RuntimeResultMetadata,
    /// Operation JSON envelope; file contents are carried only by artifact streams.
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
/// Identity and contract of the terminal runtime result.
///
/// The message identity is always new and the causation is the request's
/// message identity (RT-012). Operation, operation version and correlation are
/// copied byte for byte from the request only when canonical, and omitted
/// otherwise: a rejection never reflects, normalizes or invents a routing
/// value (rule R2 of the common runtime matrix, pending ratification in
/// `plenora-contracts`). A success always carries all three.
pub struct RuntimeResultMetadata {
    #[serde(rename = "plenora.message.id")]
    /// New canonical UUID identifying this result message.
    pub message_id: String,
    #[serde(
        rename = "plenora.message.causation_id",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// The request's message identity when canonical; omitted otherwise, and
    /// `null` is rejected rather than read as absent.
    pub causation_id: Option<String>,
    #[serde(
        rename = "plenora.capability.operation",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// Stable storage operation selector; omitted when the request's is not canonical.
    pub operation: Option<String>,
    #[serde(
        rename = "plenora.operation.version",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// Version of the selected operation; omitted when the request's is not canonical.
    pub operation_version: Option<String>,
    #[serde(rename = "plenora.output.contract")]
    /// Versioned response contract, or the error contract on failure.
    pub output_contract: String,
    #[serde(
        rename = "plenora.trace.correlation_id",
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    /// The request's correlation UUID; omitted when it is not canonical.
    pub correlation_id: Option<String>,
}

/// Host-owned asynchronous source stream for an artifact reference.
pub type ArtifactSource = Pin<Box<dyn AsyncRead + Send + Unpin>>;
/// Host-owned asynchronous destination stream; publication belongs to its resolver.
pub type ArtifactSink = Pin<Box<dyn AsyncWrite + Send + Unpin>>;

#[async_trait]
/// Application boundary that authorizes and opens artifact streams.
pub trait ArtifactResolver: Send + Sync {
    /// Authorize and open a source owned by the host.
    ///
    /// # Errors
    /// Returns a redacted reference, authorization or open failure; never include raw callback exceptions.
    async fn open_source(&self, source: &ArtifactReference) -> StorageResult<ArtifactSource>;
    /// Authorize and open a host-owned sink; opening may already mutate the destination.
    ///
    /// # Errors
    /// Returns redacted authorization or open failures with the actual or unknown external effect.
    async fn open_sink(&self, sink: &ArtifactSinkReference) -> StorageResult<ArtifactSink>;
}

/// Application-owned authorization for protected secret references. Provider
/// adapters still receive secret material through their `CredentialResolver`;
///
/// a consumer normally backs both traits with the same secret authority.
pub trait SecretResolver: Send + Sync {
    ///
    /// # Errors
    /// Returns a redacted authorization failure when the host refuses access to the protected reference.
    fn authorize(&self, reference: &str) -> StorageResult<()>;
}

/// Borrowed engine and host resolvers implementing transport-neutral dispatch.
pub struct RuntimeBinding<'a> {
    engine: &'a Engine,
    artifacts: &'a dyn ArtifactResolver,
    secrets: &'a dyn SecretResolver,
}

impl<'a> RuntimeBinding<'a> {
    /// Bind an engine and host resolvers without opening resources or resolving secrets.
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

    /// Validate and dispatch a serialized invocation exactly as a transport
    /// received it. Unlike deserializing [`RuntimeInvocation`] first, every
    /// request produces a result envelope: a missing reserved key, a value
    /// that is not a JSON string or a `null` control is a `protocol` rejection
    /// before invocation (RT-016, RT-017), whose metadata reflects only the
    /// well-formed routing values the request carried (RT-019).
    pub async fn invoke_json(
        &self,
        invocation: Value,
        cancellation: CancellationToken,
    ) -> RuntimeResultEnvelope {
        let metadata = invocation.get("metadata");
        let text = |key: &str| {
            metadata
                .and_then(|metadata| metadata.get(key))
                .and_then(Value::as_str)
        };
        let identity = result_identity(
            text("plenora.message.id"),
            text("plenora.capability.operation"),
            text("plenora.operation.version"),
            text("plenora.trace.correlation_id"),
        );
        match serde_json::from_value::<RuntimeInvocation>(invocation) {
            Ok(invocation) => self.invoke(invocation, cancellation).await,
            Err(_) => error_envelope(
                identity,
                &StorageError::new(
                    ErrorCategory::Protocol,
                    ErrorPhase::Validate,
                    RemoteEffect::None,
                    RetryDisposition::Never,
                    "RUNTIME_ENVELOPE_INVALID",
                    "runtime envelope lacks a reserved key or carries a non-string value",
                ),
            ),
        }
    }

    /// Validate and dispatch an invocation, returning a correlated success or redacted error envelope.
    pub async fn invoke(
        &self,
        invocation: RuntimeInvocation,
        cancellation: CancellationToken,
    ) -> RuntimeResultEnvelope {
        let request = &invocation.metadata;
        let identity = result_identity(
            Some(&request.message_id),
            Some(&request.operation),
            Some(&request.operation_version),
            Some(&request.correlation_id),
        );
        match self.invoke_inner(&invocation, cancellation).await {
            Ok((descriptor, payload)) => RuntimeResultEnvelope {
                content_type: descriptor.content_type.to_owned(),
                metadata: RuntimeResultMetadata {
                    output_contract: descriptor.output_contract.to_owned(),
                    ..identity
                },
                payload,
            },
            Err(error) => error_envelope(identity, &error),
        }
    }

    #[allow(
        clippy::too_many_lines,
        reason = "Keep the seven dispatch arms together to audit admission before artifact effects"
    )]
    async fn invoke_inner(
        &self,
        invocation: &RuntimeInvocation,
        cancellation: CancellationToken,
    ) -> StorageResult<(&'static RuntimeOperationDescriptor, Value)> {
        // RT-018: `protocol` for any malformed reserved value, then
        // `unsupported` for a well-formed one nothing advertises, then
        // `timeout` for a deadline already elapsed; all before invocation.
        validate_grammar(&invocation.metadata, &invocation.content_type)?;
        let descriptor = validate_runtime_route(RuntimeRoute {
            capability_name: &invocation.metadata.capability_name,
            capability_version: parse_version(&invocation.metadata.capability_version)?,
            operation: &invocation.metadata.operation,
            operation_version: parse_version(&invocation.metadata.operation_version)?,
            input_contract: &invocation.metadata.input_contract,
            content_type: &invocation.content_type,
            idempotency_key: invocation.metadata.idempotency_key.as_deref(),
        })?;
        let control = runtime_control(invocation.metadata.deadline.as_deref(), cancellation)?;
        // An expired deadline is `timeout`/`validate`/`none`/`never` here:
        // nothing has started, and the same message would expire again.
        control.check(ErrorPhase::Validate, false)?;
        validate_payload_security(&invocation.payload)?;

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
                let mut sink = CountingSink {
                    inner: control
                        .run(
                            self.artifacts.open_sink(&input.artifact_sink),
                            ErrorPhase::Prepare,
                            true,
                        )
                        .await?,
                    delivered: 0,
                };
                let result = self
                    .engine
                    .get(&input.connection, &input.request, &mut sink, &control)
                    .await
                    .map_err(|error| sink_failure_outcome(error, sink.delivered))?;
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

fn error_envelope(metadata: RuntimeResultMetadata, error: &StorageError) -> RuntimeResultEnvelope {
    RuntimeResultEnvelope {
        content_type: ERROR_CONTENT_TYPE.to_owned(),
        metadata,
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
    }
}

#[cfg(test)]
#[path = "runtime_tests.rs"]
mod tests;
