"""Asking wikis politely: an honest User-Agent, about one request a second, Retry-After
honoured, a 403 means stop for now (not three more URLs and retries), and only the
MediaWiki API is asked unless it answers without usable text."""
import os
import sys
import time
from pathlib import Path

import httpx

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_polite"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import references as R, wikihttp  # noqa: E402

seen = []
mode = {}
BODY = "<p>" + "Sportacus: Let's go! " * 60 + "</p>"


def handler(req):
    seen.append((req.url.host, req.url.path, req.headers["user-agent"], time.time()))
    m = mode.get(req.url.host, "ok")
    if m == "403":
        return httpx.Response(403, text="blocked")
    if m == "429once":
        mode[req.url.host] = "ok"
        return httpx.Response(429, headers={"Retry-After": "1"})
    if m == "missing":
        return httpx.Response(200, json={"error": {"code": "missingtitle"}})
    if m == "short" and req.url.path == "/api.php":
        return httpx.Response(200, json={"parse": {"text": {"*": "<p>tiny</p>"}}})
    if req.url.path == "/api.php":
        return httpx.Response(200, json={"parse": {"text": {"*": BODY}}})
    if req.url.path == "/index.php":
        return httpx.Response(200, text="Ziggy: " + "candy " * 200)
    return httpx.Response(404)


wikihttp.client = lambda: httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": wikihttp.WIKI_UA})
wikihttp.MIN_INTERVAL, wikihttp.MAX_WAIT = 0.3, 5

# pacing + honest UA
for p in ("A", "B", "C"):
    assert R.fandom_page("lazytown", f"{p}/Transcript")[1] == "ok"
t = [x[3] for x in seen]
assert t[2] - t[0] >= 0.55, t
assert all(x[2].startswith("EpisodeID/") and "Mozilla" not in x[2] for x in seen)
assert all(x[1] == "/api.php" for x in seen), "API only"

# a missing page: one request, "missing"
seen.clear(); mode["m.fandom.com"] = "missing"
assert R.fandom_page("m", "Nope/Transcript") == (None, "missing") and len(seen) == 1

# API answers without usable text: then the raw page
seen.clear(); mode["s.fandom.com"] = "short"
text, st = R.fandom_page("s", "Odd/Transcript")
assert st == "ok" and [x[1] for x in seen] == ["/api.php", "/index.php"], seen

# 429 with Retry-After: 1 → waits a second and gets it
seen.clear(); mode["r.fandom.com"] = "429once"
t0 = time.time()
assert R.fandom_page("r", "X/Transcript")[1] == "ok" and time.time() - t0 >= 0.95 and len(seen) == 2

# 403: one request (backoff 15 s > MAX_WAIT 5 → stop), no other URLs; then the wiki is
# left alone until the backoff ends — no request at all
seen.clear(); mode["b.fandom.com"] = "403"
assert R.fandom_page("b", "Y/Transcript") == (None, "error") and len(seen) == 1
assert R.LAST_WIKI_ERROR["why"] == "HTTP 403"
assert R.fandom_page("b", "Z/Transcript") == (None, "error") and len(seen) == 1, "no request while backing off"
assert 10 < wikihttp.blocked_for("b.fandom.com") <= 15
print("PASS")
