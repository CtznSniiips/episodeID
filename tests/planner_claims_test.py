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

# The S06E39 / S06E44E45 screenshot: AI agreeing with the filename is not a rename.
assert planner.replace_colons("Mighty Pups, Charged Up: Pups vs. the Copycat") == \
    "Mighty Pups, Charged Up - Pups vs. the Copycat"
assert planner.replace_colons("At 10:30", "smart") == "At 10-30"
assert planner.replace_colons("A: B", "delete") == "A B"
# Other illegal characters, as Sonarr's FileNameBuilder.CleanFileName
ct = planner.clean_token
assert ct("Who's Afraid? Not Me") == "Who's Afraid! Not Me"
assert ct("AC/DC <Live> *Encore* \"Hits\" | B\\C") == "AC+DC Live -Encore- Hits  B+C"
assert ct("What?", {"replace_illegal_characters": False}) == "What"
assert ct("Part 1: The Start", {"colon_replacement": "custom", "colon_replacement_custom": " ~ "}) == "Part 1 ~  The Start"
assert ct("...Hello. ") == "Hello."
series["episodes"] += [{"season": 6, "episode": 39, "title": "Mighty Pups, Charged Up: Pups vs. the Copycat"},
                       {"season": 6, "episode": 44, "title": "Mighty Pups, Charged Up: Pups Stop a Big Bad Bot"},
                       {"season": 6, "episode": 45, "title": "Mighty Pups, Charged Up: Mighty Pups Versus the Dome"}]
E = "Season 6/Paw Patrol - S06E39 - Mighty Pups, Charged Up - Pups vs. the Copycat WEBRip-1080p.mkv"
F = "Season 6/Paw Patrol - S06E44E45 - Mighty Pups, Charged Up - Pups Stop a Big Bad Bot + Mighty Pups, Charged Up - Mighty Pups Versus the Dome HDTV-1080p.mkv"
(root / "Season 6").mkdir(exist_ok=True)
for rel in (E, F):
    (root / rel).touch()
files = [
    {"rel": E, "expected": ["S06E39"], "status": "NO_MATCH", "duration": 1393, "segments": [
        {"start": 0, "end": 1393, "code": None, "score": 0, "confidence": "none",
         "llm": {"code": "S06E39", "confidence": 1.0}}]},
    {"rel": F, "expected": ["S06E44", "S06E45"], "status": "LOW_CONFIDENCE", "duration": 1393, "segments": [
        {"start": 0, "end": 608, "code": "S06E09", "score": .15, "confidence": "low",
         "llm": {"code": "S06E44", "confidence": 1.0}}]},
]
by = {it["source"]: it for it in planner.build_plan(series, {"files": files})}
for rel in (E, F):
    print(f"{by[rel]['kind']:7} sel={by[rel]['selected']} — {by[rel]['reason']}")
assert by[E]["kind"] == "ok", by[E]
assert by[F]["kind"] == "review" and not by[F]["selected"] and "S06E45" in by[F]["reason"], by[F]
# A Sonarr-named file is "correct" as named (colon → " - ").
eps39 = [e for e in series["episodes"] if (e["season"], e["episode"]) == (6, 39)]
assert planner.episode_filename("Paw Patrol", eps39, ".mkv", E, planner.get_settings()) == Path(E).name

# Sonarr's multi-episode titles (S09E03-E07 file): too long for " + " → "first...last".
st = dict(planner.get_settings(), multi_episode_style="repeat", colon_replacement="smart",
          replace_illegal_characters=True)
cat = [{"season": 9, "episode": n, "title": t} for n, t in (
    (3, "Pups Meet the Cat Pack"), (4, "Cat Pack/PAW Patrol Rescue: Rocket Rescuers"),
    (5, "Cat Pack/PAW Patrol Rescue: The Golden Lion Mask"), (6, "Cat Pack/PAW Patrol Rescue: The Cat Who Roared"),
    (7, "Cat Pack/PAW Patrol Rescue: Saving the Safe"))]
have = "Paw Patrol - S09E03E04E05E06E07 - Pups Meet the Cat Pack...Cat Pack+PAW Patrol Rescue - Saving the Safe WEBDL-1080p.mkv"
got = planner.episode_filename("Paw Patrol", cat, ".mkv", have, st)
print(" ", got); assert got == have, got
# Two short titles still join with " + "; "(1)"/"(2)" parts collapse to one title.
two = [{"season": 1, "episode": 1, "title": "The Quest (1)"}, {"season": 1, "episode": 2, "title": "The Quest (2)"}]
assert planner.episode_filename("Show", two, ".mkv", "", dict(st, multi_episode_style="prefixed_range")) == \
    "Show - S01E01-E02 - The Quest.mkv"
assert planner.episode_filename("Show", [{"season": 1, "episode": 3, "title": "Who's Afraid?"}], ".mkv", "", st) == \
    "Show - S01E03 - Who's Afraid.mkv"
# Two files whose title cards each name the other's episode (Thomas S04E09/S04E10):
# a confirmed swap, both renames pre-ticked — not a rename plus an aside.
from app import pipeline  # noqa: E402
from app.jobs import JobContext  # noqa: E402,F401
T = root.parent / "Thomas"
(T / "Season 4").mkdir(parents=True, exist_ok=True)
A = "Season 4/Thomas the Tank Engine & Friends - S04E09 - Home at Last DVD.mkv"
B = "Season 4/Thomas the Tank Engine & Friends - S04E10 - Rock 'n' Roll DVD.mkv"
for rel in (A, B):
    (T / rel).touch()
tseries = {"path": str(T), "tvdb_id": 999997, "options": {"name_in_files": "Thomas the Tank Engine & Friends"},
           "episodes": [{"season": 4, "episode": 9, "title": "Home at Last"},
                        {"season": 4, "episode": 10, "title": "Rock 'n' Roll"}]}
def conflict_file(rel, exp, card, dlg):
    return {"rel": rel, "expected": [exp], "status": "LOW_CONFIDENCE", "duration": 333, "text_source": "whisper small",
            "segments": [{"start": 0, "end": 333, "code": dlg, "score": .1, "confidence": "conflict",
                          "title_conflict": card, "title_card": {"code": card, "text": "x"}, "alternatives": []}]}
files = [conflict_file(A, "S04E09", "S04E10", "S02E09"), conflict_file(B, "S04E10", "S04E09", "S02E07")]
files[0]["segments"][0]["llm"] = {"code": "S04E10", "confidence": 1.0}
class _Ctx:
    def log(self, m): print("  log:", m)
pipeline._title_card_swaps(files, _Ctx())
by = {it["source"]: it for it in planner.build_plan(tseries, {"files": files})}
for rel in (A, B):
    it = by[rel]
    print(f"  {it['kind']:7} sel={it['selected']} {rel.split(' - ')[1]} -> {[t['path'].split(' - ')[1] for t in it.get('targets', [])]}")
    assert it["kind"] == "rename" and it["selected"] and not it.get("ai"), it
# A lone contradicting card is still not enough on its own.
lone = [conflict_file(A, "S04E09", "S04E10", "S02E09")]
pipeline._title_card_swaps(lone, _Ctx())
assert lone[0]["segments"][0]["confidence"] == "conflict"
print("PASS")
