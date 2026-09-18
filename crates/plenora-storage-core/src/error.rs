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

    /// Restates the remote outcome of an error whose publication state became
    /// known only after the error was produced.
    #[must_use]
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
