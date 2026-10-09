use super::{Inactivity, InactivityTimeout};
use std::{future::poll_fn, time::Duration};
use tokio::time::Instant;

const LIMIT: Duration = Duration::from_secs(300);

/// Progress every 100 s keeps a request alive for ten minutes, three times
/// the limit.
#[tokio::test(start_paused = true)]
async fn progress_keeps_a_long_request_alive() {
    let inactivity = Inactivity::new(Some(LIMIT));
    let started = Instant::now();
    let watched = inactivity.clone();
    let result = inactivity
        .guard(async move {
            for _ in 0..6 {
                tokio::time::sleep(Duration::from_secs(100)).await;
                watched.touch();
            }
        })
        .await;
    assert_eq!(result, Ok(()));
    assert_eq!(started.elapsed(), Duration::from_secs(600));
}

/// Without progress the request fails exactly at the limit after the last
/// one, whatever came before.
#[tokio::test(start_paused = true)]
async fn a_request_that_stops_moving_fails_at_the_limit() {
    let inactivity = Inactivity::new(Some(LIMIT));
    let started = Instant::now();
    let watched = inactivity.clone();
    let result = inactivity
        .guard(async move {
            tokio::time::sleep(Duration::from_secs(200)).await;
            watched.touch();
            std::future::pending::<()>().await;
        })
        .await;
    assert_eq!(result, Err(InactivityTimeout));
    assert_eq!(started.elapsed(), Duration::from_secs(500));
}

/// Without a limit nothing is ever cut.
#[tokio::test(start_paused = true)]
async fn without_a_limit_the_request_runs_to_completion() {
    let inactivity = Inactivity::new(None);
    let started = Instant::now();
    let result = inactivity
        .guard(tokio::time::sleep(Duration::from_secs(3_600)))
        .await;
    assert_eq!(result, Ok(()));
    assert_eq!(started.elapsed(), Duration::from_secs(3_600));
    let mut timer = None;
    let idle = poll_fn(|cx| Inactivity::new(None).poll_idle(&mut timer, cx).map(Some));
    let outcome = tokio::time::timeout(Duration::from_secs(3_600), idle).await;
    assert!(outcome.is_err(), "a body without a limit never runs out");
}

/// A body waiting for data runs out at the limit after the last frame, and
/// a frame in between moves the expiry.
#[tokio::test(start_paused = true)]
async fn an_idle_body_runs_out_at_the_limit_after_the_last_frame() {
    let inactivity = Inactivity::new(Some(LIMIT));
    let started = Instant::now();
    let toucher = inactivity.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(250)).await;
        toucher.touch();
    });
    let mut timer = None;
    let timeout = poll_fn(|cx| inactivity.poll_idle(&mut timer, cx)).await;
    assert_eq!(timeout, InactivityTimeout);
    assert_eq!(started.elapsed(), Duration::from_secs(550));
}

/// The timeout reaches the transport mappings as an I/O timeout.
#[test]
fn the_timeout_is_an_io_timeout() {
    let error = std::io::Error::from(InactivityTimeout);
    assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
}
