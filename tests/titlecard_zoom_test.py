"""Doc McStuffins "You Crack Me Up": an animated card readable for only part of each
second. At one frame a second the scan can land only on moments where it's half drawn
("YOU CRACK" — big text, no full title), so it found nothing; Check title cards, on
different seconds, read it. Big text that matches no title now gets a closer look,
four frames a second around it."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_zoom"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import titlecard as T  # noqa: E402

if not T.available():
    print("SKIP (no OCR engine)")
    sys.exit(0)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
v = W / "card.mkv"
v.parent.mkdir(parents=True)
(W / "cache").mkdir(parents=True)
# 0–40 s; "YOU CRACK" from 19.3 s to 22.7 s, the full title only 20.6–20.9 s (between
# the one-a-second samples at 20 and 21).
draw = (f"drawtext=fontfile={FONT}:text='YOU CRACK':fontsize=60:fontcolor=white:borderw=5:x=(w-tw)/2:y=120:"
        f"enable='between(t,19.3,22.7)',"
        f"drawtext=fontfile={FONT}:text='ME UP':fontsize=60:fontcolor=white:borderw=5:x=(w-tw)/2:y=200:"
        f"enable='between(t,20.6,20.9)'")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x3060a0:s=640x360:r=25:d=40",
                "-vf", draw, "-c:v", "libx264", "-preset", "ultrafast", "-g", "250", str(v)], check=True)
idx = T.TitleIndex([{"season": 2, "episode": n, "title": t} for n, t in (
    (19, "Professor Pancake"), (20, "You Crack Me Up"), (21, "Bubble Trouble"))])

frames = T.scan_window(v, 10.0, 25.0, idx, expected=["S02E19", "S02E20"])
cards = T.cards_from_frames(frames)
print("  frames read:", [(f["t"], (f["hit"] or {}).get("code"), f.get("zoom", False)) for f in frames if f["lines"]])
print("  cards:", [(c["code"], c["time"], c["frames"], c["partial"]) for c in cards])
assert any(c["code"] == "S02E20" and not c["partial"] for c in cards), cards

# A window cached before the closer look existed gets it once, from the cache path.
import json  # noqa: E402
cache = T._cache_path(v, 10.0, 25.0, 1.0, "full")
data = json.loads(cache.read_text())
data["frames"] = [f for f in data["frames"] if float(f["t"]).is_integer()]
data.pop("zoomed")
cache.write_text(json.dumps(data))
frames = T._scan_pass(v, 10.0, 25.0, idx, None, True, "full", 0, 10.0, ["S02E19", "S02E20"])
assert any(f["hit"] and f["hit"]["code"] == "S02E20" and not f["hit"]["partial"] for f in frames)
assert json.loads(cache.read_text())["zoomed"] is True
print("PASS")
