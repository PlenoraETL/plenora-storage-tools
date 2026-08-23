use std::{future::Future, time::Instant};

use tokio_util::sync::CancellationToken as TokioCancellationToken;

use crate::{ErrorPhase, StorageError, StorageResult};

#[derive(Clone, Debug, Default)]
pub struct CancellationToken {
    inner: TokioCancellationToken,
}

impl CancellationToken {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.inner.cancel();
    }

    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.inner.is_cancelled()
    }

    pub async fn cancelled(&self) {
        self.inner.cancelled().await;
    }
}

#[derive(Clone, Debug, Default)]
pub struct ExecutionControl {
    pub cancellation: CancellationToken,
    pub deadline: Option<Instant>,
}

impl ExecutionControl {
    #[must_use]
    pub fn new(cancellation: CancellationToken) -> Self {
        Self {
            cancellation,
            deadline: None,
        }
    }

    #[must_use]
    pub fn with_deadline(mut self, deadline: Instant) -> Self {
        self.deadline = Some(deadline);
        self
    }

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
