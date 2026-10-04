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
       (10, 32, "Mighty Pups vs. The Mighty Cheetah")]
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

# A lone complete card that contradicts filename and weak dialogue goes to review.
f = {"expected": ["S07E24"], "text_source": "embedded",
     "segments": [{"start": 0, "end": 690, "code": "S01E42", "score": .08, "confidence": "low", "alternatives": []}]}
pipeline._apply_title_cards(f, [{**idx.read("SAVEA ROCKET ROLLERSKATER"), "time": 40.0}])
assert f["segments"][0]["confidence"] == "conflict" and f["status"] == "LOW_CONFIDENCE", f["segments"]
print("  lone card contradicting filename + weak dialogue → review")

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
print("PASS")
