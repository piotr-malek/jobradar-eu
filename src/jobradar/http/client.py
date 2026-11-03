"""PoliteClient: the only way jobradar talks to the network.

- Per-host minimum interval shared by every client in the process (token bucket
  with one token), so two sources hitting the same host still respect it.
- Per-client request budget (one source run never exceeds it).
- Retries only for network errors and 5xx, with exponential backoff + jitter,
  honouring Retry-After (capped).
- 403/429 and Cloudflare-style challenge pages raise ``Blocked`` immediately:
  no retries, the caller's circuit breaker decides what happens next.
- Optional robots.txt enforcement for HTML / frontend endpoints.
- Optional recording of raw responses (fixtures / debugging).
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

API_UA = "job-radar/0.1 (personal job-search aggregator; low volume)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}

RETRY_AFTER_CAP_S = 120.0
CHALLENGE_MARKERS = ("challenge-platform", "cf-chl-", "Just a moment...", "Attention Required! | Cloudflare")


def _api_error(resp: httpx.Response) -> str:
    """A JSON API's own reason for a refusal (e.g. a plan limit), so the source health row says why."""
    if "json" not in resp.headers.get("content-type", ""):
        return ""
    text = " ".join(resp.text.split())
    return f" [{text[:160]}]" if text else ""


class Blocked(Exception):
    """The host refused us (403/429/challenge/robots). Never retried."""

    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{reason} ({status}) for {url}")
        self.url = url
        self.reason = reason
        self.status = status


class BudgetExceeded(Exception):
    pass


_host_lock = threading.Lock()
_host_next_ok: dict[str, float] = {}


def _wait_for_host(host: str, min_interval: float) -> None:
    while True:
        with _host_lock:
            now = time.monotonic()
            ready_at = _host_next_ok.get(host, 0.0)
            if now >= ready_at:
                _host_next_ok[host] = now + min_interval
                return
            sleep_for = ready_at - now
        time.sleep(min(sleep_for, 5.0))


_MULTI_TLDS = ("co.uk", "com.pl", "com.au", "co.jp", "com.br")


def _rate_key(netloc: str) -> str:
    """Rate-limit per registrable domain so {company}.recruitee.com shares one clock."""
    host = netloc.split(":")[0].lower()
    parts = host.split(".")
    n = 3 if any(host.endswith(t) for t in _MULTI_TLDS) else 2
    return ".".join(parts[-n:]) if len(parts) > n else host


def reset_host_clock() -> None:
    with _host_lock:
        _host_next_ok.clear()


@dataclass
class ClientStats:
    requests: int = 0
    bytes: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    retries: int = 0

    def count(self, status: int | str, nbytes: int = 0) -> None:
        key = str(status)
        self.status_counts[key] = self.status_counts.get(key, 0) + 1
        self.bytes += nbytes


