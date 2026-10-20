"""A two-part story in one file with one title card: "Diesel's Ghostly Christmas (1)"
and "(2)" in "S19E19E20". The card reads the story's title (it fits both parts), and
the dialogue matched S19E20 across the whole file (one reference holds the story).
The card confirms both of the filename's parts — not S19E19 alone with S19E20 left
unaccounted for."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pipeline, titlecard  # noqa: E402

EPS = [(19, 18, "Lost Property"), (19, 19, "Diesel's Ghostly Christmas (1)"),
       (19, 20, "Diesel's Ghostly Christmas (2)"), (19, 21, "Thomas the Babysitter")]
idx = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS],
                           series_names=["Thomas & Friends"])


def run(expected, segs, card, at=1.0, dur=1050.0):
    f = {"expected": expected, "duration": dur, "text_source": "whisper", "segments": segs}
    f["status"], f["note"] = pipeline.classify(expected, segs, "whisper")
    notes = pipeline._apply_title_cards(f, [{**idx.read(card), "time": at, "frames": 4}])
    return f, [(sg["code"], sg["confidence"]) for sg in f["segments"]], notes


r = idx.read("DIESEL'S GHOSTLY CHRISTMAS Writtenby")
assert r["partial"] and set(r["candidates"]) == {"S19E19", "S19E20"}, r

seg = lambda code, conf="high": [{"start": 0.0, "end": 1050.0, "code": code, "score": .3,  # noqa: E731
                                  "confidence": conf, "alternatives": []}]
for code, conf in (("S19E20", "high"), ("S19E19", "high"), ("S19E20", "low")):
    f, got, notes = run(["S19E19", "S19E20"], seg(code, conf), "DIESEL'S GHOSTLY CHRISTMAS Writtenby")
    print(" ", code, conf, "→", f["status"], got, notes)
    assert f["status"] == "OK" and got == [("S19E19", "high"), ("S19E20", "high")], (got, f.get("note"))
    assert "one reference" in f["segments"][0]["why"]
print("PASS (one card confirms both parts)")

# Only one part in the file (named S19E19, ~half the length): unchanged behaviour.
f, got, _ = run(["S19E19"], [{"start": 0.0, "end": 525.0, "code": "S19E20", "score": .3, "confidence": "high",
                              "alternatives": []}], "DIESEL'S GHOSTLY CHRISTMAS", dur=525.0)
assert len(got) == 1 and "story" not in f["segments"][0], got  # one part named: nothing to split
# The dialogue matched only the first half: the second half's evidence is still its own.
f, got, _ = run(["S19E19", "S19E20"], [{"start": 0.0, "end": 500.0, "code": "S19E20", "score": .3,
                                         "confidence": "high", "alternatives": []}], "DIESEL'S GHOSTLY CHRISTMAS")
assert len(got) == 1 and "story" not in f["segments"][0], got
print("PASS (other cases unchanged)")

# The card says which part: "Part Two" under the title (Steve's S07E12 file, really
# part 2 of the special) reads as S19E20 alone.
L = [["DIESEL'S GHOSTLY", .968, .075, .2], ["CHRISTMAS", .991, .072, .3], ["PartTwo", .954, .083, .4],
     ["writtenby", .953, .067, .5], ["Beckyoverton", .968, .064, .6]]
r = idx.match_frame(L, 34)
assert r["code"] == "S19E20" and not r["partial"] and r["part"] == 2, r
assert idx.match_frame(L[:2] + L[3:], 34)["partial"], "no marker: still either part"
for marker, code in (("PART 1", "S19E19"), ("Part I", "S19E19"), ("PART ONE", "S19E19"), ("Pt. 2", "S19E20")):
    assert idx.match_frame(L[:2] + [[marker, .99, .06, .4]], 34)["code"] == code, marker
assert idx.match_frame(L[:2] + [["PART THREE", .99, .06, .4]], 34)["partial"], "no such part: unchanged"
for t in ("APARTMENT", "party two", "Part Ten"):
    assert titlecard._part_no(titlecard._CARD_PART.search(t)) is None, t
# Applied to a file named as some other episode, it's a plain card for S19E20.
print("PASS (part marker on the card)")
