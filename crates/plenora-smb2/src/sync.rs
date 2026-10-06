//! Locks and system primitives whose failures are explicit.
//!
//! `std::sync::Mutex` reports through poisoning that a thread panicked while
//! holding it. Unwrapping that report turns one panic into a cascade across
//! every later caller; discarding it hands them state that may be
//! half-updated. The two wrappers here do neither: [`StateLock`] refuses
//! with [`Error::Internal`], and [`ValueLock`] never exposes its value
//! between two states, so even a poisoned one still holds a whole value.

use std::sync::{Mutex, MutexGuard, PoisonError};

use crate::Error;

/// Mutex over state updated in several steps (maps, queues, crypto state).
///
/// A panic while it was held may have left that state inconsistent, so every
/// later [`lock`](Self::lock) fails with [`Error::Internal`] naming `what`.
pub(crate) struct StateLock<T> {
    inner: Mutex<T>,
    what: &'static str,
}

impl<T> StateLock<T> {
    /// Wraps `value`; `what` names the state in the error a poisoned lock
    /// produces.
    pub(crate) const fn new(what: &'static str, value: T) -> Self {
        Self {
            inner: Mutex::new(value),
            what,
        }
    }

    /// Locks the state, or reports that an earlier panic left it untrusted.
    pub(crate) fn lock(&self) -> Result<MutexGuard<'_, T>, Error> {
        self.inner
            .lock()
            .map_err(|_| Error::Internal { what: self.what })
    }

    /// Locks the state even if it is poisoned, for teardown only: removing or
    /// failing what it holds (a waiter that must hear an error, a task to
    /// abort). Nothing read through this guard may be treated as valid state;
    /// it exists so a poisoned connection still fails its callers instead of
    /// leaving them parked.
    pub(crate) fn lock_for_teardown(&self) -> MutexGuard<'_, T> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Mutex over a value that is only ever read or replaced whole.
///
/// It never hands out `&mut T`: a value is copied or cloned out, swapped with
/// `mem::replace`, or recomputed from a copy and stored back in one
/// assignment. A panic under the lock (a panicking `Clone`, or the closure
/// given to [`update`](Self::update)) therefore happens before the stored
/// value is touched, so the value is never left between two states and a
/// poisoned lock still holds a whole, previously stored value. Old values are
/// dropped after the guard is released, so a user `Drop` never runs under it.
pub(crate) struct ValueLock<T> {
    inner: Mutex<T>,
}

impl<T> ValueLock<T> {
    /// Wraps `value`.
    pub(crate) const fn new(value: T) -> Self {
        Self {
            inner: Mutex::new(value),
        }
    }

    fn guard(&self) -> MutexGuard<'_, T> {
        // A poisoned lock still holds a whole value (see the type
        // documentation), so reading it is exact, not a guess.
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Replaces the value, returning the previous one; it is dropped by the
    /// caller, outside the lock.
    pub(crate) fn replace(&self, value: T) -> T {
        std::mem::replace(&mut *self.guard(), value)
    }

    /// Replaces the value, dropping the previous one outside the lock.
    pub(crate) fn set(&self, value: T) {
        drop(self.replace(value));
    }

    /// Copies the value out.
    pub(crate) fn get(&self) -> T
    where
        T: Copy,
    {
        *self.guard()
    }

    /// Clones the value out.
    pub(crate) fn cloned(&self) -> T
    where
        T: Clone,
    {
        self.guard().clone()
    }

    /// Recomputes the value from a copy of it and stores the result; if `f`
    /// panics the stored value is unchanged.
    pub(crate) fn update<R>(&self, f: impl FnOnce(T) -> (T, R)) -> R
    where
        T: Copy,
    {
        let mut guard = self.guard();
        let (value, result) = f(*guard);
        *guard = value;
        result
    }
}

impl<T: Default> ValueLock<T> {
    /// Takes the value, leaving the default in its place.
    pub(crate) fn take(&self) -> T {
        self.replace(T::default())
    }
}

/// Fills `buf` from the operating system's random source.
///
/// A failing source is reported, never replaced by weaker randomness: these
/// bytes become nonces, salts, client challenges and GUIDs.
pub(crate) fn fill_random(buf: &mut [u8]) -> Result<(), Error> {
    getrandom::fill(buf).map_err(|_| Error::Internal {
        what: "operating system random source failed",
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn poisoned_state_lock_is_an_internal_error_not_a_panic() {
        let lock = Arc::new(StateLock::new("test state", 0_u32));
        let held = Arc::clone(&lock);
        let joined = std::thread::spawn(move || {
            let _guard = held.lock().unwrap();
            panic!("poison the lock");
        })
        .join();
        assert!(joined.is_err());
        let what = match lock.lock() {
            Err(Error::Internal { what }) => what,
            Err(other) => panic!("expected Internal, got {other:?}"),
            Ok(_) => panic!("a poisoned lock must not be handed out"),
        };
        assert_eq!(what, "test state");
        // Teardown still reaches the state.
        assert_eq!(*lock.lock_for_teardown(), 0);
    }

    #[test]
    fn value_lock_replaces_and_takes_whole_values() {
        let lock = ValueLock::new(Some(3_u8));
        assert_eq!(lock.get(), Some(3));
        assert_eq!(lock.replace(Some(4)), Some(3));
        assert_eq!(lock.take(), Some(4));
        assert_eq!(lock.get(), None);
        lock.set(Some(5));
        assert_eq!(lock.cloned(), Some(5));
        assert_eq!(lock.update(|v| (v.map(|n| n + 1), 7)), 7);
        assert_eq!(lock.get(), Some(6));
    }

    #[test]
    fn value_lock_keeps_the_whole_value_after_a_panicking_update() {
        let lock = Arc::new(ValueLock::new(1_u32));
        let held = Arc::clone(&lock);
        let joined = std::thread::spawn(move || {
            held.update(|_| -> (u32, ()) { panic!("panic before storing") });
        })
        .join();
        assert!(joined.is_err());
        assert_eq!(lock.get(), 1);
        lock.set(2);
        assert_eq!(lock.get(), 2);
    }

    #[test]
    fn random_source_fills_the_buffer() {
        let mut a = [0_u8; 32];
        let mut b = [0_u8; 32];
        fill_random(&mut a).unwrap();
        fill_random(&mut b).unwrap();
        assert_ne!(a, b);
    }
}
