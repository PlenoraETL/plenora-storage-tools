//! Runtime Binding 1.0 admission: reserved key grammars, rejection
//! categories, deadline control and result identity, applied before any
//! resolver or provider runs.

use std::{
    pin::Pin,
    task::{Context, Poll},
    time::Instant,
};

use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tokio::io::AsyncWrite;

use crate::{
    CAPABILITY_NAME, CancellationToken, ErrorCategory, ErrorPhase, ExecutionControl,
    RUNTIME_BINDING_VERSION, RUNTIME_OPERATIONS, RemoteEffect, RetryDisposition,
    RuntimeOperationDescriptor, RuntimeRequestMetadata, RuntimeResultMetadata, RuntimeRoute,
    StorageError, StorageResult, runtime::ERROR_CONTRACT,
};

/// Accepts only the canonical decimal form (`^[1-9][0-9]*$` in the runtime
/// vector schema). `u32::from_str` alone also accepts `+1` and `01`, which
/// would admit a selector that differs textually from the descriptor.
/// A canonical version too large for `u32` is well formed and unannounced.
pub fn parse_version(value: &str) -> StorageResult<u32> {
    if !canonical_version(value) {
        return Err(route_error("runtime version is invalid"));
    }
    value
        .parse()
        .map_err(|_| route_rejection(true, "runtime version is unsupported"))
}

pub fn runtime_control(
    deadline: Option<&str>,
    cancellation: CancellationToken,
) -> StorageResult<ExecutionControl> {
    let mut control = ExecutionControl::new(cancellation);
    if let Some(deadline) = deadline {
        let parsed = parse_deadline(deadline)?;
        let now = OffsetDateTime::now_utc();
        let instant = if parsed <= now {
            Instant::now()
        } else {
            std::time::Duration::try_from(parsed - now)
                .ok()
                .and_then(|remaining| Instant::now().checked_add(remaining))
                .ok_or_else(|| {
                    StorageError::new(
                        ErrorCategory::Unsupported,
                        ErrorPhase::Validate,
                        RemoteEffect::None,
                        RetryDisposition::Never,
                        "RUNTIME_DEADLINE_UNSUPPORTED",
                        "runtime deadline is outside the supported range",
                    )
                })?
        };
        control = control.with_deadline(instant);
    }
    Ok(control)
}

/// Runtime Binding 1.0 §4 and RT-021: the deadline is an absolute RFC 3339
/// UTC timestamp. Every RFC 3339 spelling of UTC is accepted (`Z` or `z`,
/// `+00:00`, `T` or `t`, fractions of a second); a non-zero offset and `-00:00`
/// (an unknown local offset) are `protocol` rejections, never reinterpreted.
/// Whether a receiver accepts the UTC spellings other than `Z` is left open by
/// 1.0; accepting them is the common choice pending ratification.
fn parse_deadline(value: &str) -> StorageResult<OffsetDateTime> {
    let invalid = || route_error("runtime deadline must be an RFC 3339 UTC timestamp");
    if !value.is_ascii() {
        return Err(invalid());
    }
    // Only the `T` separator and the `Z` designator are case-insensitive in
    // RFC 3339; the remaining characters are digits and punctuation.
    let spelled = value.to_ascii_uppercase();
    // The parser also takes a space for `T`, which RFC 3339 allows only by
    // mutual agreement outside its grammar.
    let separator = spelled.as_bytes().get(10) == Some(&b'T');
    if !separator || !(spelled.ends_with('Z') || spelled.ends_with("+00:00")) {
        return Err(invalid());
    }
    OffsetDateTime::parse(&spelled, &Rfc3339)
        .ok()
        .filter(|parsed| parsed.offset().is_utc())
        .ok_or_else(invalid)
}

