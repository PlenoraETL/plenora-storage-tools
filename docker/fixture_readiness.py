"""Bounded fixture startup with fixed diagnostics and verified application probes."""
import socket
import ssl
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

ATTEMPTS = 30
REQUEST_TIMEOUT = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


def urlopen(request, *, context, timeout):
    # Dedicated Docker services must answer directly, not through a proxy or redirect.
    return build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context)).open(request, timeout=timeout)


class FixtureFailure(Exception):
    """Only locally assigned categories/phases and numeric statuses are public."""
    def __init__(self, category, phase, retry=False, status=None):
        self.category, self.phase, self.retry, self.status = category, phase, retry, status
        super().__init__('fixture request failed')

    def diagnostic(self, service, attempt):
        effect = 'unknown' if self.phase.startswith('create_') else 'none'
        return (f'FAIL fixture={service} phase={self.phase} category={self.category} '
                f'status={self.status if self.status is not None else "none"} '
                f'attempt={attempt} remote_effect={effect} retry={"safe" if self.retry else "never"}')


def transport_failure(error, phase):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return FixtureFailure('tls_certificate', phase)
    if isinstance(reason, ssl.SSLError):
        return FixtureFailure('tls_protocol', phase)
    if isinstance(reason, (TimeoutError, ConnectionError, socket.gaierror)):
        return FixtureFailure('transport', phase, retry=True)
    return FixtureFailure('configuration', phase)


def status_failure(status, phase, *, missing=False):
    category = 'authentication' if status in (401, 403) else 'http'
    return FixtureFailure(category, phase, status in RETRY_STATUSES or (missing and status == 404), status)


def exchange(request, phase, *, context=None, allowed=(200,), missing=False):
    """Read at most 64 KiB; never print response bodies, headers or exceptions."""
    try:
        try:
            response = urlopen(request, context=context, timeout=REQUEST_TIMEOUT)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            if status not in allowed:
                raise status_failure(status, phase, missing=missing)
            data = response.read(65537)
            if len(data) > 65536:
                raise FixtureFailure('protocol', phase, status=status)
            return status, data
    except (OSError, URLError) as error:
        raise transport_failure(error, phase) from None
    except HTTPException:
        raise FixtureFailure('protocol', phase) from None


def wait_ready(service, operation, *, sleep=time.sleep):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            operation()
        except FixtureFailure as error:
            if not error.retry or attempt == ATTEMPTS:
                print(error.diagnostic(service, attempt), flush=True)
                return False
            sleep(1)
        except Exception:
            print(FixtureFailure('internal', 'initialization').diagnostic(service, attempt), flush=True)
            return False
        else:
            print(f'PASS fixture={service} phase=application_ready attempt={attempt}', flush=True)
            return True
    return False
