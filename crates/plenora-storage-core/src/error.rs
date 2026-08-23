use std::{collections::BTreeMap, fmt};

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub type StorageResult<T> = Result<T, StorageError>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCategory {
    InvalidConfiguration,
    Unsupported,
    NotFound,
    Conflict,
    Authentication,
    Authorization,
    Timeout,
    Cancelled,
    ResourceLimit,
    Io,
    Protocol,
    Transient,
    Execution,
    Internal,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ErrorPhase {
    Validate,
    Connect,
    Probe,
    Prepare,
    Read,
    Write,
    Commit,
    Cleanup,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RemoteEffect {
    None,
    RolledBack,
    Partial,
    Committed,
    Unknown,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RetryDisposition {
    Never,
    Quarantine,
    Safe,
    RequiresIdempotencyKey,
    RequiresRecovery,
    After { delay_ms: u64 },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageError {
    pub category: ErrorCategory,
    pub phase: ErrorPhase,
    pub remote_effect: RemoteEffect,
    pub retry: RetryDisposition,
    pub code: String,
    pub message: String,
    pub provider: Option<String>,
    #[serde(default)]
    pub details: BTreeMap<String, Value>,
}

impl StorageError {
    #[must_use]
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
            details: BTreeMap::new(),
        }
    }

    #[must_use]
    pub fn with_provider(mut self, provider: impl Into<String>) -> Self {
        self.provider = Some(provider.into());
        self
    }

    #[must_use]
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