/// Validates routing before a consumer-owned adapter resolves credentials or
/// artifacts and before any storage operation can start.
///
/// # Errors
/// Returns a validation error for unknown selectors, versions, input contract, content type or unsupported idempotency control.
pub fn validate_runtime_route(
    route: RuntimeRoute<'_>,
) -> StorageResult<&'static RuntimeOperationDescriptor> {
    // Rule R1 of the common runtime matrix (pending ratification in
    // `plenora-contracts`): a well-formed value that no advertised operation
    // carries is `unsupported`; an absent, malformed or non-canonical one is
    // `protocol`. Versions reach this point already canonical.
    if route.capability_name != CAPABILITY_NAME {
        return Err(route_rejection(
            is_capability_name(route.capability_name),
            "runtime capability identity is unsupported",
        ));
    }
    if route.capability_version != RUNTIME_BINDING_VERSION {
        return Err(route_rejection(
            true,
            "runtime binding version is unsupported",
        ));
    }
    let descriptor = RUNTIME_OPERATIONS
        .iter()
        .find(|candidate| candidate.operation == route.operation)
        .ok_or_else(|| {
            route_rejection(
                is_operation_selector(route.operation),
                "runtime storage operation is unsupported",
            )
        })?;
    if route.operation_version != descriptor.version {
        return Err(route_rejection(
            true,
            "runtime operation version is unsupported",
        ));
    }
    if route.input_contract != descriptor.input_contract {
        return Err(route_rejection(
            is_contract_id(route.input_contract),
            "runtime input contract is unsupported",
        ));
    }
    if route.content_type != descriptor.content_type {
        return Err(route_rejection(
            is_media_type(route.content_type),
            "runtime content type is unsupported",
        ));
    }
    // RT-006: a control the descriptor declares unsupported is never accepted
    // silently. No storage v1 operation supports idempotency keys.
    match route.idempotency_key {
        Some("") => Err(route_error("runtime idempotency key is empty")),
        Some(_) => Err(StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "RUNTIME_CONTROL_UNSUPPORTED",
            "storage v1 operations do not accept idempotency keys",
        )),
        None => Ok(descriptor),
    }
}

/// A rejection before invocation: `validate`, no effect, never retried.
fn route_rejection(well_formed: bool, message: &'static str) -> StorageError {
    if well_formed {
        StorageError::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "RUNTIME_ROUTE_UNSUPPORTED",
            message,
        )
    } else {
        route_error(message)
    }
}

pub fn route_error(message: &'static str) -> StorageError {
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

/// RT-020: a new message identity for a result, a version 4 UUID (RFC 9562)
/// from the operating system's random source. It never depends on the request,
/// so two requests, even without a usable identity, never share a result
/// identity.
///
/// # Errors
/// An unavailable random source is an `internal` error before invocation: no
/// result can be identified, and nothing has started.
pub fn new_result_message_id() -> StorageResult<String> {
    result_message_id_from(getrandom::fill)
}

fn result_message_id_from(
    fill: impl FnOnce(&mut [u8]) -> Result<(), getrandom::Error>,
) -> StorageResult<String> {
    let mut bytes = [0_u8; 16];
    fill(&mut bytes).map_err(|_| {
        StorageError::new(
            ErrorCategory::Internal,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "RUNTIME_RESULT_IDENTITY_UNAVAILABLE",
            "the random source for a runtime result identity is unavailable",
        )
    })?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = hex::encode(bytes);
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

/// `^[1-9][0-9]*$`, the version grammar of the runtime vector schema.
fn canonical_version(value: &str) -> bool {
    !value.is_empty() && !value.starts_with('0') && value.bytes().all(|byte| byte.is_ascii_digit())
}

/// `^plenora\.[a-z][a-z0-9-]*-tools$`.
fn is_capability_name(value: &str) -> bool {
    value.strip_prefix("plenora.").is_some_and(|domain| {
        domain.len() > "-tools".len()
            && domain.ends_with("-tools")
            && domain.as_bytes()[0].is_ascii_lowercase()
            && domain
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    })
}

/// `^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$`.
fn is_operation_selector(value: &str) -> bool {
    value.contains('.')
        && value.split('.').all(|segment| {
            segment
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_lowercase)
                && segment.bytes().all(|byte| {
                    byte.is_ascii_lowercase()
                        || byte.is_ascii_digit()
                        || matches!(byte, b'_' | b'-')
                })
        })
}

/// `^plenora-[a-z0-9-]+-v[1-9][0-9]*$`.
fn is_contract_id(value: &str) -> bool {
    value
        .strip_prefix("plenora-")
        .and_then(|rest| rest.rsplit_once("-v"))
        .is_some_and(|(name, version)| {
            !name.is_empty()
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
                && canonical_version(version)
        })
}

/// A lowercase `type/subtype` media type without parameters.
fn is_media_type(value: &str) -> bool {
    let token = |part: &str| {
        !part.is_empty()
            && part.bytes().all(|byte| {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(
                        byte,
                        b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
                    )
            })
    };
    value
        .split_once('/')
        .is_some_and(|(kind, subtype)| token(kind) && token(subtype))
}

/// Sink wrapper that records the bytes the sink accepted and whether the sink
/// itself failed, so a failed get can state what reached the sink.
pub struct CountingSink<W> {
    pub inner: W,
    pub delivered: u64,
    pub failed: bool,
}

impl<W> CountingSink<W> {
    pub const fn new(inner: W) -> Self {
        Self {
            inner,
            delivered: 0,
            failed: false,
        }
    }

    /// A get whose sink accepted part of the transfer and then failed is
    /// `STORAGE_GET_SINK_PARTIAL` with `remote_effect: partial` and retry
    /// `never`, whatever code the provider gave the write failure. Category
    /// and phase stay those of the failure, so a full disk remains a
    /// `resource_limit` and an I/O failure an `io` in `write`.
    pub fn restate(&self, error: StorageError, provider: &str) -> StorageError {
        if !(self.failed && self.delivered > 0) {
            return error;
        }
        StorageError::new(
            error.category,
            error.phase,
            RemoteEffect::Partial,
            RetryDisposition::Never,
            "STORAGE_GET_SINK_PARTIAL",
            "The artifact sink received a partial transfer; automatic retry is not permitted.",
        )
        .with_provider(provider)
    }
}

impl<W: AsyncWrite + Unpin> AsyncWrite for CountingSink<W> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        let polled = Pin::new(&mut self.inner).poll_write(context, bytes);
        match &polled {
            Poll::Ready(Ok(written)) => {
                self.delivered = self.delivered.saturating_add(*written as u64);
            }
            Poll::Ready(Err(_)) => self.failed = true,
            Poll::Pending => {}
        }
        polled
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<std::io::Result<()>> {
        let polled = Pin::new(&mut self.inner).poll_flush(context);
        if matches!(polled, Poll::Ready(Err(_))) {
            self.failed = true;
        }
        polled
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(context)
    }
}

