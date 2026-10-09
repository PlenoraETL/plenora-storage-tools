//! Inactivity limit for one request without a deadline.
//!
//! A request without a deadline must still end when it stops moving. A fixed
//! read timeout is not that: reqwest starts its read timeout with the request
//! and does not re-arm it with the bytes sent, so it cuts an upload that is
//! still moving, and it has no write timeout at all. [`Inactivity`] measures
//! progress in both directions on one clock instead. The transport adapters
//! re-arm it ([`Inactivity::touch`]) for every frame of the request body the
//! transport takes and every frame of the response body it delivers;
//! [`Inactivity::guard`] fails the request once the clock runs out while the
//! body is sent or the answer is awaited, and [`Inactivity::poll_idle`] fails
//! a response body that stops arriving.
//!
//! The limit is intrinsic to what the client can see: a frame counts once the
//! transport has taken it, which may be into socket buffers the server never
//! reads. A server that stops reading is noticed once those buffers are full.

use std::{
    fmt,
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::Duration,
};
use tokio::time::{Instant, Sleep};

/// One request's inactivity clock, shared by the adapters that watch its
/// body and its answer. Cloning shares the clock. `None` as the limit
/// disables it: every method then lets the request run.
#[derive(Clone, Debug)]
pub struct Inactivity {
    limit: Option<Duration>,
    clock: Arc<Clock>,
}

#[derive(Debug)]
struct Clock {
    started: Instant,
    /// Time of the last progress, in nanoseconds since `started`.
    last: AtomicU64,
}

impl Inactivity {
    /// Starts the clock now.
    #[must_use]
    pub fn new(limit: Option<Duration>) -> Self {
        Self {
            limit,
            clock: Arc::new(Clock {
                started: Instant::now(),
                last: AtomicU64::new(0),
            }),
        }
    }

    /// The limit on inactivity, if any.
    #[must_use]
    pub const fn limit(&self) -> Option<Duration> {
        self.limit
    }

    /// Records progress now: the clock restarts.
    pub fn touch(&self) {
        let elapsed = u64::try_from(self.clock.started.elapsed().as_nanos()).unwrap_or(u64::MAX);
        self.clock.last.fetch_max(elapsed, Ordering::Relaxed);
    }

    /// When the clock runs out unless progress is recorded first; `None`
    /// without a limit, or past the end of representable time.
    fn expires_at(&self) -> Option<Instant> {
        let last = Duration::from_nanos(self.clock.last.load(Ordering::Relaxed));
        self.clock
            .started
            .checked_add(last)?
            .checked_add(self.limit?)
    }

    fn expired(&self) -> bool {
        self.expires_at()
            .is_some_and(|expires_at| expires_at <= Instant::now())
    }

    /// Runs `future` until it completes, or fails once the clock runs out.
    ///
    /// # Errors
    /// [`InactivityTimeout`] when no progress was recorded for the whole
    /// limit; `future` is dropped, which abandons the request.
    pub async fn guard<F: Future>(&self, future: F) -> Result<F::Output, InactivityTimeout> {
        let mut future = std::pin::pin!(future);
        loop {
            let Some(expires_at) = self.expires_at() else {
                return Ok(future.await);
            };
            tokio::select! {
                biased;
                output = &mut future => return Ok(output),
                () = tokio::time::sleep_until(expires_at) => {
                    if self.expired() {
                        return Err(InactivityTimeout);
                    }
                }
            }
        }
    }

    /// Polls the clock on behalf of a body that has nothing to deliver yet.
    /// `timer` is the body's own timer slot, created and re-armed here.
    ///
    /// Returns `Ready` once the clock has run out; `Pending` otherwise, with
    /// `cx` woken when it next could.
    pub fn poll_idle(
        &self,
        timer: &mut Option<Pin<Box<Sleep>>>,
        cx: &mut Context<'_>,
    ) -> Poll<InactivityTimeout> {
        loop {
            let Some(expires_at) = self.expires_at() else {
                return Poll::Pending;
            };
            let sleep = timer.get_or_insert_with(|| Box::pin(tokio::time::sleep_until(expires_at)));
            if sleep.deadline() != expires_at {
                sleep.as_mut().reset(expires_at);
            }
            if sleep.as_mut().poll(cx).is_pending() {
                return Poll::Pending;
            }
            if self.expired() {
                return Poll::Ready(InactivityTimeout);
            }
        }
    }
}

/// A request made no progress for the whole inactivity limit.
///
/// Converts into an [`std::io::Error`] of kind
/// [`TimedOut`](std::io::ErrorKind::TimedOut), the form the transport error
/// mappings recognise as a timeout.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InactivityTimeout;

impl fmt::Display for InactivityTimeout {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("request made no progress within the inactivity limit")
    }
}

impl std::error::Error for InactivityTimeout {}

impl From<InactivityTimeout> for std::io::Error {
    fn from(timeout: InactivityTimeout) -> Self {
        Self::new(std::io::ErrorKind::TimedOut, timeout)
    }
}

#[cfg(test)]
#[path = "inactivity_tests.rs"]
mod tests;
