"""Partial and animated title cards (the PAW Patrol cases).

Unit: the exact OCR texts from a real scan, against real PAW Patrol titles.
End to end: generated videos whose cards animate in —
  "PUPS SAVE THE BABY" → then "OSTRICHES" appears below (TVDB also has "Pups Save the Bay")
  "MIGHTY PUPS" banner → then "STOP THE MIGHTY QUEEN" (TVDB also has a special "Mighty Pups")
  "PUPS SAVE RYDER'S" only, never completing (TVDB has "Pups Save Ryder" and "…Ryder's Surprise")
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_tcp"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pipeline, titlecard  # noqa: E402

EPS = [(0, 14, "Mighty Pups"), (1, 24, "Pups Save the Bay"), (1, 42, "Pups Save Ryder"),
       (6, 11, "Pups and the Stinky Bubble Trouble"), (6, 12, "Pups Save the Baby Ostriches"),
       (7, 23, "Pups Save a Rocket Roller Skater"), (7, 24, "Pups Save Ryder's Surprise"),
       (10, 30, "Mighty Pups Stop the Mighty Queen"), (10, 31, "Mighty Pups Stop the Hiccups"),
       (10, 32, "Mighty Pups vs. The Mighty Cheetah"),
       (11, 1, "Rescue Wheels: Pups Save the Teetering Tower"), (11, 2, "Rescue Wheels: Pups Save the Spelunkers"),
       (11, 3, "Rescue Wheels: Pups Save the Risky Race"), (11, 4, "Rescue Wheels: Pups Save the Runaway Truck"),
       (11, 38, "Fire Rescue: Pups Save the Flaming Flounder"), (11, 39, "Fire Rescue: Pups Make the News"),
       (11, 40, "Fire Rescue: Pups Save a Baby Goat Birthday"), (11, 41, "Fire Rescue: Pups Save a S'more-mergency")]
episodes = [{"season": s, "episode": e, "title": t} for s, e, t in EPS]
idx = titlecard.TitleIndex(episodes, include_specials=True)

print("Unit — OCR texts from the real scan:")
cases = {
    "MIGHTYPUPS": ("partial", {"S00E14", "S10E30", "S10E31"}),
    "MIGHTYPUPS STOPTHE HICCUPS": ("full", "S10E31"),
    "ANDTHE STINLY BUBBLETROUBLE": ("full", "S06E11"),
    "PUPS SAVETHE BABY": ("partial", {"S06E12"}),
    "SAVEA ROCKET ROLLERSKATER": ("full", "S07E23"),
    "PUPS SAVE RYDER'S": ("partial", {"S07E24"}),
    # second scan
    "AW MIGHTYPUPS": ("partial", {"S00E14", "S10E30", "S10E31"}),
    "RESCUE WHEELS PUPS": ("partial", {"S11E01", "S11E02", "S11E03", "S11E04"}),
    "FIRERESCUE PUPS SAVETHE": ("partial", {"S11E38"}),
    "FIRE RESCUE PUPS MAKETHE": ("partial", {"S11E39"}),
    "FIRERESCUE PUPS SAVEA": ("partial", {"S11E40", "S11E41"}),
    "RESCUE WHEELS PUPS SAVE THE TEETERING TOWER": ("full", "S11E01"),
}
for text, (kind, want) in cases.items():
    r = idx.read(text)
    got = ("partial", set(r["candidates"])) if r and r["partial"] else ("full", r and r["code"])
    print(f"  {text!r:32} → {got[0]:7} {sorted(got[1]) if kind == 'partial' else got[1]}")
    assert got[0] == kind, (text, r)
    assert (want <= got[1]) if kind == "partial" else got[1] == want, (text, r)

# Partial reads resolve against the filename, never pick the shorter title on their own.
f = {"expected": ["S06E11", "S06E12"], "text_source": "embedded",
     "segments": [{"start": 0, "end": 610, "code": "S06E11", "score": .3, "confidence": "high", "alternatives": []},
                  {"start": 695, "end": 1390, "code": "S01E24", "score": .08, "confidence": "low",
                   "alternatives": [{"code": "S06E12", "score": .07}]}]}
pipeline._apply_title_cards(f, [{**idx.read("PUPS SAVETHE BABY"), "time": 700.0}])
assert f["segments"][-1]["code"] == "S06E12" and f["status"] == "OK", f["segments"]
print("  partial 'PUPS SAVETHE BABY' + filename S06E11E12 → S06E12, file OK")

# "MAKETHE" must not be read as the start of "…Pups Save the Flaming Flounder".
assert "S11E38" not in idx.read("FIRE RESCUE PUPS MAKETHE")["candidates"]

# Two-episode files: each half's partial card is checked against the episode the
# filename puts at that point — even when the dialogue split doesn't line up.
for name, exp, t1, t2, want in (
        ("Rescue Wheels", ["S11E01", "S11E02"], "RESCUE WHEELS PUPS", "RESCUE WHEELS PUPS", ["S11E01", "S11E02"]),
        ("Fire Rescue", ["S11E38", "S11E39"], "FIRERESCUE PUPS SAVETHE", "FIRE RESCUE PUPS MAKETHE", ["S11E38", "S11E39"]),
        ("Fire Rescue 2", ["S11E40", "S11E41"], "FIRERESCUE PUPS SAVEA", "FIRERESCUE PUPS SAVEA", ["S11E40", "S11E41"]),
        ("Mighty Pups", ["S10E30", "S10E31"], "AW MIGHTYPUPS", "MIGHTYPUPS STOPTHE HICCUPS", ["S10E30", "S10E31"])):
    f = {"expected": exp, "text_source": "embedded", "duration": 1397.0,
         "segments": [{"start": 0, "end": 617, "code": "S01E01", "score": .05, "confidence": "low", "alternatives": []},
                      {"start": 617, "end": 702, "code": "S02E02", "score": .04, "confidence": "low", "alternatives": []},
                      {"start": 702, "end": 1397, "code": "S03E03", "score": .05, "confidence": "low", "alternatives": []}]}
    pipeline._apply_title_cards(f, [{**idx.read(t1), "time": 47.0}, {**idx.read(t2), "time": 707.0}])
    got = [sg["code"] for sg in f["segments"] if sg.get("title_card")]
    print(f"  {name:13} cards → {got}")
    assert got == want, (name, f["segments"])

# A lone complete card that contradicts filename and weak dialogue goes to review.
f = {"expected": ["S07E24"], "text_source": "embedded",
     "segments": [{"start": 0, "end": 690, "code": "S01E42", "score": .08, "confidence": "low", "alternatives": []}]}
pipeline._apply_title_cards(f, [{**idx.read("SAVEA ROCKET ROLLERSKATER"), "time": 40.0}])
assert f["segments"][0]["confidence"] == "conflict" and f["status"] == "LOW_CONFIDENCE", f["segments"]
print("  lone card contradicting filename + weak dialogue → review")

# Cached OCR from an older version that stopped reading too early must not be reused.
L = lambda txt: [[txt, 0.95, 0.08, 0.4]]
partial_frames = [{"t": 20.0 + i, "lines": L("PUPS SAVETHE BABY")} for i in range(7)]
old = {"complete": False, "frames": partial_frames}            # old rule: stopped 6 s after a hit
assert titlecard._reuse_cached(dict(old), idx, 1.0) is None, "early-stopped old cache must be re-read"
ok = {"complete": False, "stopped_at": 33.0, "ocr": titlecard.OCR_SIG, "frames": [{"t": 20.0 + i, "lines": L("PUPS SAVETHE BABY")} for i in range(13)]}
assert titlecard._reuse_cached(ok, idx, 1.0) is not None, "cache that covers the settle time is reused"
gone = {"complete": False, "stopped_at": 27.0, "frames": [{"t": 20.0, "lines": L("XQZW")}]}
assert titlecard._reuse_cached(gone, idx, 1.0) is None, "cache stopped on a no-longer-matching frame is re-read"
assert titlecard._reuse_cached({"complete": True, "ocr": titlecard.OCR_SIG, "frames": [{"t": 1.0, "lines": []}]}, idx, 1.0) is not None
# Read by an older OCR version: windows that found no complete title are read again…
assert titlecard._reuse_cached({"complete": True, "frames": [{"t": 1.0, "lines": []}]}, idx, 1.0) is None
# …but a window that already found its title is kept.
assert titlecard._reuse_cached({"complete": True, "frames": [{"t": 1.0, "lines": L("MIGHTYPUPS STOPTHE HICCUPS")}]}, idx, 1.0) is not None
print("  cached OCR: early-stopped old scans re-read, covering scans reused")

# An exact read wins against a title only two letters longer (S06E31 on the full PAW
# Patrol list: "Pups Save the Bears" vs "Pups Save the Beavers").
bi = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in
                           EPS + [(6, 31, "Pups Save the Bears"), (2, 9, "Pups Save the Beavers"), (2, 1, "Pups Save the Bees")]])
r = bi.match_frame([["PUPS", .986, .197, .3], ["SAVETHE", .99, .117, .46], ["BEARS", .982, .2, .63],
                    ["Written by Clark Stubbs", .978, .058, .82]])
assert r and not r["partial"] and r["code"] == "S06E31", r
assert bi.read("PUPS SAVETHE BEAVERS")["code"] == "S02E09"
r = bi.read("PUPS SAVETHE BEAERS")  # an OCR slip between the two: not decided by itself
assert not r or r["partial"] or r["code"] == "S06E31", r
print("  exact 'PUPS SAVETHE BEARS' → S06E31 despite 'Pups Save the Beavers'")

# Banner + subtitle titles (S09E06E07): the card shows "CAT PACK" small and the
# subtitle big; "PAW Patrol Rescue" isn't on screen. And a card worded differently
# from TVDB ("The Cat That Roared" vs "The Cat Who Roared") nothing else comes close to.
ci = titlecard.TitleIndex([{"season": s, "episode": e, "title": t} for s, e, t in EPS + [
    (9, 4, "Cat Pack/PAW Patrol Rescue: Cat Pack Meets the PAW Patrol"),
    (9, 5, "Cat Pack/PAW Patrol Rescue: The Golden Lion Mask"),
    (9, 6, "Cat Pack/PAW Patrol Rescue: The Cat Who Roared"),
    (9, 7, "Cat Pack/PAW Patrol Rescue: Saving the Safe"),
    (6, 31, "Pups Save the Bears"), (2, 9, "Pups Save the Beavers")]])
safe = [["CATPACK", .851, .097, .2], ["SAVING", .985, .153, .4], ["THE", .994, .164, .55],
        ["SAFE", .995, .192, .7], ["Written by Michael Stokes", .959, .056, .85]]
r = ci.match_frame(safe); assert r and not r["partial"] and r["code"] == "S09E07", r
roar = [["CATTAEK", .908, .089, .2], ["THECAT", .993, .136, .35], ["THAT", .996, .119, .5],
        ["ROARED", .991, .144, .65], ["Written byMichael Stokes", .965, .047, .85]]
r = ci.match_frame(roar); assert r and not r["partial"] and r["code"] == "S09E06", r
print("  'CAT PACK / SAVING THE SAFE' → S09E07, 'CAT PACK / THE CAT THAT ROARED' → S09E06")
# Same subtitle under two banners → only candidates, never a pick by itself.
di = titlecard.TitleIndex([{"season": 1, "episode": 1, "title": "Rescue Wheels: Pups Save the Day"},
                           {"season": 2, "episode": 1, "title": "Aqua Pups: Pups Save the Day"}])
r = di.read("PUPS SAVE THE DAY"); assert r["partial"] and set(r["candidates"]) == {"S01E01", "S02E01"}, r

print("End to end — animated cards:")
FONT = next(p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",) if Path(p).exists())


def card(line1, line2, at, full_at, until):
    f1 = (f"drawtext=fontfile={FONT}:text='{line1}':fontsize=54:fontcolor=white:borderw=5:bordercolor=blue:"
          f"x=(w-tw)/2:y=h*0.36:enable='between(t,{at},{until})'")
    if not line2:
        return f1
    return f1 + (f",drawtext=fontfile={FONT}:text='{line2}':fontsize=54:fontcolor=yellow:borderw=5:"
                 f"bordercolor=blue:x=(w-tw)/2:y=h*0.52:enable='between(t,{full_at},{until})'")


if W.exists():
    shutil.rmtree(W)
(W / "cache").mkdir(parents=True)
videos = {
    "baby": ("PUPS SAVE THE BABY", "OSTRICHES", ["S06E12"], "S06E12"),
    "mighty": ("MIGHTY PUPS", "STOP THE MIGHTY QUEEN", ["S10E30"], "S10E30"),
    "ryder": ("PUPS SAVE RYDER’S", None, ["S07E24"], "S07E24"),
}
for name, (l1, l2, expected, want) in videos.items():
    v = W / f"{name}.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=854x480:r=10:d=60",
                    "-vf", card(l1, l2, 20, 22.5, 27), "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
                    str(v)], check=True)
    cards = titlecard.detect(v, 60.0, [], idx)
    f = {"expected": expected, "text_source": "none", "segments": [], "duration": 60.0}
    pipeline._apply_title_cards(f, cards)
    got = f["segments"][0]["code"] if f["segments"] else None
    print(f"  {name:7} cards={[(c['text'], c.get('code'), c['partial']) for c in cards]} → {got}")
    assert got == want, (name, cards)

# Cached reads are reused; force=<job start> re-reads each window once.
import time  # noqa: E402
v = W / "baby.mkv"
calls = []
orig = titlecard._sample
titlecard._sample = lambda *a, **k: calls.append(a) or orig(*a, **k)
titlecard.detect(v, 60.0, [], idx)
assert not calls, "second detect should come from the cache"
t0 = time.time()
titlecard.detect(v, 60.0, [], idx, force=t0)
n = len(calls)
assert n >= 1, "force must re-read the video"
titlecard.detect(v, 60.0, [], idx, force=t0)
assert len(calls) == n, "a window already re-read in this job is not read again"
titlecard._sample = orig
print(f"  force re-read: {n} window pass(es) re-decoded, then cached again")
print("PASS")
