"""One episode in several files ("… - Part 1.mkv", "… - Part 2.mkv"): offered as one join
instead of keeping one part and moving the rest aside as duplicates. Joined losslessly,
checked before anything moves, undoable.

Never joined: parts whose names give each its own TVDB episode (Regular Show's "A Regular
Epic Final Battle, Part 1/2/3" = S08E27/E28/E29, three 10-minute episodes on TVDB) — even
when one reference (a transcript of the whole story) matches all three — and a two-part
story TVDB counts as two episodes. A reference several times longer than a file's
dialogue doesn't push files named for other episodes aside as duplicates."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_join"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, executor, pipeline, planner  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Regular Show"
(root / "Season 3").mkdir(parents=True)
(root / "Season 8").mkdir(parents=True)
(root / "Season 1").mkdir(parents=True)


def clip(path, secs, color, size="320x180"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=10:d={secs}",
                    "-f", "lavfi", "-i", f"sine=f=440:r=8000:d={secs}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "20", "-c:a", "aac", "-shortest", str(path)], check=True)


A1 = "Season 3/Regular Show - S03E04 - Terror Tales of the Park WEBDL-1080p - Part 1.mkv"
A2 = "Season 3/Regular Show - S03E04 - Terror Tales of the Park WEBDL-1080p - Part 2.mkv"
B1 = "Season 8/Regular Show - S08E27 - A Regular Epic Final Battle, Part 1 HDTV-1080p.mkv"
B2 = "Season 8/Regular Show - S08E28 - A Regular Epic Final Battle, Part 2 HDTV-1080p.mkv"
B3 = "Season 8/Regular Show - S08E29 - A Regular Epic Final Battle, Part 3 HDTV-1080p.mkv"
Q1 = "Season 1/Regular Show - S01E05 - The Quest (Part 1).mkv"   # two TVDB episodes
Q2 = "Season 1/Regular Show - S01E06 - The Quest (Part 2).mkv"
for rel, secs, col in ((A1, 30, "red"), (A2, 20, "blue"), (B1, 25, "green"), (B2, 25, "yellow"), (B3, 20, "white"),
                       (Q1, 20, "gray"), (Q2, 20, "black")):
    clip(root / rel, secs, col)
(root / A2).with_suffix(".en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")

db.init()
sid = db.add_series(1, "Regular Show", "2010", str(root), None)
EPS = [(3, 4, "Terror Tales of the Park", 20), (8, 27, "A Regular Epic Final Battle (1)", 10),
       (8, 28, "A Regular Epic Final Battle (2)", 10), (8, 29, "A Regular Epic Final Battle (3)", 10),
       (1, 5, "The Quest (1)", 10), (1, 6, "The Quest (2)", 10)]
db.update_series(sid, episodes=[{"season": s, "episode": e, "title": t, "runtime": r} for s, e, t, r in EPS])


def f(rel, code, dur, conf="high"):
    exp = planner.parse_filename_episodes(Path(rel).name) if hasattr(planner, "parse_filename_episodes") else None
    from app.matcher import parse_filename_episodes
    exp = parse_filename_episodes(Path(rel).name)
    return {"rel": rel, "size": 1, "duration": dur, "text_source": "embedded", "expected": exp,
            "segments": [{"start": 0, "end": dur, "code": code, "score": .5, "confidence": conf, "alternatives": []}],
            "status": "OK" if code in exp else "MISMATCH", "note": ""}


scan = {"files": [f(A1, "S03E04", 30), f(A2, "S03E04", 20), f(B1, "S08E27", 25), f(B2, "S08E27", 25, "low"),
                  f(B3, "S08E27", 20, "low"), f(Q1, "S01E05", 20), f(Q2, "S01E06", 20)], "counts": {}}  # E28/E29: as the scan now leaves them (whole-story reference)
scan_id = db.add_scan(sid, scan)
items = planner.build_plan(db.get_series(sid), db.get_scan(scan_id)["results"])
for it in items:
    print(f"  {it['kind']:<6} sel={it['selected']!s:<5} {it.get('sources') or it['source']} -> "
          f"{[t['path'] for t in it.get('targets') or []]}")
joins = [it for it in items if it["kind"] == "join"]
assert len(joins) == 1, items
ja = joins[0]
assert ja["codes"] == ["S03E04"] and ja["sources"] == [A1, A2] and ja["selected"] and "names" in ja["reason"]
assert ja["targets"][0]["path"] == "Season 3/Regular Show - S03E04 - Terror Tales of the Park WEBDL-1080p.mkv"
assert not any(it["kind"] == "join" and B2 in it["sources"] for it in items), "S08E27–E29 are three TVDB episodes"
assert not any(it["kind"] == "join" and Q1 in it["sources"] for it in items), "two TVDB episodes stay apart"
pid = db.add_plan(sid, scan_id, items)

# The whole-story reference: S08E27's transcript covers all three parts, E28/E29 have none.
from app.pipeline import _discount_unreferenced  # noqa: E402
story = " ".join(f"line{i}" for i in range(3000))
segs = [{"start": 0, "end": 600, "code": "S08E27", "score": .6, "confidence": "high", "words": 1000}]
_discount_unreferenced(segs, ["S08E28"], 600, {"S08E27": story})
print("  whole-story reference:", segs[0]["confidence"], "|", segs[0]["why"][:90])
assert segs[0]["confidence"] == "low" and segs[0].get("broad_ref")
segs = [{"start": 0, "end": 600, "code": "S08E27", "score": .6, "confidence": "high", "words": 1000}]
_discount_unreferenced(segs, ["S08E28"], 600, {"S08E27": " ".join(["w"] * 1100)})
assert segs[0]["confidence"] == "high", "an episode-length reference still counts"
scan2 = {"files": [f(B1, "S08E27", 600), f(B2, "S08E27", 600, "low"), f(B3, "S08E27", 600, "low")], "counts": {}}
it2 = {it["source"]: it for it in planner.build_plan(db.get_series(sid), scan2)}
assert it2[B2]["kind"] != "aside" and it2[B3]["kind"] != "aside", [(k, v["kind"]) for k, v in it2.items()]

# Parts much longer together than TVDB's runtime: offered, not ticked.
long_scan = {"files": [f(A1, "S03E04", 1400), f(A2, "S03E04", 1400)], "counts": {}}
lj = next(it for it in planner.build_plan(db.get_series(sid), long_scan) if it["kind"] == "join")
assert not lj["selected"] and "TVDB gives S03E04 20 min" in lj["reason"], lj["reason"]

# Parts encoded differently can't be joined losslessly: refused, nothing changed.
odd = root / "Season 3" / "odd - Part 1.mkv"
before = sorted(p.name for p in (root / "Season 3").iterdir())
plan = db.get_plan(pid)
bad = [dict(it) for it in plan["items"]]
clip(odd, 5, "red", "640x360")
for it in bad:
    if it["kind"] == "join" and it["codes"] == ["S03E04"]:
        it["sources"] = [str(odd.relative_to(root)), A2]
bp = db.add_plan(sid, scan_id, bad)
try:
    executor.apply_plan(db.get_series(sid), db.get_plan(bp), JobContext(db.add_job("apply", sid, {}), sid))
    raise AssertionError("should refuse")
except executor.ApplyError as e:
    print("  refused:", str(e)[:100])
    assert "encoded differently" in str(e)
odd.unlink()
assert sorted(p.name for p in (root / "Season 3").iterdir()) == before

# Apply: joined, checked, parts kept in the backup folder; the plan updates itself.
j = db.add_job("apply", sid, {"plan_id": pid})
res = pipeline.job_apply(JobContext(j, sid), {"plan_id": pid})
print("  apply:", res)


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(p)], capture_output=True, text=True).stdout)


ta = root / ja["targets"][0]["path"]
print(f"  joined: {dur(ta):.1f}s")
assert abs(dur(ta) - 50) < 1
for rel in (A1, A2):
    assert not (root / rel).exists()
for rel in (B1, B2, B3):
    assert (root / rel).exists(), "separate TVDB episodes stay where they are"
bk = W / "media" / "tv" / "_episodeid_backup" / "Regular Show" / "joined_parts"
assert (bk / A2).exists() and (bk / A2).with_suffix(".en.srt").exists()
new = db.latest_plan(sid)["items"]
mine = [it for it in new if it["source"] == ja["targets"][0]["path"]]
assert len(mine) == 1 and mine[0]["kind"] == "ok", [(it["kind"], it["source"]) for it in new]

# Undo puts every part back.
ap = db.list_applies(sid)[0]
pipeline.job_undo(JobContext(db.add_job("undo", sid, {}), sid), {"apply_id": ap["id"]})
for rel in (A1, A2, B1, B2, B3):
    assert (root / rel).exists(), rel
assert not ta.exists()
print("PASS")

# A draft plan made before joins existed is made again when it's next shown — the join
# appears without a rescan, and ticks on unchanged rows carry over.
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402
old_items = [{"id": 0, "kind": "ok", "source": A1, "selected": False, "expected": ["S03E04"], "segments": []},
             {"id": 1, "kind": "aside", "source": A2, "selected": True, "expected": ["S03E04"], "segments": [],
              "targets": [{"path": "x"}]},
             {"id": 2, "kind": "ok", "source": Q1, "selected": False, "expected": ["S01E05"], "segments": []}]
sid2 = db.add_scan(sid, scan)
db.add_plan(sid, sid2, old_items)
got = TestClient(main.app).get(f"/api/series/{sid}/plan").json()
kinds = {(it["kind"], tuple(it.get("sources") or [it["source"]])) for it in got["items"]}
print("  old plan shown as:", sorted(k for k, _ in kinds))
assert ("join", (A1, A2)) in kinds and not any(k == "aside" and s == (A2,) for k, s in kinds)
assert all(it.get("pv") for it in got["items"])
html = TestClient(main.app).get("/").text
assert "/static/app.js?v=" in html and "/static/style.css?v=" in html
print("PASS (old plans rebuilt)")
