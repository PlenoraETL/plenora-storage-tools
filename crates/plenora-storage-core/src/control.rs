use std::{future::Future, time::Instant};

use tokio_util::sync::CancellationToken as TokioCancellationToken;

use crate::{ErrorPhase, StorageError, StorageResult};

#[derive(Clone, Debug, Default)]
/// Cloneable cooperative cancellation shared by operations; cancellation is irreversible.
pub struct CancellationToken {
    inner: TokioCancellationToken,
}

impl CancellationToken {
    #[must_use]
    /// Create control state without a deadline; cancellation is initially unset unless supplied.
    pub fn new() -> Self {
        Self::default()
    }

    /// Signal cancellation to all clones; already committed effects are not undone.
    pub fn cancel(&self) {
        self.inner.cancel();
    }

    #[must_use]
    /// Return whether cancellation has been signalled.
    pub fn is_cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }

    /// Wait until cancellation is signalled; returns immediately if already cancelled.
    pub async fn cancelled(&self) {
        self.inner.cancelled().await;
    }
}

#[derive(Clone, Debug, Default)]
/// Cooperative cancellation and optional monotonic deadline for one operation.
pub struct ExecutionControl {
    /// Shared cancellation token; signalling it interrupts cooperative operation waits.
    pub cancellation: CancellationToken,
    /// Optional absolute monotonic deadline; None means no time limit.
    pub deadline: Option<Instant>,
}

impl ExecutionControl {
    #[must_use]
    /// Create control state without a deadline; cancellation is initially unset unless supplied.
    #[allow(
        clippy::missing_const_for_fn,
        reason = "Do not promise const construction for cancellation control internals"
    )]
    pub fn new(cancellation: CancellationToken) -> Self {
        Self {
            cancellation,
            deadline: None,
        }
    }

    #[must_use]
    /// Set an absolute monotonic deadline; an elapsed deadline rejects subsequent work.
    #[allow(
        clippy::missing_const_for_fn,
        reason = "Keep deadline configuration independent of const evaluation"
    )]
    pub fn with_deadline(mut self, deadline: Instant) -> Self {
        self.deadline = Some(deadline);
        self
    }

    ///
    /// # Errors
    /// Returns cancellation or timeout in the supplied phase. A mutating phase has unknown effect and requires recovery.
    /// Check cancellation and deadline without running external work.
    pub fn check(&self, phase: ErrorPhase, mutating: bool) -> StorageResult<()> {
        if self.cancellation.is_cancelled() {
            return Err(StorageError::cancelled(phase, mutating));
        }
        if self
            .deadline
            .is_some_and(|deadline| deadline <= Instant::now())
        {
            return Err(StorageError::timeout(phase, mutating));
        }
        Ok(())
    }

    ///
    /// # Errors
    /// Returns the future error unchanged, or cancellation/timeout in the supplied phase; interruption does not prove rollback.
    /// Race an operation against cancellation and deadline, preserving its terminal error.
    pub async fn run<T, F>(&self, future: F, phase: ErrorPhase, mutating: bool) -> StorageResult<T>
    where
        F: Future<Output = StorageResult<T>> + Send,
    {
        self.check(phase, mutating)?;
        match self.deadline {
            Some(deadline) => {
                tokio::select! {
                    biased;
                    () = self.cancellation.cancelled() => Err(StorageError::cancelled(phase, mutating)),
                    () = tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)) => Err(StorageError::timeout(phase, mutating)),
                    result = future => result,
                }
            }
            None => {
                tokio::select! {
                    biased;
                    () = self.cancellation.cancelled() => Err(StorageError::cancelled(phase, mutating)),
                    result = future => result,
                }
            }
        }
    }
}