class PoliteClient:
    def __init__(
        self,
        source_id: str,
        *,
        rps: float = 0.5,
        max_requests: int = 200,
        timeout: float = 30.0,
        retries: int = 3,
        browser: bool = False,
        respect_robots: bool = False,
        record_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: bool = True,
    ):
        self.source_id = source_id
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.max_requests = max_requests
        self.retries = retries
        self.respect_robots = respect_robots
        self.record_dir = record_dir
        self.sleep = sleep
        self.stats = ClientStats()
        headers = dict(BROWSER_HEADERS) if browser else {"User-Agent": API_UA, "Accept": "application/json, */*;q=0.5"}
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._record_n = 0

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def remaining(self) -> int:
        return max(0, self.max_requests - self.stats.requests)

    # -- robots ---------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self._send("GET", f"{base}/robots.txt", check_robots=False, raise_blocked=False)
                if resp.status_code >= 400:
                    parser = None  # no robots.txt -> everything allowed
                else:
                    parser.parse(resp.text.splitlines())
            except (httpx.HTTPError, BudgetExceeded):
                parser = None
            self._robots[base] = parser
        parser = self._robots[base]
        if parser is None:
            return True
        return parser.can_fetch("*", url) or parser.can_fetch(BROWSER_UA, url)

    # -- requests --------------------------------------------------------------
    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self._send("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self._send("POST", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.get(url, **kw)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, url: str, **kw: Any) -> Any:
        resp = self.post(url, **kw)
        resp.raise_for_status()
        return resp.json()

    def _send(
        self,
        method: str,
        url: str,
        *,
        check_robots: bool = True,
        raise_blocked: bool = True,
        **kw: Any,
    ) -> httpx.Response:
        if check_robots and self.respect_robots and not self.allowed(url):
            raise Blocked(url, "robots.txt disallows")
        host = _rate_key(urlsplit(url).netloc)
        attempt = 0
        while True:
            if self.stats.requests >= self.max_requests:
                raise BudgetExceeded(f"{self.source_id}: request budget {self.max_requests} exhausted")
            if self.sleep and self.min_interval:
                _wait_for_host(host, self.min_interval)
            self.stats.requests += 1
            try:
                resp = self._client.request(method, url, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                self.stats.count(type(exc).__name__)
                if attempt >= self.retries:
                    raise
                attempt += 1
                self.stats.retries += 1
                self._backoff(attempt, None)
                continue
            self.stats.count(resp.status_code, len(resp.content))
            self._record(method, url, resp)
            if resp.status_code in (403, 429) and raise_blocked:
                raise Blocked(url, "refused" + _api_error(resp), resp.status_code)
            if raise_blocked and self._is_challenge(resp):
                raise Blocked(url, "anti-bot challenge", resp.status_code)
            if resp.status_code >= 500 and attempt < self.retries:
                attempt += 1
                self.stats.retries += 1
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp

    @staticmethod
    def _is_challenge(resp: httpx.Response) -> bool:
        if resp.headers.get("cf-mitigated") == "challenge":
            return True
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype or len(resp.content) > 200_000:
            return False
        head = resp.text[:20_000]
        return sum(marker in head for marker in CHALLENGE_MARKERS) >= 2

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2**attempt + random.uniform(0, 1), 30.0)
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError):
                    pass
            delay = max(0.0, min(delay, RETRY_AFTER_CAP_S))
        if self.sleep:
            time.sleep(delay)

    def _record(self, method: str, url: str, resp: httpx.Response) -> None:
        if not self.record_dir:
            return
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self._record_n += 1
        stem = f"{self._record_n:03d}"
        (self.record_dir / f"{stem}.body.gz").write_bytes(gzip.compress(resp.content))
        meta = {
            "method": method,
            "url": url,
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
        }
        (self.record_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=1))


class ReplayTransport(httpx.BaseTransport):
    """Serve recorded responses (from ``record_dir``) by URL; used in tests."""

    def __init__(self, record_dir: Path):
        self._by_url: dict[str, list[tuple[int, str | None, bytes]]] = {}
        for meta_path in sorted(record_dir.glob("*.meta.json")):
            meta = json.loads(meta_path.read_text())
            body = gzip.decompress((meta_path.parent / meta_path.name.replace(".meta.json", ".body.gz")).read_bytes())
            self._by_url.setdefault(meta["url"], []).append((meta["status"], meta.get("content_type"), body))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        candidates = self._by_url.get(url)
        if not candidates:
            # tolerate query-string ordering differences
            base = url.split("?")[0]
            for key, value in self._by_url.items():
                if key.split("?")[0] == base:
                    candidates = value
                    break
        if not candidates:
            return httpx.Response(404, content=b"not recorded", request=request)
        status, ctype, body = candidates[0] if len(candidates) == 1 else candidates.pop(0)
        headers = {"content-type": ctype} if ctype else {}
        return httpx.Response(status, content=body, headers=headers, request=request)
