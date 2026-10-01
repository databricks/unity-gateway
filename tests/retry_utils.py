"""Transient-retry helper for the live test suite.

Upstream MPS endpoints occasionally return 429/502/503/504 or drop connections
during tests; this module centralises the retry logic so individual tests don't
have to re-implement it.
"""

from __future__ import annotations

import time
import urllib.error
from collections.abc import Callable

import httpx

TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0
# Network-level exceptions that indicate a transient upstream blip.
DEFAULT_TRANSIENT_EXCEPTIONS = (httpx.TransportError, httpx.TimeoutException, urllib.error.URLError)


def is_transient_status(status: int) -> bool:
    return status in TRANSIENT_STATUS


def retry_transient[T](
    send: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    retry_exceptions: tuple[type[BaseException], ...] = DEFAULT_TRANSIENT_EXCEPTIONS,
    retry_on_result: Callable[[T], bool] | None = None,
) -> T:
    """Call *send()* up to *attempts* times, retrying on transient errors.

    Retries when *send* raises one of *retry_exceptions*, with one carve-out:
    ``urllib.error.HTTPError`` is a ``URLError`` subclass, so it is only retried
    when its ``.code`` is in ``TRANSIENT_STATUS`` — non-transient HTTP errors
    (400/401/403/404…) are re-raised immediately.

    Also retries when *retry_on_result* is provided and returns True for the
    returned value (e.g. an httpx.Response whose status_code is transient).

    Backoff is linear: ``sleep(backoff_seconds * attempt)`` (1-based) between
    attempts, skipped after the final attempt.  If the last outcome was an
    exception it is re-raised; if it was a transient result it is returned so
    the caller's assertion fails with real error detail.
    """
    last_exc: BaseException | None = None
    last_result: T | None = None

    for attempt in range(1, attempts + 1):
        last_exc = None
        try:
            result = send()
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError subclass — only retry on transient codes.
            if exc.code in TRANSIENT_STATUS:
                last_exc = exc
            else:
                raise
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, retry_exceptions):
                last_exc = exc
            else:
                raise
        else:
            if retry_on_result is not None and retry_on_result(result):
                last_result = result
            else:
                return result

        if attempt < attempts:
            time.sleep(backoff_seconds * attempt)

    if last_exc is not None:
        raise last_exc
    # last_result is the transient response — return it so the caller's assertion
    # surfaces the real status code / body instead of a generic retry-exhausted error.
    return last_result  # type: ignore[return-value]
