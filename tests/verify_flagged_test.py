"""Flagged OpenSubtitles references are settled by comparing dialogue.

TVDB S01E14 is "Pups Save the Bunnies"; OpenSubtitles/IMDb labels its S01E14 subtitle
"Pup-Tacular", which is S01E24 on TVDB. S01E24 has a wiki transcript.
  A) the subtitle's dialogue is Pup-Tacular's        → discarded for S01E14
  B) the dialogue is genuinely S01E14's              → verified (label was IMDb's order)
  C) label points at an episode with no reference    → stays flagged
"""
import json
import os
import random
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_verify"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import references  # noqa: E402

random.seed(3)
COMMON = "pups ryder chase marshall skye rubble rocky zuma bay adventure mayor help".split()
TITLES = {i: f"Pups Episode {chr(64 + i)}" for i in range(1, 31)}
TITLES.update({14: "Pups Save the Bunnies", 24: "Pup-Tacular", 15: "Pups Save a Toof", 30: "Pups Save a Hoot"})


def dialogue(n_words=900):
    own = ["".join(random.choice("bcdfghklmnprstvz") + random.choice("aeiou") for _ in range(3)) for _ in range(70)]
    return " ".join(random.choice(own + COMMON) for _ in range(n_words))


def transcript(text):
    w = text.split()
    return "\n".join("Ryder: " + " ".join(w[i:i + 9]) for i in range(0, len(w), 9))


TEXT = {i: dialogue() for i in TITLES}
eps = [{"season": 1, "episode": i, "title": t} for i, t in TITLES.items()]


class Ctx:
    def log(self, m): print("   ", m)


def setup(e14_text):
    if W.exists():
        shutil.rmtree(W)
    d = W / "cache" / "refs" / "1"
    d.mkdir(parents=True)
    for i in list(range(1, 11)) + [24]:  # wiki transcripts for some episodes incl. Pup-Tacular
        (d / f"S01E{i:02d}.json").write_text(json.dumps({"source": "fandom", "text": transcript(TEXT[i])}))
    os_ref = lambda text, title, code: {"source": "opensubtitles", "text": text, "via": "series + episode number",  # noqa: E731
                                        "os_title": title, "os_number": "S01E14" if "Bunnies" in title or title == "Pup-Tacular" else "S01E15",
                                        "title_verified": False, "numbering_conflict": True,
                                        "os_title_code": code}
    (d / "S01E14.json").write_text(json.dumps(os_ref(e14_text, "Pup-Tacular", "S01E24")))
    (d / "S01E15.json").write_text(json.dumps(os_ref(TEXT[15], "Pups Save a Hoot", "S01E30")))
    references._ref_cache.clear()


series = {"tvdb_id": 1, "episodes": eps}
assert references.title_code(eps, "Pup-Tacular") == "S01E24"
assert references.title_code(eps, "pup tacular") == "S01E24"
print("wording:", references.describe_conflict(
    {"os_title": "Pup-Tacular", "os_number": "S01E14", "os_title_code": "S01E24"}, "S01E14", "Pups Save the Bunnies"))

print("A) subtitle is really Pup-Tacular's dialogue")
setup(" ".join(w for w in TEXT[24].split() if random.random() > 0.2))
r = references.verify_flagged(series, Ctx())
refs = references.load_refs(1)
assert r["removed"] == 1 and "S01E14" not in refs, r
assert "S01E24" in references._load_misses(1).get("S01E14", "")

print("B) subtitle is genuinely S01E14's dialogue")
setup(TEXT[14])
r = references.verify_flagged(series, Ctx())
refs = references.load_refs(1)
assert r["verified"] == 1 and not references.is_low_trust(refs["S01E14"]), r

print("C) label points at an episode with no reference yet")
assert references.is_low_trust(refs["S01E15"]) and r["pending"] == 1
print("PASS")
