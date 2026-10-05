use std::{collections::BTreeMap, fmt};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Result whose error includes the phase, remote effect and retry disposition.
pub type StorageResult<T> = Result<T, StorageError>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Failure cause; interpret together with phase, external effect and retry.
pub enum ErrorCategory {
    /// Invalid request, connection or engine policy.
    InvalidConfiguration,
    /// Requested guarantee or operation cannot be provided.
    Unsupported,
    /// Requested object does not exist.
    NotFound,
    /// Destination or precondition conflicts with current state.
    Conflict,
    /// Credentials or peer identity could not be authenticated.
    Authentication,
    /// Authenticated principal lacks permission for the operation.
    Authorization,
    /// The operation exceeded its deadline.
    Timeout,
    /// The caller requested cancellation.
    Cancelled,
    /// A declared size, buffer or enumeration budget was exceeded.
    ResourceLimit,
    /// Transport, source, sink or filesystem I/O failed.
    Io,
    /// The peer returned an invalid or unexpected protocol response.
    Protocol,
    /// Potentially temporary service failure; still inspect effect and retry.
    Transient,
    /// Operation failed outside a more specific category.
    Execution,
    /// An internal invariant could not be satisfied.
    Internal,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
/// Phase reached when an operation failed.
pub enum ErrorPhase {
    /// Local request and policy admission before effects.
    Validate,
    /// Address validation, authentication or connection setup.
    Connect,
    /// Backend reachability probe.
    Probe,
    /// Preparing handles, directories or staging resources.
    Prepare,
    /// Reading source bytes or provider metadata.
    Read,
    /// Writing bytes before publication completes.
    Write,
    /// Publishing or deleting the destination.
    Commit,
    /// Final verification or cleanup after transfer/publication.
    Cleanup,
}

/// What can be proved about a mutation, independently of its failure category.
/// A timeout or cancellation alone never implies `None` or `RolledBack`.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RemoteEffect {
    /// No external effect has occurred.
    None,
    /// All effects covered by this result were confirmed undone.
    RolledBack,
    /// Some effects are known to remain without full completion.
    Partial,
    /// Publication is confirmed, even if later verification or cleanup failed.
    Committed,
    /// The remote outcome cannot be proved; do not infer rollback from lost responses.
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
/// Action a host may take before repeating a failed operation.
pub enum RetryDisposition {
    /// Repeating without changing the request cannot resolve the failure.
    Never,
    /// Isolate the failing input or resource for host review.
    Quarantine,
    /// The operation may be retried under the same request semantics.
    Safe,
    /// Retry only with a supported idempotency mechanism.
    RequiresIdempotencyKey,
    /// Reconcile the external state before deciding whether to retry.
    RequiresRecovery,
    /// Retry after the indicated delay, subject to the stated external effect.
    After {
        /// Minimum delay before retry, in milliseconds.
        delay_ms: u64,
    },
}

/// Public operational failure. Custom providers are responsible for redacting
/// message/details before construction; the type does not sanitize arbitrary text.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageError {
    /// Cause classification, independent of whether an effect was committed.
    pub category: ErrorCategory,
    /// Operation phase in which failure was observed.
    pub phase: ErrorPhase,
    /// Provable external effect; unknown requires reconciliation before retry.
    pub remote_effect: RemoteEffect,
    /// Retry policy derived from both cause and external effect.
    pub retry: RetryDisposition,
    /// Stable machine-readable diagnostic code without secret or payload data.
    pub code: String,
    /// Public operational context only: never payload bytes, credentials,
    /// connection strings or untrusted server/callback exception text.
    pub message: String,
    /// Stable provider identifier used for dispatch and capability discovery.
    pub provider: Option<String>,
    /// Host execution identifier carried by `plenora-error-v1`. This component
    /// never produces one; a deserialized error keeps the value it carried.
    /// `null` and an absent key both read as `None`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_id: Option<ExecutionId>,
    #[serde(default)]
    /// Redacted structured diagnostics; callers constructing errors must sanitize values.
    pub details: BTreeMap<String, Value>,
}

/// Longest `execution_id` accepted by `plenora-error-v1`, in characters.
const EXECUTION_ID_MAX_CHARS: usize = 128;

/// Host execution identifier of `plenora-error-v1`: 1 to 128 characters,
/// counted as Unicode code points like JSON Schema lengths. Construction and
/// deserialization reject any other length.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct ExecutionId(String);

impl ExecutionId {
    /// Validates an execution identifier.
    ///
    /// # Errors
    /// Returns `invalid_configuration` when the identifier is empty or longer
    /// than 128 characters; the value is not echoed.
    pub fn new(value: impl Into<String>) -> StorageResult<Self> {
        let value = value.into();
        let length = value.chars().count();
        if length == 0 || length > EXECUTION_ID_MAX_CHARS {
            return Err(StorageError::invalid_configuration(
                "EXECUTION_ID_INVALID",
                "execution_id must contain 1 to 128 characters",
            ));
        }
        Ok(Self(value))
    }

