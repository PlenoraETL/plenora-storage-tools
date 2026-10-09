use super::{BodyFailure, PIECE, RequestBody, ResponseBody};
use bytes::Bytes;
use http_body::{Body, Frame};
use plenora_storage_core::{Inactivity, InactivityTimeout};
use std::{
    convert::Infallible,
    future::{Future, poll_fn},
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use tokio::time::{Instant, Sleep};

const LIMIT: Duration = Duration::from_secs(300);

/// A body that yields one frame of `size` bytes every `period`, forever.
struct Periodic {
    period: Duration,
    size: usize,
    sleep: Pin<Box<Sleep>>,
}

impl Periodic {
    fn new(period: Duration, size: usize) -> Self {
        Self {
            period,
            size,
            sleep: Box::pin(tokio::time::sleep(period)),
        }
    }
}

impl Body for Periodic {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        if self.sleep.as_mut().poll(cx).is_pending() {
            return Poll::Pending;
        }
        let next = self.sleep.deadline() + self.period;
        self.sleep.as_mut().reset(next);
        Poll::Ready(Some(Ok(Frame::data(Bytes::from(vec![0; self.size])))))
    }
}

/// Pulls frames from a request body as a transport would, under the
/// inactivity guard, and returns when the guard gives up.
async fn send_until_cut(inactivity: &Inactivity, body: Periodic) -> Duration {
    let started = Instant::now();
    let mut body = RequestBody::new(body, inactivity.clone());
    let result = inactivity
        .guard(async {
            while poll_fn(|cx| Pin::new(&mut body).poll_frame(cx))
                .await
                .is_some()
            {}
        })
        .await;
    assert_eq!(result, Err(InactivityTimeout));
    started.elapsed()
}

/// Empty frames are not progress: a body that sends only empty frames is
/// cut at the limit, however often it sends them.
#[tokio::test(start_paused = true)]
async fn empty_request_frames_do_not_keep_a_request_alive() {
    let inactivity = Inactivity::new(Some(LIMIT));
    let elapsed = tokio::time::timeout(
        LIMIT * 2,
        send_until_cut(&inactivity, Periodic::new(Duration::from_secs(10), 0)),
    )
    .await
    .expect("empty frames kept the request alive");
    assert_eq!(elapsed, LIMIT);
}

/// Data every 100 s keeps the request alive; the guard is only reached once
/// the data stops, which here it never does within ten minutes.
#[tokio::test(start_paused = true)]
async fn request_data_keeps_a_request_alive() {
    let inactivity = Inactivity::new(Some(LIMIT));
    let body = Periodic::new(Duration::from_secs(100), 1);
    let outcome =
        tokio::time::timeout(Duration::from_secs(600), send_until_cut(&inactivity, body)).await;
    assert!(outcome.is_err(), "the request was cut while moving");
}

/// A large frame is handed over in pieces of at most `PIECE` bytes, without
/// copying, and the size the transport announces does not change.
#[tokio::test(start_paused = true)]
async fn a_large_frame_is_handed_over_in_pieces() {
    let size = 3 * PIECE + 100;
    let data = Bytes::from(vec![7; size]);
    let start = data.as_ptr();
    let mut body = RequestBody::new(Full(Some(data)), Inactivity::new(Some(LIMIT)));
    assert_eq!(
        body.size_hint().exact(),
        Some(u64::try_from(size).expect("size"))
    );
    let mut pieces = Vec::new();
    while let Some(frame) = poll_fn(|cx| Pin::new(&mut body).poll_frame(cx)).await {
        let piece = frame.expect("frame").into_data().expect("data");
        pieces.push(piece);
    }
    assert_eq!(
        pieces.iter().map(Bytes::len).collect::<Vec<_>>(),
        vec![PIECE, PIECE, PIECE, 100]
    );
    assert_eq!(pieces.first().map(|piece| piece.as_ptr()), Some(start));
    assert!(body.is_end_stream());
}

/// A body made of one data frame.
struct Full(Option<Bytes>);

impl Body for Full {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        Poll::Ready(self.0.take().map(|data| Ok(Frame::data(data))))
    }

    fn is_end_stream(&self) -> bool {
        self.0.is_none()
    }

    fn size_hint(&self) -> http_body::SizeHint {
        http_body::SizeHint::with_exact(
            self.0
                .as_ref()
                .map_or(0, |data| u64::try_from(data.len()).unwrap_or(u64::MAX)),
        )
    }
}

/// Reads a response body until it fails, and returns when it did.
async fn read_until_cut(body: Periodic) -> Duration {
    let started = Instant::now();
    let mut body = ResponseBody::new(body, Inactivity::new(Some(LIMIT)), |failure| {
        matches!(failure, BodyFailure::Inactive(_))
    });
    loop {
        match poll_fn(|cx| Pin::new(&mut body).poll_frame(cx)).await {
            Some(Ok(_)) => {}
            Some(Err(inactive)) => {
                assert!(inactive, "the body failed for another reason");
                return started.elapsed();
            }
            None => panic!("the body ended"),
        }
    }
}

/// Empty frames of a response are not progress either.
#[tokio::test(start_paused = true)]
async fn empty_response_frames_do_not_keep_a_response_alive() {
    let elapsed = tokio::time::timeout(
        LIMIT * 2,
        read_until_cut(Periodic::new(Duration::from_secs(10), 0)),
    )
    .await
    .expect("empty frames kept the response alive");
    assert!(elapsed >= LIMIT, "{elapsed:?}");
    assert!(elapsed <= LIMIT + Duration::from_secs(10), "{elapsed:?}");
}

/// Response data every 100 s keeps the response alive.
#[tokio::test(start_paused = true)]
async fn response_data_keeps_a_response_alive() {
    let outcome = tokio::time::timeout(
        Duration::from_secs(600),
        read_until_cut(Periodic::new(Duration::from_secs(100), 1)),
    )
    .await;
    assert!(outcome.is_err(), "the response was cut while moving");
}
