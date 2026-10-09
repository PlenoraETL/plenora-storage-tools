//! S3 requests under the inactivity limit of
//! [`plenora_storage_core::Inactivity`]: the request body re-arms the clock
//! as the transport takes each frame, the clock runs while the answer is
//! awaited, and the response body re-arms it with each frame delivered.
//!
//! Every request `object_store` sends goes through here, reads and writes
//! alike, so there is no routing decision to get wrong: `UploadPartCopy`,
//! `CompleteMultipartUpload` and a multipart part follow the same rule. A copy
//! of `plenora-storage-providers/src/watched.rs`, kept in step with it, because
//! the adapters implement `http_body::Body`, which must not cross the public API
//! of `plenora-storage-core`.
use bytes::Bytes;
use http_body::{Body, Frame, SizeHint};
use plenora_storage_core::{Inactivity, InactivityTimeout};
use std::{
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use tokio::time::Sleep;

/// Largest piece of a request body handed to the transport at once. A body
/// frame can be a whole multipart part held in memory: taken in one piece it
/// would count as progress only once, however long the socket takes to send
/// it, and a slow link would be cut while still moving. In 64 KiB pieces the
/// clock follows the socket.
const PIECE: usize = 64 * 1024;

/// A request body whose frames re-arm the clock as the transport takes them,
/// split into pieces of at most [`PIECE`] bytes.
pub struct RequestBody<B> {
    inner: B,
    inactivity: Inactivity,
    /// The rest of a data frame larger than [`PIECE`].
    rest: Option<Bytes>,
}

impl<B> RequestBody<B> {
    pub const fn new(inner: B, inactivity: Inactivity) -> Self {
        Self {
            inner,
            inactivity,
            rest: None,
        }
    }

    /// The next piece of `data`, keeping what is left for later.
    fn piece(&mut self, mut data: Bytes) -> Bytes {
        if data.len() > PIECE {
            self.rest = Some(data.split_off(PIECE));
        }
        data
    }
}

impl<B: Body<Data = Bytes> + Unpin> Body for RequestBody<B> {
    type Data = Bytes;
    type Error = B::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, B::Error>>> {
        let this = &mut *self;
        if let Some(rest) = this.rest.take() {
            this.inactivity.touch();
            let piece = this.piece(rest);
            return Poll::Ready(Some(Ok(Frame::data(piece))));
        }
        match Pin::new(&mut this.inner).poll_frame(cx) {
            Poll::Ready(Some(Ok(frame))) => {
                this.inactivity.touch();
                Poll::Ready(Some(Ok(match frame.into_data() {
                    Ok(data) => Frame::data(this.piece(data)),
                    Err(frame) => frame,
                })))
            }
            other => other,
        }
    }

    fn is_end_stream(&self) -> bool {
        self.rest.is_none() && self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        let rest = self
            .rest
            .as_ref()
            .map_or(0, |rest| u64::try_from(rest.len()).unwrap_or(u64::MAX));
        let inner = self.inner.size_hint();
        inner.exact().map_or_else(
            || {
                let mut hint = SizeHint::new();
                hint.set_lower(inner.lower().saturating_add(rest));
                hint
            },
            |exact| SizeHint::with_exact(exact.saturating_add(rest)),
        )
    }
}

/// Why a watched response body failed.
pub enum BodyFailure<E> {
    /// The transport failed.
    Inner(E),
    /// Nothing arrived for the whole inactivity limit.
    Inactive(InactivityTimeout),
}

/// A response body that fails once nothing arrives for the whole limit.
pub struct ResponseBody<B: Body, E> {
    inner: B,
    inactivity: Inactivity,
    timer: Option<Pin<Box<Sleep>>>,
    convert: fn(BodyFailure<B::Error>) -> E,
}

impl<B: Body, E> ResponseBody<B, E> {
    pub const fn new(
        inner: B,
        inactivity: Inactivity,
        convert: fn(BodyFailure<B::Error>) -> E,
    ) -> Self {
        Self {
            inner,
            inactivity,
            timer: None,
            convert,
        }
    }
}

impl<B: Body + Unpin, E> Body for ResponseBody<B, E> {
    type Data = B::Data;
    type Error = E;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<B::Data>, E>>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_frame(cx) {
            Poll::Ready(frame) => {
                this.inactivity.touch();
                let convert = this.convert;
                Poll::Ready(
                    frame.map(|frame| frame.map_err(|error| convert(BodyFailure::Inner(error)))),
                )
            }
            Poll::Pending => this
                .inactivity
                .poll_idle(&mut this.timer, cx)
                .map(|timeout| Some(Err((this.convert)(BodyFailure::Inactive(timeout))))),
        }
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

/// Runs one `object_store` request on `client` with the inactivity limit
/// `idle`, converting to and from reqwest as `object_store` would.
pub async fn call(
    client: &reqwest::Client,
    idle: Option<Duration>,
    request: object_store::client::HttpRequest,
) -> Result<object_store::client::HttpResponse, object_store::client::HttpError> {
    use object_store::client::{HttpError, HttpErrorKind, HttpResponse, HttpResponseBody};
    let (parts, body) = request.into_parts();
    let url = reqwest::Url::parse(&parts.uri.to_string())
        .map_err(|error| HttpError::new(HttpErrorKind::Request, error))?;
    let mut outgoing = reqwest::Request::new(parts.method, url);
    *outgoing.headers_mut() = parts.headers;
    let inactivity = Inactivity::new(idle);
    *outgoing.body_mut() = Some(reqwest::Body::wrap(RequestBody::new(
        body,
        inactivity.clone(),
    )));
    let mut response = inactivity
        .guard(client.execute(outgoing))
        .await
        .map_err(|timeout| HttpError::new(HttpErrorKind::Timeout, std::io::Error::from(timeout)))?
        .map_err(http_error)?;
    inactivity.touch();
    let status = response.status();
    let version = response.version();
    let headers = std::mem::take(response.headers_mut());
    let body = ResponseBody::new(
        reqwest::Body::from(response),
        inactivity,
        |failure| match failure {
            BodyFailure::Inner(error) => http_error(error),
            BodyFailure::Inactive(timeout) => {
                HttpError::new(HttpErrorKind::Timeout, std::io::Error::from(timeout))
            }
        },
    );
    let mut converted = HttpResponse::new(HttpResponseBody::new(body));
    *converted.status_mut() = status;
    *converted.version_mut() = version;
    *converted.headers_mut() = headers;
    Ok(converted)
}

/// The `object_store` form of a reqwest failure, classified as
/// `object_store` classifies it and without the URL.
fn http_error(error: reqwest::Error) -> object_store::client::HttpError {
    use object_store::client::{HttpError, HttpErrorKind};
    let io_kind = {
        let mut cause: Option<&(dyn std::error::Error + 'static)> = Some(&error);
        let mut found = None;
        while let Some(current) = cause {
            if let Some(io) = current.downcast_ref::<std::io::Error>() {
                found = Some(io.kind());
                break;
            }
            cause = current.source();
        }
        found
    };
    let kind = if error.is_timeout() || io_kind == Some(std::io::ErrorKind::TimedOut) {
        HttpErrorKind::Timeout
    } else if error.is_connect() {
        HttpErrorKind::Connect
    } else if error.is_decode() {
        HttpErrorKind::Decode
    } else if matches!(
        io_kind,
        Some(
            std::io::ErrorKind::ConnectionAborted
                | std::io::ErrorKind::ConnectionReset
                | std::io::ErrorKind::BrokenPipe
                | std::io::ErrorKind::UnexpectedEof
        )
    ) {
        HttpErrorKind::Interrupted
    } else {
        HttpErrorKind::Unknown
    };
    HttpError::new(kind, error.without_url())
}
