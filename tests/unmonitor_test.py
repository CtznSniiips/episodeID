"""Unmonitor complete seasons in Sonarr: only seasons where every aired episode has a
correct file, the plan has nothing left for them, and Sonarr has every file."""
import json
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_unmon"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import config, db, main  # noqa: E402

root = W / "media" / "tv" / "Show"
root.mkdir(parents=True)
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
EPS = [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (3, 1), (3, 2), (4, 1), (5, 1)]
eps = [{"season": s, "episode": e, "title": f"E{s}{e}", "aired": "2099-01-01" if (s, e) == (3, 2) else "2020-01-01"}
       for s, e in EPS]
db.update_series(sid, episodes=eps)
code = lambda s, e: f"S{s:02d}E{e:02d}"  # noqa: E731
ok = lambda s, e: {"kind": "ok", "source": f"Season {s}/Show - {code(s, e)}.mkv", "expected": [code(s, e)]}  # noqa: E731
items = [ok(1, 1), ok(1, 2), ok(1, 3), ok(2, 1),
         {"kind": "rename", "source": "Season 2/Show - S02E03.mkv", "expected": ["S02E03"],
          "targets": [{"path": "Season 2/Show - S02E02.mkv", "codes": ["S02E02"]}]},
         ok(3, 1), ok(4, 1), ok(5, 1)]
db.add_scan(sid, {"files": [], "counts": {}})
db.add_plan(sid, None, [{**it, "id": i, "selected": True} for i, it in enumerate(items)])
config.save_settings({"sonarr_url": "http://sonarr:8989", "sonarr_api_key": "k"})

SERIES = {"id": 7, "tvdbId": 1, "title": "Show", "seasons": [{"seasonNumber": n, "monitored": n != 5} for n in range(0, 6)]}
SEP = [{"id": 100 + i, "seasonNumber": s, "episodeNumber": e, "monitored": s != 5,
        "hasFile": (s, e) not in ((4, 1), (3, 2))} for i, (s, e) in enumerate(EPS)]
sent = []


def fake_get(url, headers=None, params=None, timeout=None):
    if url.endswith("/series"):
        body = [SERIES]
    elif url.endswith("/series/7"):
        body = json.loads(json.dumps(SERIES))
    else:
        body = SEP
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def fake_request(method, url, json=None, headers=None, timeout=None):
    sent.append((method, url.split("/api/v3/")[1], json))
    return httpx.Response(202, json={}, request=httpx.Request(method, url))


httpx.get, httpx.request = fake_get, fake_request
c = TestClient(main.app)
st = c.get(f"/api/series/{sid}/seasons").json()
for r in st["seasons"]:
    print(f"  S{r['season']}: eligible={r['eligible']!s:<5} {r['have']}/{r['episodes']}  {r['why']}")
by = {r["season"]: r for r in st["seasons"]}
assert [n for n, r in by.items() if r["eligible"]] == [1]
assert "change" in by[2]["why"] and "S02E02" in by[2]["why"]
assert "haven't aired" in by[3]["why"] and "without a correct file" not in by[3]["why"]
assert "Sonarr has no file" in by[4]["why"]
assert by[5]["complete"] and not by[5]["eligible"] and "already unmonitored" in by[5]["why"]
assert by[1]["why"] == ""

r = c.post(f"/api/series/{sid}/unmonitor", json={"seasons": [1, 2]}).json()
print(" ", r, [(m, p) for m, p, _ in sent])
assert r["seasons"] == [1] and r["skipped"] == [2] and r["episodes"] == 3
put_series = next(b for m, p, b in sent if p == "series/7")
assert [x["monitored"] for x in put_series["seasons"] if x["seasonNumber"] == 1] == [False]
assert [x["monitored"] for x in put_series["seasons"] if x["seasonNumber"] == 2] == [True]
assert next(b for m, p, b in sent if p == "episode/monitor") == {"episodeIds": [100, 101, 102], "monitored": False}

allr = c.get("/api/seasons").json()
assert allr[0]["eligible"] == [1], allr
sent.clear()
r = c.post("/api/unmonitor", json={"series": {str(sid): [1]}}).json()
assert r[0]["seasons"] == [1], r
print("PASS")
