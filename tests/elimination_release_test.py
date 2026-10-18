"""Process of elimination (the one unsettled file in a season and the one episode left;
a file between confirmed neighbours), and Sonarr's original release names as evidence."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_elim"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, pipeline, planner, sonarr  # noqa: E402
from app.matcher import classify, parse_filename_episodes  # noqa: E402

root = W / "media" / "tv" / "Show"
for n in (1, 2, 3, 4):
    (root / f"Season {n}").mkdir(parents=True)
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
TITLES = {(1, 1): "Pilot", (1, 2): "The Return", (1, 3): "Dunder Mifflin Infinity", (1, 4): "Launch Party", (1, 5): "Money",
          (2, 1): "Fun Run", (2, 2): "Survivor Man", (2, 3): "The Deposition",
          (3, 1): "Dinner Party", (3, 2): "Chair Model", (3, 3): "Night Out", (3, 4): "Goodbye Toby",
          (4, 1): "Branch Wars", (4, 2): "Business Trip", (4, 3): "Frame Toby"}
db.update_series(sid, episodes=[{"season": s, "episode": e, "title": t, "aired": "2010-01-01"} for (s, e), t in TITLES.items()])
series = db.get_series(sid)
code = lambda s, e: f"S{s:02d}E{e:02d}"  # noqa: E731


def f(rel, seg_code=None, conf="high"):
    exp = parse_filename_episodes(Path(rel).name)
    segs = [{"start": 0, "end": 1300, "code": seg_code, "score": .4, "confidence": conf, "alternatives": []}] if seg_code else []
    st, note = classify(exp, segs, "embedded")
    return {"rel": rel, "size": 1, "duration": 1300, "text_source": "embedded", "expected": exp,
            "segments": segs, "status": st, "note": note}


nm = lambda s, e: f"Season {s}/Show - {code(s, e)} - {TITLES[(s, e)]}.mkv"  # noqa: E731
files = [f(nm(1, 1), "S01E01"), f(nm(1, 2), "S01E02"), f(nm(1, 3), "S01E05", "low"), f(nm(1, 4), "S01E04"),
         f(nm(1, 5), "S01E05"),
         f(nm(2, 1), "S02E01"), f(nm(2, 2), "S02E02"), f("Season 2/Show - unknown episode.mkv"),
         f(nm(3, 1), "S03E01"), f(nm(3, 2), "S03E04", "low"), f(nm(3, 3), "S03E03"),   # S03E04: no file
         f(nm(4, 1), "S04E03", "low"), f(nm(4, 2), "S04E01", "low")]                  # two unsettled, three left
for x in files:
    (root / x["rel"]).write_bytes(b"x")
items = {it["source"]: it for it in planner.build_plan(series, {"files": files})}
for k, it in items.items():
    print(f"  {it['kind']:<7} {k.split('/', 1)[1]:<38} {it.get('reason', '')[:100]}")
a = items[nm(1, 3)]
assert a["kind"] == "ok" and a.get("elimination"), a                      # only one left in season 1, as named
b = items["Season 2/Show - unknown episode.mkv"]
assert b["kind"] == "rename" and b["codes"] == ["S02E03"] and not b["selected"] and b.get("elimination"), b
c = items[nm(3, 2)]
assert c["kind"] == "ok" and "either side" in c["reason"], c       # S03E01 and S03E03 confirmed around it
assert items[nm(4, 1)]["kind"] == "review" and items[nm(4, 2)]["kind"] == "review"
print("PASS (elimination)")

# Sonarr release names.
REL = {nm(4, 1): ["Show.S04E01.Branch.Wars.720p.HDTV.x264-GRP"],              # confirms its name
       nm(4, 2): ["Show.S04E02.Frame.Toby.720p.HDTV.x264-GRP"],               # names another episode
       nm(2, 1): ["Show.S02E01.720p.HDTV.x264-GRP"]}                          # number only: nothing
sonarr.release_names = lambda tvdb_id: REL
files2 = [f(nm(4, 1), "S04E03", "low"), f(nm(4, 2)), f(nm(2, 1), "S02E01", "low"),
          f(nm(1, 1), "S01E04")]                                              # dialogue sure of another ep
REL[nm(1, 1)] = ["Show.S01E01.Pilot.1080p.WEB-DL"]


class Ctx:
    def log(self, m): print("   ", m)


pipeline._release_name_pass(series, files2, Ctx())
by = {x["rel"]: x for x in files2}
print("  ", {k.split("/", 1)[1]: (v["status"], [(s["code"], s["confidence"]) for s in v["segments"]]) for k, v in by.items()})
assert by[nm(4, 1)]["status"] == "OK" and by[nm(4, 1)]["segments"][0]["evidence"] == "release name"
assert by[nm(4, 2)]["segments"][0]["code"] == "S04E03" and by[nm(4, 2)]["segments"][0]["confidence"] == "low"
assert by[nm(4, 2)]["status"] != "OK"
assert by[nm(2, 1)]["status"] != "OK" and "release" not in by[nm(2, 1)]
assert by[nm(1, 1)]["segments"][0]["code"] == "S01E04", "a confident dialogue match isn't overruled"
items2 = {it["source"]: it for it in planner.build_plan(series, {"files": files2})}
assert items2[nm(4, 1)]["kind"] == "ok"
assert items2[nm(4, 2)]["kind"] == "review" and "S04E03" in (items2[nm(4, 2)].get("suggested") or [])
print("PASS")
