"""Two files claiming one episode (the S07E40E41 screenshot).

A file whose own title card confirms its name must never be moved aside just because
another file claims one of its episodes; and an aside is only pre-ticked when the file
taking the name is itself pre-ticked.
"""
import os
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_claims"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import planner  # noqa: E402

root = W / "media" / "Paw Patrol"
(root / "Season 7").mkdir(parents=True, exist_ok=True)
EPS = [(7, 34, "Moto Pups: Pups vs. the Ruff-Ruff Pack"), (7, 40, "Moto Pups: Rescue at Twisty Top Mesa"),
       (7, 41, "Moto Pups: Pups Save a Sneezy Chase"), (7, 42, "Pups Save a Big Bot"), (7, 43, "Pups Save a Toy")]
series = {"path": str(root), "tvdb_id": 999999, "options": {},
          "episodes": [{"season": s, "episode": e, "title": t} for s, e, t in EPS]}
A = "Season 7/Paw Patrol - S07E34 - Moto Pups - Pups vs. the Ruff-Ruff Pack HDTV-1080p.mkv"
B = "Season 7/Paw Patrol - S07E40E41 - Moto Pups - Rescue at Twisty Top Mesa + Moto Pups - Pups Save a Sneezy Chase WEBDL-1080p.mkv"
C = "Season 7/Paw Patrol - S07E42 - Pups Save a Big Bot.mkv"
D = "Season 7/Paw Patrol - S07E43 - Pups Save a Toy.mkv"
for rel in (A, B, C, D):
    (root / rel).touch()
card = {"text": "x", "code": None}


def plan(a_second):
    files = [
        {"rel": A, "expected": ["S07E34"], "status": "MISMATCH", "duration": 2811, "segments": [
            {"start": 0, "end": 1463, "code": "S07E34", "score": .2, "confidence": "high",
             "evidence": "title+filename", "title_card": card},
            a_second]},
        {"rel": B, "expected": ["S07E40", "S07E41"], "status": "LOW_CONFIDENCE", "hold": True,
         "duration": 1393, "note": "no evidence either way for S07E41 — left as named", "segments": [
             {"start": 0, "end": 1393, "code": "S07E40", "score": .2, "confidence": "high",
              "evidence": "title+filename", "title_card": card}]},
        # an unverified file whose name another file confirmed by dialogue needs: still moved aside
        {"rel": C, "expected": ["S07E42"], "status": "NO_TEXT", "duration": 690, "segments": []},
        {"rel": D, "expected": ["S07E43"], "status": "MISMATCH", "duration": 690, "segments": [
            {"start": 0, "end": 690, "code": "S07E42", "score": .6, "confidence": "high", "evidence": "dialogue"}]},
    ]
    return {it["source"]: it for it in planner.build_plan(series, {"files": files})}


for label, second in (
        ("title card", {"start": 1463, "end": 2811, "code": "S07E41", "score": .2, "confidence": "high",
                        "evidence": "title", "title_card": card}),
        ("AI", {"start": 1463, "end": 2811, "code": "S07E37", "score": .15, "confidence": "low",
                "llm": {"code": "S07E41", "confidence": 1.0}})):
    by = plan(second)
    b = by[B]
    print(f"{label:10} A: {by[A]['kind']} sel={by[A]['selected']}   B: {b['kind']} sel={b['selected']} — {b['reason']}")
    assert b["kind"] == "review" and not b["selected"], b
    assert "also claimed" in b["reason"] and "S07E41" in b["reason"], b["reason"]
    c = by[C]
    print(f"{'':10} C: {c['kind']} sel={c['selected']} — {c['reason']}")
    assert c["kind"] == "aside" and c["selected"], c
print("PASS")
