"""Polite HTTP helpers: identified user agent, retries with backoff, robots.txt."""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import requests

AGENT_TOKEN = "bf-price-audit"
USER_AGENT = os.environ.get(
    "BF_USER_AGENT", f"{AGENT_TOKEN}/0.1 (open research crawler; public price data only)"
)
RETRY_STATUSES = {429, 430, 500, 502, 503, 504}
TIMEOUT = (10, 60)  # connect, read


class _Limiter:
    """Spaces out every request this process makes, across all stores.

    Shopify throttles by client IP across the whole platform, not per store.
    Measured on 2026-11-14: a steady 2 requests/second earned a penalty after
    about five minutes, during which nearly every store answered HTTP 429 for
    about ten minutes. So the limit that matters is global, and when several
    different stores throttle at once the right move is to stop everything,
    wait, and come back slower.
    """

    MAX_INTERVAL = 4.0  # never slower than one request every 4 seconds
    MAX_COOLDOWN = 300.0

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.blocked_until = 0.0
        self.cooldown = 60.0
        self.cooldowns = 0
        self.recent: deque[tuple[float, str]] = deque()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot, self.blocked_until)
            self.next_slot = slot + self.interval
        time.sleep(max(0.0, slot - now))

    def throttled(self, host: str) -> None:
        """Record an HTTP 429. Three different hosts within a minute means the
        platform is throttling this IP (one host alone is that store's own rule)."""
        with self.lock:
            now = time.monotonic()
            if now < self.blocked_until:
                return
            self.recent.append((now, host))
            while self.recent and now - self.recent[0][0] > 60:
                self.recent.popleft()
            if len({h for _, h in self.recent}) < 3:
                return
            self.recent.clear()
            self.cooldowns += 1
            self.blocked_until = now + self.cooldown
            self.interval = min(self.interval * 1.5, self.MAX_INTERVAL)
            print(
                f"platform throttling: pausing all requests {self.cooldown:.0f}s, "
                f"then 1 request per {self.interval:.2f}s",
                flush=True,
            )
            self.cooldown = min(self.cooldown * 2, self.MAX_COOLDOWN)


LIMITER = _Limiter(float(os.environ.get("BF_REQUESTS_PER_SECOND", "1")))
# Stores on other platforms are separate servers, not one platform throttling
# by IP, so they get their own budget and never wait on a Shopify pause. What
# protects each of them is that a store is read one page at a time with a pause
# between pages; this cap only bounds the total across the many stores read at
# once (raised from 2 on 2026-11-17, when 11,000 WooCommerce shops made it the
# bottleneck of every snapshot).
OTHER_LIMITER = _Limiter(float(os.environ.get("BF_OTHER_REQUESTS_PER_SECOND", "10")))


# SiteGround hosts thousands of the WooCommerce shops followed here, and its bot
# protection judges the request rate per visitor IP across its whole network,
# answering with an HTTP 202 challenge page when that rate is too high. Like
# Shopify, it gets a budget of its own, and a challenge counts as throttling.
# (Measured 2026-11-20: at 10 requests/s shared with other shops, 400-1,260 of
# about 3,000 SiteGround shops were challenged in each snapshot.)
SITEGROUND_LIMITER = _Limiter(float(os.environ.get("BF_SITEGROUND_REQUESTS_PER_SECOND", "0.5")))
SITEGROUND_MARK = "6b7412fb82ca5edfd0917e3957f05d89"  # SiteGround's "host-header" response header


def siteground(r: requests.Response) -> bool:
    return r.headers.get("host-header") == SITEGROUND_MARK or "sgcaptcha" in r.headers.get("location", "")


def challenged(r: requests.Response) -> bool:
    """A hosting network's bot check answering instead of the site: slow down, never solve it."""
    return r.status_code == 202 and (siteground(r) or "sgcaptcha" in r.text[:3000])


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
    return s


def get(
    session: requests.Session,
    url: str,
    *,
    cookies: dict | None = None,
    tries: int = 4,
    limiter: _Limiter = LIMITER,
) -> requests.Response:
    """GET with backoff on throttling and server errors. Honors Retry-After (capped at 60s)."""
    last = "no attempt made"
    status = None
    for attempt in range(tries):
        limiter.wait()
        try:
            r = session.get(url, cookies=cookies, timeout=TIMEOUT)
        except requests.RequestException as e:
            last, status = f"{type(e).__name__}", None
            time.sleep(2**attempt)
            continue
        if r.status_code in RETRY_STATUSES or challenged(r):
            last, status = f"HTTP {r.status_code}", r.status_code
            if r.status_code in (429, 430) or challenged(r):
                limiter.throttled(urlsplit(url).netloc)
            try:
                wait = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
            except ValueError:
                wait = 2 ** (attempt + 1)
            time.sleep(min(max(wait, 1.0), 60.0))
            continue
        return r
    raise FetchError(f"gave up after {tries} tries: {last}", status)


# --- robots.txt -------------------------------------------------------------


class Robots:
    """Minimal RFC 9309 matcher with `*` and `$` wildcards; longest match wins."""

    def __init__(self, text: str, agent: str = AGENT_TOKEN):
        groups: list[tuple[list[str], list[tuple[bool, str]], list[float]]] = []
        prev_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not prev_was_agent:
                    groups.append(([], [], []))
                groups[-1][0].append(value.lower())
                prev_was_agent = True
            elif key in ("allow", "disallow"):
                prev_was_agent = False
                if groups and value:
                    groups[-1][1].append((key == "allow", value))
            elif key == "crawl-delay":
                if groups:
                    try:
                        groups[-1][2].append(float(value))
                    except ValueError:
                        pass

        agent = agent.lower()
        mine = [g for g in groups if any(a != "*" and a in agent for a in g[0])]
        if not mine:
            mine = [g for g in groups if "*" in g[0]]
        self.rules = [rule for g in mine for rule in g[1]]
        delays = [d for g in mine for d in g[2]]
        self.crawl_delay = max(delays) if delays else 0.0

    @staticmethod
    def _regex(pattern: str) -> re.Pattern:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = ".*".join(re.escape(part) for part in body.split("*"))
        return re.compile(rx + ("$" if anchored else ""))

    def allowed(self, path: str) -> bool:
        best_len, best_allow = -1, True
        for allow, pattern in self.rules:
            if self._regex(pattern).match(path):
                n = len(pattern)
                if n > best_len or (n == best_len and allow):
                    best_len, best_allow = n, allow
        return best_allow


def fetch_robots(session: requests.Session, base_url: str, *, limiter: _Limiter = LIMITER) -> Robots:
    """A missing or unreadable robots.txt means no restrictions (RFC 9309 §2.3.1)."""
    limiter.wait()
    try:
        r = session.get(base_url.rstrip("/") + "/robots.txt", timeout=TIMEOUT)
        if r.status_code == 200:
            return Robots(r.text)
    except requests.RequestException:
        pass
    return Robots("")
