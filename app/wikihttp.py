"""Polite HTTP for wikis (Fandom/MediaWiki).

Fandom sits behind bot protection that answers bursts of requests from scripts with
403/429. So: say honestly who we are (MediaWiki asks scripts to), keep to about one
request a second per wiki, and when told to back off, wait as long as asked — or stop
for now and let the next fetch carry on — instead of retrying straight away."""
from __future__ import annotations

import email.utils
import threading
import time

import httpx

from . import __version__

WIKI_UA = (f"EpisodeID/{__version__} (self-hosted TV episode identifier; "
           "https://github.com/CtznSniiips/EpisodeID) httpx")
MIN_INTERVAL = 1.0     # seconds between requests to one wiki
MAX_WAIT = 60.0        # longer than this and we give up for now instead of waiting
BLOCKED = (403, 429, 502, 503, 504)

_lock = threading.Lock()
_hosts: dict[str, dict] = {}   # host -> {"last", "until", "strikes"}
LAST_ERROR: dict = {}          # {"why": "HTTP 403"} — for log messages


def client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": WIKI_UA, "Accept-Encoding": "gzip"})


def _retry_after(r: httpx.Response) -> float | None:
    v = (r.headers.get("retry-after") or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        return max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def blocked_for(host: str) -> float:
    with _lock:
        return max(0.0, _hosts.get(host, {}).get("until", 0) - time.time())


def get(c: httpx.Client, url: str, params: dict | None = None) -> httpx.Response | None:
    """One paced GET. Returns the response (any status), or None when the wiki can't be
    reached or has told us to stay away for longer than MAX_WAIT."""
    host = httpx.URL(url).host
    for attempt in range(2):
        with _lock:
            st = _hosts.setdefault(host, {"last": 0.0, "until": 0.0, "strikes": 0})
            wait = max(st["until"] - time.time(), st["last"] + MIN_INTERVAL - time.time(), 0.0)
        if wait > MAX_WAIT:
            LAST_ERROR["why"] = LAST_ERROR.get("why") or "backing off"
            return None
        if wait:
            time.sleep(wait)
        with _lock:
            st["last"] = time.time()
        try:
            r = c.get(url, params=params)
        except httpx.HTTPError as e:
            LAST_ERROR["why"] = type(e).__name__
            return None
        if r.status_code not in BLOCKED:
            with _lock:
                st["strikes"] = 0
            return r
        LAST_ERROR["why"] = f"HTTP {r.status_code}"
        with _lock:
            st["strikes"] += 1
            asked = _retry_after(r)
            back = asked if asked is not None else min(300.0, 15.0 * 2 ** (st["strikes"] - 1))
            st["until"] = time.time() + back
        if back > MAX_WAIT or attempt:
            return r
    return r
