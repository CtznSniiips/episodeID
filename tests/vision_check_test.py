"""When the vision model is asked about a title card: OCR read none, only part of one,
or one that disagrees with both the filename and the dialogue. Its whole-title read
replaces OCR's read of the first episode's card; anything else keeps OCR's. Every
decision is logged."""
import os
import sys
from pathlib import Path

os.environ.setdefault("CONFIG_DIR", "/tmp/epid_vision/config")
os.environ.setdefault("CACHE_DIR", "/tmp/epid_vision/cache")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import llm, pipeline, titlecard  # noqa: E402

EPS = [{"season": 23, "episode": n, "title": t} for n, t in
       ((4, "The Other Big Engine"), (12, "Panicky Percy"), (13, "All Tracks Lead to Rome"),
        (14, "Percy's Lucky Day"), (15, "Percy's Lucky Penny"))]
idx = titlecard.TitleIndex(EPS, series_names=["Thomas & Friends"])


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m):
        self.lines.append(m)


answer = {"text": None, "calls": 0}


def fake_read(images, name):
    answer["calls"] += 1
    return answer["text"]


llm.read_title_card = fake_read
titlecard.vision_frames = lambda video, windows, k=4: [(70.0, b"jpg")] if windows else []


def check(cards, expected=("S23E04",), segs=(), text=None, windows=((0, 180),)):
    answer["text"], answer["calls"] = text, 0
    f = {"rel": "Season 23/x.mkv", "expected": list(expected), "title_scan": [list(w) for w in windows],
         "segments": [{"code": c} for c in segs]}
    ctx = Ctx()
    out = pipeline._vision_check(Path("/x.mkv"), f, cards, idx, "Thomas & Friends", ctx)
    return out, ctx.lines, answer["calls"]


card = lambda code, t=70.0, partial=False, **k: {"code": code, "text": code, "score": 1.0,  # noqa: E731
                                                 "partial": partial, "time": t, "frames": 3, **k}

# No card → asked; a whole title is used.
out, log, n = check([], text="Panicky Percy")
assert n == 1 and [c["code"] for c in out] == ["S23E12"] and "(vision)" in out[0]["text"], (out, log)
assert "OCR read no title card" in log[0], log
# Only a partial read → asked; the model's whole title replaces it.
out, log, n = check([card(None, partial=True, candidates=["S23E14", "S23E15"])], text="Percy's Lucky Penny")
assert n == 1 and out[0]["code"] == "S23E15", out
# OCR's card disagrees with filename and dialogue → asked; the model's read wins.
out, log, n = check([card("S23E13")], segs=["S23E12"], text="Panicky Percy")
assert n == 1 and out[0]["code"] == "S23E12" and "disagrees" in log[0], log
# … and if the model agrees with OCR, OCR's card stays.
out, log, n = check([card("S23E13")], segs=["S23E12"], text="All Tracks Lead to Rome")
assert out[0]["text"] == "S23E13" and "keeping it" in log[0], log
print("PASS (asked and used)")

# Not asked: the card agrees with the filename, or with the dialogue.
assert check([card("S23E04")])[2] == 0
assert check([card("S23E12")], segs=["S23E12"])[2] == 0
# A card for a second episode later in the file doesn't count for the first one.
out, log, n = check([card("S23E13", t=700.0)], text=None)
assert n == 1 and out[0]["code"] == "S23E13" and "saw no episode title" in log[0], log
print("PASS (not asked when OCR is fine)")

# The model's answer isn't usable: OCR's cards stay, and the log says why.
out, log, n = check([], text="Thomas & Friends")
assert out == [] and "matches no episode title" in log[0], log
out, log, n = check([card(None, partial=True, candidates=["S23E14", "S23E15"])], text="Percy's Lucky")
assert out[0]["partial"] and "fits several titles" in log[0], log
out, log, n = check([], windows=())
assert n == 0 and "no frame shows large text" in log[0], log


def boom(images, name):
    raise RuntimeError("Vision request failed (500)")


llm.read_title_card = boom
out, log, n = check([])
assert out == [] and "model error" in log[0], log
print("PASS (unusable answers logged)")