/// The outcome of a get that failed after the host sink was opened. A get
/// does not mutate the remote object, so the effect is the sink's: once bytes
/// were delivered, part of the transfer is known to have reached it and is
/// not finalized (`partial`, never retried automatically, as the
/// `storage-get-partial-error` vector requires). Before any byte, opening the
/// sink may already have created or truncated it, which cannot be proved
/// either way (`unknown`, `requires_recovery`).
pub fn sink_failure_outcome(error: StorageError, delivered: u64) -> StorageError {
    if delivered > 0 {
        error.with_outcome(RemoteEffect::Partial, RetryDisposition::Never)
    } else {
        error.with_outcome(RemoteEffect::Unknown, RetryDisposition::RequiresRecovery)
    }
}

/// RT-017: every reserved request key matches its grammar. A malformed value
/// is a `protocol` rejection and is never normalized.
pub fn validate_grammar(
    metadata: &RuntimeRequestMetadata,
    content_type: &str,
) -> StorageResult<()> {
    if !canonical_uuid(&metadata.message_id)
        || !canonical_uuid(&metadata.correlation_id)
        || metadata
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
    let routing = is_capability_name(&metadata.capability_name)
        && canonical_version(&metadata.capability_version)
        && is_operation_selector(&metadata.operation)
        && canonical_version(&metadata.operation_version)
        && is_contract_id(&metadata.input_contract)
        && is_media_type(content_type);
    if !routing {
        return Err(route_error("runtime routing metadata is malformed"));
    }
    if metadata.idempotency_key.as_deref() == Some("") {
        return Err(route_error("runtime idempotency key is empty"));
    }
    if let Some(deadline) = metadata.deadline.as_deref() {
        parse_deadline(deadline)?;
    }
    Ok(())
}

/// RT-019 and RT-020: the identity of a result. The message identity is new,
/// the causation is the request's message identity when canonical, and the
/// operation, operation version and correlation are copied byte for byte only
/// when well-formed, and omitted otherwise.
pub fn result_identity(
    result_message_id: String,
    message_id: Option<&str>,
    operation: Option<&str>,
    operation_version: Option<&str>,
    correlation_id: Option<&str>,
) -> RuntimeResultMetadata {
    let keep = |value: Option<&str>, valid: fn(&str) -> bool| {
        value.filter(|value| valid(value)).map(str::to_owned)
    };
    RuntimeResultMetadata {
        message_id: result_message_id,
        causation_id: keep(message_id, canonical_uuid),
        operation: keep(operation, is_operation_selector),
        operation_version: keep(operation_version, canonical_version),
        output_contract: ERROR_CONTRACT.to_owned(),
        correlation_id: keep(correlation_id, canonical_uuid),
    }
}

#[cfg(test)]
#[path = "runtime_admission_tests.rs"]
mod tests;
