"""Ask AI on one file: the AI's pick lands on that file's row (marked AI, not ticked),
the rest of the plan and its ticks stay as they were; the button's job refuses when the
AI isn't set up."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_ai"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, llm, pipeline  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
db.update_series(sid, episodes=[{"season": 1, "episode": n, "title": f"Ep {n}", "overview": f"about {n}"}
                                for n in range(1, 5)])
A, B = "Season 1/Show - S01E01.mkv", "Season 1/Show - S01E02.mkv"
for r in (A, B):
    (root / r).write_bytes(b"x")
files = [{"rel": A, "size": 1, "duration": 600, "text_source": "embedded", "expected": ["S01E01"], "whole": [],
          "segments": [{"start": 0, "end": 600, "code": "S01E03", "score": .1, "confidence": "low", "alternatives": []}],
          "status": "LOW_CONFIDENCE", "note": ""},
         {"rel": B, "size": 1, "duration": 600, "text_source": "embedded", "expected": ["S01E02"], "whole": [],
          "segments": [{"start": 0, "end": 600, "code": "S01E04", "score": .5, "confidence": "high", "alternatives": []}],
          "status": "MISMATCH", "note": ""}]
sc = db.add_scan(sid, {"files": files, "counts": {}})
from app.planner import build_plan  # noqa: E402
items = build_plan(db.get_series(sid), db.get_scan(sc)["results"])
for it in items:
    if it["source"] == B:
        it["selected"] = False  # you unticked it
db.add_plan(sid, sc, items)

pipeline.get_dialogue = lambda v, ctx=None, allow_whisper=True: {
    "source": "embedded", "duration": 600, "cues": [[0, 5, "hello there"], [5, 10, "the dentist again"]]}
asked = []
llm.available = lambda: False
try:
    pipeline.job_ai_check(JobContext(db.add_job("ai_check", sid, {}), sid), {"source": A})
    raise AssertionError("should refuse without AI")
except RuntimeError as e:
    assert "isn't set up" in str(e)
llm.available = lambda: True
llm.judge = lambda text, cands, name: asked.append([c["code"] for c in cands]) or \
    {"code": "S01E01", "confidence": 0.8, "reason": "the dentist plot"}
res = pipeline.job_ai_check(JobContext(db.add_job("ai_check", sid, {}), sid), {"source": A})
print("  candidates:", asked, res)
assert "S01E01" in asked[0]
plan = db.latest_plan(sid)
ia = next(it for it in plan["items"] if it["source"] == A)
ib = next(it for it in plan["items"] if it["source"] == B)
assert ia["segments"][0]["llm"]["code"] == "S01E01" and not ia["selected"], ia
assert ib["selected"] is False, "your untick on another row stays"
assert len(db.latest_scan(sid)["results"]["files"]) == 2
print("PASS")
