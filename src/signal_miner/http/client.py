"""One HTTP client for every source: per-host pacing, bounded retries, explicit timeouts."""
import email.utils
import random
import threading
import time
from datetime import timezone
from urllib.parse import urlsplit

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 120.0
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class RetryExhausted(Exception):
    def __init__(self, url: str, last_status: int | None):
        super().__init__(f"{url}: gave up after retries (last status {last_status})")
        self.url = url
        self.last_status = last_status


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Test clock: sleeping advances time instantly and is recorded."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def parse_retry_after(value: str | None, now_epoch: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # "-0000" parses as naive; HTTP dates are always UTC
        when = when.replace(tzinfo=timezone.utc)
    now_epoch = time.time() if now_epoch is None else now_epoch
    return max(0.0, when.timestamp() - now_epoch)


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        clock=None,
        host_intervals: dict[str, float] | None = None,
        host_groups: dict[str, str] | None = None,
        default_interval: float = 1.0,
        max_tries: int = 5,
        jitter=random.random,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.clock = clock or RealClock()
        self.timeout = timeout
        self.max_tries = max_tries
        self._jitter = jitter
        self._intervals = dict(host_intervals or {})
        self._groups = dict(host_groups or {})
        self._default_interval = default_interval
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def _pace(self, host: str) -> None:
        key = self._groups.get(host, host)
        interval = self._intervals.get(key, self._default_interval)
        with self._lock:
            now = self.clock.monotonic()
            slot = max(now, self._next_slot.get(key, now))
            self._next_slot[key] = slot + interval
        if slot > now:
            self.clock.sleep(slot - now)

    def _hold_host(self, host: str, seconds: float) -> None:
        """Push the host's next slot out, so every thread waits, not only the one that got the 429."""
        key = self._groups.get(host, host)
        with self._lock:
            now = self.clock.monotonic()
            self._next_slot[key] = max(self._next_slot.get(key, now), now + seconds)

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET with retries on 429, 5xx, timeouts and connection errors. GET is idempotent, so retrying is safe."""
        host = urlsplit(url).hostname or ""
        last_status = None
        for attempt in range(1, self.max_tries + 1):
            self._pace(host)
            try:
                resp = self._http.get(url, **kwargs)
            except httpx.TransportError:  # includes every TimeoutException
                resp = None
            if resp is not None and resp.status_code not in RETRY_STATUSES:
                return resp
            last_status = resp.status_code if resp is not None else None
            if attempt == self.max_tries:
                break
            wait = parse_retry_after(resp.headers.get("Retry-After")) if resp is not None else None
            if wait is not None and wait > MAX_RETRY_AFTER_S:
                # Waiting that long would stall the run; sending early would break the server's ask.
                raise RetryExhausted(url, last_status)
            if wait is None:
                wait = min(2.0 ** attempt, 30.0) * (0.5 + self._jitter() / 2)
            self._hold_host(host, wait)
        raise RetryExhausted(url, last_status)

    def close(self) -> None:
        self._http.close()
