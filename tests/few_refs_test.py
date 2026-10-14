"""Rubble & Crew screenshot: one reference (S01E01) and every file "matched" it — with a
single reference there's no runner-up, and what all episodes share (the characters'
names, catchphrases, the theme song) made any episode look like it. Now word weights
come from the files' dialogue too, and with few references a match must be plainly
that episode."""
import os
import random
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_fewrefs"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import matcher as M  # noqa: E402
from app.config import get_settings  # noqa: E402

random.seed(7)
SHARED = ("rubble crew mix lucky charger wrench motor builder cove mayor greatway speed "
          "build it rubble on the double let's go dig crew").split()
THEME = "rubble and crew build it rubble and crew on the double we can build it " * 4


def words(n):
    return ["".join(random.choice("bcdfgklmnprstvz") + random.choice("aeiou") for _ in range(3)) for _ in range(n)]


def episode(own):  # ~22 minutes: shared words dominate, each episode has its own topic words
    out = [THEME]
    for _ in range(1100):
        out.append(random.choice(own) if random.random() < 0.3 else random.choice(SHARED))
    return " ".join(out)


EPS = {f"S01E{n:02d}": episode(words(40)) for n in range(1, 31)}
files = {c: t for c, t in EPS.items()}  # the files contain each episode's own dialogue


def cues(text, dur=1380.0):
    w = text.split()
    per = dur / max(1, len(w) // 8)
    return [(i * per, (i + 1) * per, " ".join(w[i * 8:i * 8 + 8])) for i in range(len(w) // 8)]


s = get_settings()
refs = {"S01E01": EPS["S01E01"]}
old = M.Matcher(refs, s)                       # before: references only
new = M.Matcher(refs, s, list(files.values()))  # now: the files' dialogue too


def high(m, code):
    segs = m.match_file(cues(files[code]), 1380.0)["segments"]
    return [(sg["code"], sg["confidence"], round(sg["score"], 2)) for sg in segs]


print("  before, S01E07 file:", high(old, "S01E07"))
print("  now,    S01E07 file:", high(new, "S01E07"))
print("  now,    S01E01 file:", high(new, "S01E01"))
others = [c for c in EPS if c != "S01E01"]
assert sum(1 for c in others if any(conf == "high" for _c, conf, _s in high(old, c))) >= 20, "the bug: most matched E01"
assert not any(conf == "high" for c in others for _c, conf, _s in high(new, c)), [high(new, c) for c in others[:3]]
assert any(code == "S01E01" and conf == "high" for code, conf, _s in high(new, "S01E01")), "the real E01 still matches"

# With plenty of references nothing changes in kind: every file still finds its own episode.
full = M.Matcher(dict(EPS), s, list(files.values()))
assert all(high(full, c)[0][:2] == (c, "high") for c in EPS), [high(full, c) for c in list(EPS)[:3]]
print("PASS")
