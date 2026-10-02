"""OpenSubtitles VIP accounts: login returns base_url vip-api.opensubtitles.com, which
answers searches with 401 {"errors":["Invalid domain"]}. Searches must use the main
API; downloads may use the VIP host and fall back if it refuses too. A rejected API
key must stop OpenSubtitles for the run instead of failing every episode."""
import os
import sys
from pathlib import Path

import httpx

os.environ.setdefault("CONFIG_DIR", "/tmp/epid_os/config")
os.environ.update(OPENSUBTITLES_API_KEY="k", OPENSUBTITLES_USERNAME="u", OPENSUBTITLES_PASSWORD="p")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import references  # noqa: E402

references.time.sleep = lambda s: None
hits = []


def server(vip_download_ok: bool, key_ok: bool = True):
    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        hits.append((host, path))
        if not key_ok:
            return httpx.Response(401, json={"errors": ["Invalid API key"], "status": 401})
        if path.endswith("/login"):
            return httpx.Response(200, json={"token": "t", "base_url": "vip-api.opensubtitles.com",
                                             "user": {"remaining_downloads": 1000, "vip": True}})
        if host == "vip-api.opensubtitles.com" and (path.endswith("/subtitles") or not vip_download_ok):
            return httpx.Response(401, json={"errors": ["Invalid domain"], "status": 401})
        if path.endswith("/subtitles"):
            return httpx.Response(200, json={"data": [{"id": "1", "attributes": {"files": [{"file_id": 9}]}}]})
        if path.endswith("/download"):
            return httpx.Response(200, json={"link": "https://dl.opensubtitles.org/x.srt", "remaining": 999})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


real_get = httpx.get
httpx.get = lambda url, **kw: httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                                             request=httpx.Request("GET", url))

for vip_ok in (True, False):
    hits.clear()
    os_ = references.OpenSubtitles()
    os_.client = httpx.Client(transport=server(vip_ok), headers=os_.client.headers)
    os_.login()
    res = os_.search(parent_imdb_id="1942683", season_number=2, episode_number=31)
    text = os_.download(9)
    print(f"vip download ok={vip_ok}:", hits)
    assert ("api.opensubtitles.com", "/api/v1/subtitles") in hits, "search must use the main API"
    assert ("vip-api.opensubtitles.com", "/api/v1/subtitles") not in hits
    assert "Hello" in text and len(res) == 1
    expected_dl = "vip-api.opensubtitles.com" if vip_ok else "api.opensubtitles.com"
    assert hits[-1] == (expected_dl, "/api/v1/download"), hits[-1]

# Rejected key → OpenSubtitlesAuthError (the fetch loop logs it once and stops using OpenSubtitles)
os_ = references.OpenSubtitles()
os_.client = httpx.Client(transport=server(True, key_ok=False), headers=os_.client.headers)
try:
    os_.login()
    raise SystemExit("expected an auth error")
except references.OpenSubtitlesAuthError as e:
    print("auth error:", e)
httpx.get = real_get
print("PASS")
