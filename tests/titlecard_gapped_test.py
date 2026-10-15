"""Rubble & Crew "The Crew Builds a Butterfly Garden": a butterfly covers the middle of
the card, so OCR reads "BUILDSABUTT" and, below it, "RDEN". Two pieces of one title,
in order, ending the title: that's the episode — but only when exactly one title fits."""
import os
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_gapped"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import titlecard as T  # noqa: E402

titles = ["The Crew Builds a Butterfly Garden", "The Crew Builds a Bridge", "The Crew Builds a Bus Stop",
          "The Crew Builds a Train Station", "The Crew Builds a Sierra Sparkle Sign", "The Crew Builds a Rock Garden"]
idx = T.TitleIndex([{"season": 2, "episode": i + 1, "title": t} for i, t in enumerate(titles)])
credit = ["written by Denise Downer", 0.946, 0.05, 0.8]
frames = {  # the four frames from the title card check
    "11:42": [["BUILDSABUTT", 0.995, 0.103, 0.4], ["RDEN", 0.83, 0.061, 0.55], credit],
    "11:43": [["BUILDSABUT", 0.995, 0.097, 0.4], ["RDEN", 0.958, 0.061, 0.55], credit],
    "11:44": [["BUILDSABUT'", 0.952, 0.103, 0.4], credit],
    "11:45": [["BUILDSABUT", 0.996, 0.097, 0.4], ["DEN", 0.902, 0.061, 0.55], credit],
}
got = {t: (idx.match_frame(lines) or {}).get("code") for t, lines in frames.items()}
print(" ", got)
assert [got[t] for t in ("11:42", "11:43", "11:45")] == ["S02E01"] * 3 and got["11:44"] is None

# Pieces that fit more than one title decide nothing ("BUILDS A" … "DEN": either garden).
assert idx.match_frame([["BUILDSA", 0.99, 0.1, 0.4], ["DEN", 0.9, 0.06, 0.55]]) is None
# A credits frame isn't a title.
assert idx.match_frame([["EXECUTIVE PRODUCERS", 0.97, 0.047, 0.2], ["Jennifer Dodge", 0.98, 0.064, 0.4],
                        ["Toni Stevens", 0.99, 0.067, 0.6]]) is None
# Grouped into one clear card (three frames).
recs = [{"t": 702 + i, "lines": frames[k], "hit": idx.match_frame(frames[k])} for i, k in enumerate(frames)]
cards = T.cards_from_frames(recs)
print(" ", [(c["code"], c["time"], c.get("frames")) for c in cards])
assert len(cards) == 1 and cards[0]["code"] == "S02E01" and cards[0]["frames"] >= 2
print("PASS")