    /// The identifier as carried on the wire.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for ExecutionId {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Self::new(String::deserialize(deserializer)?)
            .map_err(|_| serde::de::Error::custom("execution_id must contain 1 to 128 characters"))
    }
}

impl StorageError {
    #[must_use]
    /// Construct a public error; callers must redact code and message before passing them.
    pub fn new(
        category: ErrorCategory,
        phase: ErrorPhase,
        remote_effect: RemoteEffect,
        retry: RetryDisposition,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            category,
            phase,
            remote_effect,
            retry,
            code: code.into(),
            message: message.into(),
            provider: None,
            execution_id: None,
            details: BTreeMap::new(),
        }
    }

    #[must_use]
    /// Attach a public provider identifier, never an endpoint or credential reference.
    pub fn with_provider(mut self, provider: impl Into<String>) -> Self {
        self.provider = Some(provider.into());
        self
    }

    /// Restates the remote outcome of an error whose publication state became
    /// known only after the error was produced.
    #[must_use]
    #[allow(
        clippy::missing_const_for_fn,
        reason = "Error enrichment may evolve independently of const evaluation"
    )]
    pub fn with_outcome(mut self, remote_effect: RemoteEffect, retry: RetryDisposition) -> Self {
        self.remote_effect = remote_effect;
        self.retry = retry;
        self
    }

    /// Adds a redacted, machine-readable detail. Details never carry hosts,
    /// paths or credential material.
    #[must_use]
    pub fn with_detail(mut self, name: impl Into<String>, value: &'static str) -> Self {
        self.details
            .insert(name.into(), Value::String(value.to_owned()));
        self
    }

    /// Restates an error whose effect was undone by a verified cleanup.
    ///
    /// Only a cause that could behave differently on its own becomes retryable:
    /// a rolled back configuration or resource-limit failure would deterministically
    /// fail again.
    #[must_use]
    pub fn rolled_back(self) -> Self {
        let retry = match self.category {
            ErrorCategory::Cancelled
            | ErrorCategory::Timeout
            | ErrorCategory::Transient
            | ErrorCategory::Io => RetryDisposition::Safe,
            _ => RetryDisposition::Never,
        };
        self.with_outcome(RemoteEffect::RolledBack, retry)
    }

    /// Restates an error whose cleanup could not be confirmed, keeping the
    /// original cause instead of replacing it with a generic cleanup failure.
    #[must_use]
    pub fn cleanup_unconfirmed(self, cleanup: &'static str) -> Self {
        self.with_detail("cleanup", cleanup)
            .with_outcome(RemoteEffect::Unknown, RetryDisposition::RequiresRecovery)
    }

    /// Parent directories are effects too, even if the destination was never
    /// opened or its staging file was successfully removed.
    #[must_use]
    pub fn with_preparation_effect(mut self, may_have_created_directories: bool) -> Self {
        if may_have_created_directories {
            if matches!(
                self.remote_effect,
                RemoteEffect::None | RemoteEffect::RolledBack
            ) {
                self = self.with_outcome(RemoteEffect::Unknown, RetryDisposition::RequiresRecovery);
            }
            self = self.with_detail("preparation", "directories_may_remain");
        }
        self
    }

    #[must_use]
    /// Construct a non-retryable validation error with no external effect.
    pub fn invalid_configuration(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(
            ErrorCategory::InvalidConfiguration,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            code,
            message,
        )
    }

    #[must_use]
    /// Reject an unsupported guarantee before effects, with retry disabled.
    pub fn unsupported(message: impl Into<String>) -> Self {
        Self::new(
            ErrorCategory::Unsupported,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "UNSUPPORTED",
            message,
        )
    }

    #[must_use]
    /// Report a closed engine without external effects; retry requires a new engine.
    pub fn engine_closed() -> Self {
        Self::new(
            ErrorCategory::Execution,
            ErrorPhase::Validate,
            RemoteEffect::None,
            RetryDisposition::Never,
            "ENGINE_CLOSED",
            "storage engine is closed",
        )
    }

    #[must_use]
    /// Classify cancellation; an interrupted mutation has unknown effect and requires recovery.
    pub fn cancelled(phase: ErrorPhase, mutating: bool) -> Self {
        Self::new(
            ErrorCategory::Cancelled,
            phase,
            if mutating {
                RemoteEffect::Unknown
            } else {
                RemoteEffect::None
            },
            if mutating {
                RetryDisposition::RequiresRecovery
            } else {
                RetryDisposition::Safe
            },
            "CANCELLED",
            "storage operation was cancelled",
        )
    }

    #[must_use]
    /// Classify deadline expiry; an interrupted mutation has unknown effect and requires recovery.
    pub fn timeout(phase: ErrorPhase, mutating: bool) -> Self {
        Self::new(
            ErrorCategory::Timeout,
            phase,
            if mutating {
                RemoteEffect::Unknown
            } else {
                RemoteEffect::None
            },
            if mutating {
                RetryDisposition::RequiresRecovery
            } else {
                RetryDisposition::Safe
            },
            "TIMEOUT",
            "storage operation exceeded its deadline",
        )
    }
}

impl fmt::Display for StorageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for StorageError {}
