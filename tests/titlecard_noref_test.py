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

# The S10 screenshot: dialogue matched ONE episode across the whole two-episode file, so
# both cards land in the same segment. The second must split it, not be dropped.
EPS2 = [(10, 1, "Pups Save the Wacky Water Skiers"), (10, 2, "Pups Save the Mayor's Assistant"),
        (10, 3, "Pups Save a High-Flying Hen"), (10, 4, "Pups Save a Sloth")]
idx = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS + EPS2])
refs.update({"S10E01": "x", "S10E02": "x", "S10E03": "x"})
for name, exp, dlg, conf, c1, c2 in (
        ("S10E01E02", ["S10E01", "S10E02"], "S10E01", "high", "PUPS SAVETHE WAQLY WATERSKIERS", "PUPS SAVE THE MAYORS ASSISTANT"),
        ("S10E03E04", ["S10E03", "S10E04"], "S10E02", "high", "PUPS SAVEA HIGH-FLYING HEN", "PUPS SAVE A SLOTH")):
    f, got = run(exp, 1397, [seg(0, 1397, dlg, conf)], [(c1, 48), (c2, 745)])
    print(name, f["status"], got)
    assert [c for c, *_ in got] == exp and f["status"] == "OK", (name, f["segments"])
    assert 650 <= got[0][2] <= 700, got  # split ~ where the second episode starts (745 - 48)
print("PASS (single-segment files)")

# S01E13E14 (log): dialogue for E13 ends at 12:23, nothing matched after; E14's card at 11:53
# falls just inside E13's stretch (dialogue windows run into the next episode).
EPS3 = [(1, 13, "Pups Save the Circus"), (1, 14, "Pup a Doodle Do")]
idx = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS + EPS2 + EPS3])
refs.update({"S01E13": "x"})
f, got = run(["S01E13", "S01E14"], 1395, [seg(0, 743, "S01E13", "high", .4)],
             [("PUPS SAVE THE CIRCUS", 52), ("PUP DOODLE DO", 713)])
print("S01E13E14", f["status"], got)
assert [c for c, *_ in got] == ["S01E13", "S01E14"] and f["status"] == "OK" and got[1][2] == 1395, f["segments"]
print("PASS (card inside the previous stretch)")

# The 6 Oct screenshot: the first episode's dialogue matched nothing, so the file's first
# stretch of dialogue is the SECOND episode. The first card (0:48) must fill the opening
# gap, not relabel the second episode; the second card then confirms the second episode.
EPS4 = [(3, 45, "Pups Raise the Paw Patroller"), (3, 46, "Pups Save the Crows"),
        (10, 26, "Pups Save a Baby Caribou"), (10, 27, "Pups Save Luke and His Luke-Alike"), (4, 41, "Pups Save a Hoot")]
idx = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS + EPS2 + EPS3 + EPS4])
refs.update({"S03E46": "x", "S04E41": "x"})
for name, exp, dsegs, cards in (
        ("S03E45E46", ["S03E45", "S03E46"], [seg(653, 1341, "S03E46", "high", .4)],
         [("PUPS RAISETHE PAW PATROLLER", 44), ("PUPS SAVE THE CROWS", 700)]),
        ("S10E26E27", ["S10E26", "S10E27"], [seg(698, 1397, "S04E41", "high", .2)],
         [("PUPS SAVEA BABY CARIBOU", 48), ("PUPS SAVE LUKE AND HIS LUKE-ALIKE", 746)]),
        # second card not found: first half still gets E26, second half is left for review
        ("S10E26E27 one card", ["S10E26", "S10E27"], [seg(698, 1397, "S04E41", "high", .2)],
         [("PUPS SAVEA BABY CARIBOU", 48)])):
    pipeline._discount_unreferenced(dsegs, exp, 1397, refs)
    f = {"expected": exp, "duration": 1397, "text_source": "embedded", "segments": dsegs}
    pipeline._apply_title_cards(f, [{**idx.read(t), "time": at} for t, at in cards])
    got = [(sg["code"], round(sg["start"]), round(sg["end"])) for sg in f["segments"]]
    print(name, f["status"], got)
    assert got[0][0] == exp[0] and got[0][1] == 0, got
    if len(cards) == 2:
        assert [c for c, *_ in got] == exp and f["status"] == "OK", f["segments"]
    else:
        assert f["status"] == "LOW_CONFIDENCE" and f["segments"][1]["code"] != "S10E26", f["segments"]
print("PASS (card in the opening gap)")
