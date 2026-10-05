"""Episodes with no reference yet (the PAW Patrol S07–S09 screenshot).

With no reference for the filename's episode, dialogue can only match the closest
other episode (often a similar plot). That must not outvote title card + filename,
and those weak pieces must not be cut out of the file as separate episodes.
"""
import os
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_tcn"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pipeline, titlecard  # noqa: E402

EPS = [(2, 23, "Pups Save the Elephants"), (3, 25, "Pups Save the Yodelers"),
       (4, 4, "Pups Save the Cat Show"), (4, 28, "Sea Patrol: Pups Save a Shark"),
       (4, 47, "Pups Save the Mermaids"), (5, 24, "Pups Save an Upset Elephant"),
       (7, 22, "Moto Pups: Pups vs. the Ruff-Ruff Pack Part 2"), (7, 34, "Moto Pups: Pups vs. the Ruff-Ruff Pack"),
       (7, 37, "Moto Pups: Pups Save the Donuts"), (7, 41, "Moto Pups: Pups Save a Sneezy Chase"),
       (8, 12, "Pups Save the Hiding Elephants"), (8, 13, "Pups Save a Yodeler"),
       (9, 31, "Aqua Pups: Pups Save a Merdinger"), (9, 32, "Aqua Pups: Pups Save the Whale Patroller")]
idx = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS])
# references exist for the older episodes only
refs = {f"S{s:02d}E{e:02d}": "x" for s, e, _ in EPS if s < 7 or (s, e) in ((7, 22), (7, 37))}


def seg(a, b, code, conf="high", score=.2):
    return {"start": a, "end": b, "code": code, "score": score, "confidence": conf, "alternatives": []}


def run(expected, dur, segs, cards, runtimes=None):
    pipeline._discount_unreferenced(segs, expected, dur, refs)
    f = {"expected": expected, "duration": dur, "text_source": "embedded", "segments": segs}
    f["status"], f["note"] = pipeline.classify(expected, segs, "embedded")
    pipeline._apply_title_cards(f, [{**idx.read(t), "time": at} for t, at in cards], runtimes)
    return f, [(sg["code"], round(sg["start"]), round(sg["end"])) for sg in f["segments"]]


# Row 2: S08E12E13 — dialogue S02E23 / S05E24 / S03E25 (all "high"), cards confirm the filename.
f, got = run(["S08E12", "S08E13"], 1393,
             [seg(0, 428, "S02E23"), seg(428, 698, "S05E24"), seg(698, 1393, "S03E25")],
             [("PUPS SAVETHE HIDING ELEPHANTS", 45), ("PUPS SAVEA YODELER", 706)])
print("S08E12E13:", f["status"], got, "|", f["note"])
assert f["status"] == "OK" and [c for c, *_ in got] == ["S08E12", "S08E13"], got
assert "no reference" in f["segments"][0]["why"]

# Row 4: S09E31E32 — second half unmatched; its card sits where the dialogue matched nothing.
f, got = run(["S09E31", "S09E32"], 1390, [seg(0, 293, "S04E47"), seg(293, 653, "S04E28")],
             [("PUPS PUPS SAVEA MERDINGER", 42), ("AQUA PUPS PUPS SAVE THE WHALE PATROLLER", 706)])
print("S09E31E32:", f["status"], got)
assert f["status"] == "OK" and [c for c, *_ in got] == ["S09E31", "S09E32"], got

# Row 4 without the second card: no evidence for S09E32 → left alone (review), never renamed/split.
f, got = run(["S09E31", "S09E32"], 1390, [seg(0, 293, "S04E47"), seg(293, 653, "S04E28")],
             [("PUPS PUPS SAVEA MERDINGER", 42)])
print("S09E31E32 (one card):", f["status"], got, "|", f["note"])
assert f["status"] == "LOW_CONFIDENCE" and f.get("hold"), f

# Row 1: "S07E34" file is really two episodes; the second card is in the unmatched gap.
f, got = run(["S07E34"], 2811, [seg(0, 1463, "S07E22"), seg(2318, 2811, "S07E37", "low", .15)],
             [("MOT PUPS PUPSVSTHERUFF-RUFFPACK", 44), ("MOTO PUPS PUPS SAVE A SNEEZY CHASE", 1490)],
             {"S07E41": 22})
print("S07E34 file:", f["status"], got)
assert [c for c, *_ in got] == ["S07E34", "S07E41"] and all(sg["confidence"] == "high" for sg in f["segments"]), got

# No card at all: dialogue pointing elsewhere is no longer a confident mismatch.
segs = [seg(0, 690, "S02E23", score=.3)]
pipeline._discount_unreferenced(segs, ["S08E12"], 690, refs)
assert segs[0]["confidence"] == "low", segs
# …but it still is when the filename's episode does have a reference.
segs = [seg(0, 690, "S02E23", score=.3)]
pipeline._discount_unreferenced(segs, ["S05E24"], 690, refs)
assert segs[0]["confidence"] == "high", segs

# …and when the dialogue plainly is that other episode (a real duplicate), it still counts.
segs = [seg(0, 690, "S02E23", score=.6)]
pipeline._discount_unreferenced(segs, ["S08E12"], 690, refs)
assert segs[0]["confidence"] == "high", segs

# Where title cards are looked for: gap starts and the filename's episode positions.
st = titlecard.episode_starts(1390, [seg(0, 293, "S04E47"), seg(293, 653, "S04E28")], 11, 2)
print("scan starts S09E31E32:", st)
assert any(600 <= x <= 700 for x in st), st
st = titlecard.episode_starts(2811, [seg(0, 1463, "S07E22"), seg(2318, 2811, "S07E37")], 11, 1)
print("scan starts S07E34 file:", st)
assert any(1400 <= x <= 1463 for x in st), st
print("PASS")
