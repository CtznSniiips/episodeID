"""TVDB renumbering (Phineas and Ferb: S05E39/S05E40 became S06E01/S06E02).
The episode list refreshes itself when stale, and references, hand-set episodes and
wiki pages move to the new numbers. A filename naming an episode the list doesn't
have isn't renamed."""
import json
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_renum"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, pipeline, planner, tvdb  # noqa: E402
from app.jobs import JobContext  # noqa: E402
from app.references import load_refs, manual_dir, refs_dir  # noqa: E402

OLD = [(5, 38, "Druselsteinoween"), (5, 39, "Night of the Living Candace Suits"), (5, 40, "Terrors of the AltSpace")]
NEW = [(5, 38, "Druselsteinoween"), (6, 1, "Night of the Living Candace Suits"), (6, 2, "Terrors of the AltSpace")]
ep = lambda xs: [{"season": s, "episode": e, "title": t, "aired": "2026-10-06"} for s, e, t in xs]  # noqa: E731

root = W / "media" / "tv" / "Phineas and Ferb"
(root / "Season 6").mkdir(parents=True)
db.init()
sid = db.add_series(81848, "Phineas and Ferb", "2007", str(root), None, options={
    "overrides": {"Season 6/x.mkv": ["S05E39"]}, "fandom_page_map": {"S05E40": "Terrors of the AltSpace/Transcript"}})
db.update_series(sid, episodes=ep(OLD), episodes_updated="2026-10-10T00:00:00+00:00")
refs_dir(81848).mkdir(parents=True)
(refs_dir(81848) / "S05E39.json").write_text(json.dumps({"source": "opensubtitles", "text": "candace suits " * 50, "title_verified": True}))
manual_dir(81848).mkdir(parents=True)
(manual_dir(81848) / "S05E40 my upload.txt").write_text("CANDACE: altspace " * 50)

# Planner guard, with the stale list: S06E01 (from the filename) isn't known → no rename.
f = {"rel": "Season 6/Phineas and Ferb - S06E01 - Night of the Living Candace Suits WEBDL-1080p.mkv",
     "expected": ["S06E01"], "status": "MISMATCH", "duration": 700,
     "segments": [{"start": 0, "end": 700, "code": "S05E39", "score": .6, "confidence": "high"}]}
(root / f["rel"]).touch()
it = planner.build_plan(db.get_series(sid), {"files": [f]})[0]
print(" stale list:", it["kind"], "—", it["reason"][:90])
assert it["kind"] == "review" and "isn't in EpisodeID's" in it["reason"]

tvdb.series_episodes = lambda _id: ep(NEW)
j = db.add_job("scan", sid, {})
s = pipeline._ensure_episodes(db.get_series(sid), JobContext(j, sid), max_age_h=12)
print("\n".join("  " + l for l in db.get_job(j)["log"].splitlines()))
codes = {f"S{e['season']:02d}E{e['episode']:02d}" for e in s["episodes"]}
assert {"S06E01", "S06E02"} <= codes and "S05E39" not in codes
refs = load_refs(81848)
assert "S06E01" in refs and "S06E02" in refs and "S05E39" not in refs and "S05E40" not in refs, refs.keys()
opts = s["options"]
assert opts["overrides"]["Season 6/x.mkv"] == ["S06E01"] and "S06E02" in opts["fandom_page_map"], opts

# Fresh list: not fetched again
calls = []
tvdb.series_episodes = lambda _id: calls.append(1) or ep(NEW)
pipeline._ensure_episodes(db.get_series(sid), JobContext(j, sid), max_age_h=12)
assert not calls, "a list younger than 12 h isn't fetched again"

it = planner.build_plan(db.get_series(sid), {"files": [{**f, "segments": [{**f["segments"][0], "code": "S06E01"}], "status": "OK"}]})[0]
print(" refreshed list:", it["kind"], "—", it["reason"])
assert it["kind"] == "ok"
print("PASS")
