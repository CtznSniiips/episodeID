"""The Office: TVDB splits hour-long stories into parts ("Dunder Mifflin Infinity (1)" =
S04E03, "(2)" = S04E04), but the subtitle for the whole hour is filed under one part.
A correctly named S04E03E04 file then matched "S04E04" from start to finish and was
renamed to S04E04 alone; a whole-story file named for one part was pushed aside as a
duplicate. A match far longer than the part it names, against a story the filename
also names, confirms the filename's parts instead."""
import os
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_story"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pipeline  # noqa: E402
from app.matcher import classify  # noqa: E402

E = {(4, 3): "Dunder Mifflin Infinity (1)", (4, 4): "Dunder Mifflin Infinity (2)", (4, 5): "Launch Party (1)",
     (4, 6): "Launch Party (2)", (4, 7): "Money (1)", (4, 8): "Money (2)", (4, 9): "Local Ad",
     (7, 24): "Dwight K. Schrute, (Acting) Manager", (7, 25): "Search Committee (1)", (7, 26): "Search Committee (2)"}
eps = {f"S{s:02d}E{e:02d}": {"season": s, "episode": e, "title": t, "runtime": 22} for (s, e), t in E.items()}


def run(expected, dur, code, conf="high"):
    segs = [{"start": 0, "end": dur, "code": code, "score": .5, "confidence": conf, "alternatives": []}]
    segs = pipeline._story_parts(segs, expected, eps, dur)
    status, _ = classify(expected, segs, "whisper small")
    return status, [(s["code"], round(s["start"]), round(s["end"]), s["confidence"]) for s in segs], segs


for exp, code in ((["S04E03", "S04E04"], "S04E04"), (["S04E05", "S04E06"], "S04E06"), (["S04E03", "S04E04"], "S04E03")):
    st, got, segs = run(exp, 2523, code)
    print(" ", exp, "→", st, got)
    assert st == "OK" and [c for c, *_ in got] == exp, got
    assert "whole of" in segs[0]["why"]

# Whole story in a file named for one part (Search Committee, 56 min, named S07E25): review, with the parts named.
st, got, segs = run(["S07E25"], 3354, "S07E26")
print("  S07E25 (56 min) →", st, got, "|", segs[0]["why"][:110])
assert st != "OK" and got[0][3] == "low" and "S07E25E26" in segs[0]["why"]

# Ordinary cases untouched: a single part matching its own length; a story without parts; a real mismatch.
assert run(["S04E04"], 1320, "S04E04")[0] == "OK"
assert run(["S04E09"], 1320, "S04E07")[1][0][0] == "S04E07"            # Local Ad vs Money: not one story
assert run(["S04E03", "S04E04"], 2523, "S04E07")[1][0][0] == "S04E07"  # other story: left alone
assert run(["S04E07"], 1320, "S04E08")[1][0][:1] == ("S04E08",), "a part-length match still tells parts apart"
print("PASS")
