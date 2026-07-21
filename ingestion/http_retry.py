"""
ingestion/http_retry.py

Shared retry wrapper for external HTTP GETs (board Fix #4).

3 attempts with exponential backoff (1s, 2s), retrying only transport-level
failures (httpx.TransportError: connect/read timeouts, connection resets,
DNS errors). HTTP status handling stays at the call sites — a 404 is a real
answer, not a transient fault.
"""
import time
from typing import Callable, Optional

import httpx

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 1.0


def get_with_retry(
    client,
    url: str,
    *,
    attempts: int = MAX_ATTEMPTS,
    base_delay: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> httpx.Response:
    """client.get(url, **kwargs), retrying transient transport errors.

    base_delay=None reads BASE_DELAY_SECONDS at call time so tests can zero
    the backoff without touching call sites.
    """
    if base_delay is None:
        base_delay = BASE_DELAY_SECONDS

    last_exc: Optional[httpx.TransportError] = None
    for attempt in range(attempts):
        try:
            return client.get(url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                delay = base_delay * (2 ** attempt)
                print(
                    f"[http_retry] GET {url} failed ({type(exc).__name__}: {exc}); "
                    f"attempt {attempt + 2}/{attempts} in {delay:g}s"
                )
                sleep(delay)
    raise last_exc
