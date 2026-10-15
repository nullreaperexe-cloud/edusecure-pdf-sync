"""Bounded transport retries. Only retry writes with deterministic identities."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests

LOG = logging.getLogger(__name__)
RETRYABLE = {408, 429, 500, 502, 503, 504}


def canonical_url(value: str) -> str:
    """Hostnames are case-insensitive; PDF paths and signed queries are not."""
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def retry_delay(header: str | None, attempt: int) -> float:
    delay = min(20.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.5)
    if header:
        try:
            requested = float(header)
        except ValueError:
            try:
                requested = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                requested = 0
        delay = max(delay, requested)
    return max(0, min(30.0, delay))


def request(method: str, url: str, *, attempts: int = 3, **kwargs):
    if attempts < 1:
        raise ValueError("At least one request attempt is required")
    kwargs.setdefault("timeout", (7, 30))
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            if attempt + 1 == attempts:
                raise
            delay = retry_delay(None, attempt)
            LOG.warning("Network request failed; retrying in %.1f seconds (%s/%s)", delay, attempt + 1, attempts)
        else:
            # Quota failures receive one retry, then propagate to the cycle report.
            budget = min(attempts, 2) if response.status_code == 429 else attempts
            if response.status_code not in RETRYABLE or attempt + 1 >= budget:
                return response
            delay = retry_delay(response.headers.get("Retry-After"), attempt)
            LOG.warning("HTTP %s; retrying in %.1f seconds (%s/%s)", response.status_code, delay, attempt + 1, budget)
            response.close()
        time.sleep(delay)
