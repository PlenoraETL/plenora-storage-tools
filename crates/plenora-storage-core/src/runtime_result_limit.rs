//! RT-024: count the complete JSON payload without allocating another copy.
use std::io::{self, Write};

use super::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, RuntimeOperationDescriptor,
    StorageError, StorageResult, Value,
};

struct Budget {
    remaining: usize,
    exceeded: bool,
}

impl Write for Budget {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.remaining {
            self.exceeded = true;
            return Err(io::Error::other("runtime result exceeds payload limit"));
        }
        self.remaining -= bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

// STOR-LIMIT-001, RT-024: the Value has already been materialized; this bound
// cannot prevent allocation failure while providers build the result. Hazard:
// process termination before a typed error. Re-entry: fallible materialization
// with a shared allocation budget. Scope and status: docs/contract-limits.md.
pub(super) fn check(
    payload: &Value,
    descriptor: &RuntimeOperationDescriptor,
    limit: usize,
) -> StorageResult<()> {
    let mut budget = Budget {
        remaining: limit,
        exceeded: false,
    };
    if serde_json::to_writer(&mut budget, payload).is_ok() {
        return Ok(());
    }
    let (phase, effect) = match descriptor.operation {
        "storage.test" => (ErrorPhase::Probe, RemoteEffect::None),
        "storage.list" | "storage.stat" => (ErrorPhase::Read, RemoteEffect::None),
        // Shutdown starts commit; the host still owns proof of publication.
        "storage.get" => (ErrorPhase::Commit, RemoteEffect::Unknown),
        "storage.delete" if payload.get("deleted") == Some(&Value::Bool(false)) => {
            (ErrorPhase::Commit, RemoteEffect::None)
        }
        "storage.delete" => (ErrorPhase::Commit, RemoteEffect::Committed),
        _ => (ErrorPhase::Cleanup, RemoteEffect::Committed),
    };
    Err(StorageError::new(
        if budget.exceeded {
            ErrorCategory::ResourceLimit
        } else {
            ErrorCategory::Internal
        },
        phase,
        effect,
        if effect == RemoteEffect::Unknown {
            RetryDisposition::RequiresRecovery
        } else {
            RetryDisposition::Never
        },
        if budget.exceeded {
            "RUNTIME_RESULT_PAYLOAD_LIMIT"
        } else {
            "RUNTIME_RESULT_SERIALIZATION_FAILED"
        },
        "runtime result cannot be delivered whole",
    ))
}
