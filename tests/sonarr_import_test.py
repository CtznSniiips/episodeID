"""Scanning only some files merges into the last scan (the plan keeps every other
file); Sonarr's import webhook scans just the imported file and flags the series when
its content doesn't match its name; Compare with Sonarr lists where Sonarr's file list
and EpisodeID's disagree."""
import os
import random
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_sonarr"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import autocheck, config, db, jobs, main, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

random.seed(3)
root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
TEXT = {n: " ".join("".join(random.choice("bcdfgklmnprstvz") + random.choice("aeiou") for _ in range(3))
                    for _ in range(900)) for n in range(1, 7)}
name = lambda n: f"Season 1/Show - S01E{n:02d} - Ep {n} WEBDL-1080p.mkv"  # noqa: E731
CONTENT = {name(n): n for n in range(1, 5)}
for rel in CONTENT:
    (root / rel).write_bytes(b"x" * 100)


def fake_dialogue(v, ctx=None, allow_whisper=True):
    n = CONTENT[str(Path(v).relative_to(root))]
    w = TEXT[n].split()
    cues = [[i * 12.0, i * 12.0 + 10, " ".join(w[i * 8:i * 8 + 8])] for i in range(len(w) // 8)]
    return {"source": "embedded", "duration": 1400.0, "cues": cues}


pipeline.get_dialogue = fake_dialogue
pipeline.cached_dialogue = lambda v: fake_dialogue(v) if str(Path(v).relative_to(root)) in CONTENT else None
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
db.update_series(sid, episodes=[{"season": 1, "episode": n, "title": f"Ep {n}"} for n in range(1, 7)],
                 options={"title_cards": "off"})
for n in range(1, 7):
    references._save(1, f"S01E{n:02d}", {"source": "manual", "text": TEXT[n]})
ctx = lambda kind: JobContext(db.add_job(kind, sid, {}), sid)  # noqa: E731

pipeline.job_scan(ctx("scan"), {})
assert len(db.latest_scan(sid)["results"]["files"]) == 4

# 1. Scanning one file keeps the other three in the scan and the plan.
pipeline.job_scan(ctx("scan"), {"only": [name(2)]})
rels = [f["rel"] for f in db.latest_scan(sid)["results"]["files"]]
assert sorted(rels) == sorted(CONTENT), rels
assert len(db.latest_plan(sid)["items"]) == 4
print("PASS (partial scan merges)")

# 2. Sonarr imports S01E05 — but the file is really episode 6.
new = name(5)
(root / new).write_bytes(b"y" * 100)
CONTENT[new] = 6
queued = []
jobs.submit = lambda kind, s, params=None: queued.append((kind, s, params)) or len(queued)
c = TestClient(main.app)
assert c.post("/api/sonarr/webhook", json={"eventType": "Test"}).json()["ok"]
r = c.post("/api/sonarr/webhook", json={"eventType": "Download", "series": {"tvdbId": 1, "path": "/tv/Show"},
                                       "episodeFile": {"relativePath": new, "path": "/tv/Show/" + new}}).json()
print(" ", r)
assert [q[0] for q in queued] == ["scan"] and queued[0][2] == {"only": [new], "imported": True}
assert "ignored" in c.post("/api/sonarr/webhook", json={"eventType": "Download", "series": {"tvdbId": 99},
                                                        "episodeFile": {"relativePath": new}}).json()
assert "ignored" in c.post("/api/sonarr/webhook", json={"eventType": "Grab", "series": {"tvdbId": 1}}).json()
autocheck.record(db.get_series(sid))
res = pipeline.job_scan(ctx("scan"), queued[0][2])
print("  import scan:", res["files"])
assert res["files"][0]["rel"] == new and res["files"][0]["status"] != "OK"
assert len(db.latest_scan(sid)["results"]["files"]) == 5
s = c.get("/api/series").json()[0]
flag = s["file_check"]["imports"]
assert len(flag) == 1 and flag[0]["rel"] == new and not flag[0]["ok"], s["file_check"]
assert not (s["file_check"].get("pending") or {}).get("added"), "the imported file isn't 'new since the last scan'"
item = next(it for it in db.latest_plan(sid)["items"] if it["source"] == new)
assert item["kind"] in ("rename", "review", "aside"), item
# A full scan covers it: the flag goes.
pipeline.job_scan(ctx("scan"), {})
assert not c.get("/api/series").json()[0]["file_check"]["imports"]
print("PASS (webhook)")

# 3. Compare with Sonarr: Sonarr thinks E03's file is E04; E04's file isn't imported;
#    Sonarr lists a file that isn't there.
config.save_settings({"sonarr_url": "http://sonarr:8989", "sonarr_api_key": "k"})
SF = [{"id": 1, "relativePath": name(1)}, {"id": 2, "relativePath": name(2)}, {"id": 3, "relativePath": name(3)},
      {"id": 9, "relativePath": "Season 1/Gone.mkv"}, {"id": 5, "relativePath": new}]
SE = [{"seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 1}, {"seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 2},
      {"seasonNumber": 1, "episodeNumber": 4, "episodeFileId": 3}, {"seasonNumber": 1, "episodeNumber": 6, "episodeFileId": 9},
      {"seasonNumber": 1, "episodeNumber": 5, "episodeFileId": 5}]


def fake_get(url, headers=None, params=None, timeout=None):
    body = [{"id": 7, "tvdbId": 1, "title": "Show", "seriesType": "standard"}] if url.endswith("/series") else \
        SF if url.endswith("/episodefile") else SE
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


real = httpx.get
httpx.get = fake_get
try:
    r = c.get(f"/api/series/{sid}/sonarr-compare").json()
finally:
    httpx.get = real
for x in r["rows"]:
    print(f"  {x['kind']:<13} {x['rel']}: {x['why']}")
kinds = {x["rel"]: x["kind"] for x in r["rows"]}
assert kinds == {name(3): "differs", name(4): "not_imported", "Season 1/Gone.mkv": "sonarr_only"}, kinds
print("PASS")
