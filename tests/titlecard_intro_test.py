"""Opening-titles text that looks like an episode title (Bluey).

Bluey's intro names the family — "MUM", "DAD", "BINGO" — in every episode, and
"Bingo" is also an episode title. Five files, each with "BINGO'S" at 0:19 and its own
card at 0:43; one of them is the Bingo episode. Expected: the intro text is
recognised as recurring and ignored (and doesn't stop the scan before the real
card), every file is confirmed by its own card — including Bingo — and "RAIN"
(also the start of "Rainbow") is settled by the filename.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_intro"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"), WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, pipeline  # noqa: E402
from app.jobs import JobContext  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EPS = {(2, 9): "Bingo", (3, 18): "Rain", (2, 26): "Rainbow", (1, 3): "Keepy Uppy",
       (1, 10): "Hotel", (2, 1): "Dance Mode", (3, 4): "Pass the Parcel"}
FILES = [(3, 18), (2, 9), (1, 3), (1, 10), (2, 1)]


def text(t, at, size=60, y=0.4):
    return (f"drawtext=fontfile={FONT}:text='{t}':fontsize={size}:fontcolor=white:borderw=5:"
            f"bordercolor=blue:x=(w-tw)/2:y=h*{y}:enable='between(t,{at},{at + 3})'")


if W.exists():
    shutil.rmtree(W)
root = W / "media" / "Bluey (2018)"
for s, e in FILES:
    p = root / f"Season {s}" / f"Bluey (2018) - S{s:02d}E{e:02d} - {EPS[(s, e)]}.mkv"
    p.parent.mkdir(parents=True, exist_ok=True)
    vf = ",".join([text("MUM", 8), text("DAD", 13), text("BINGO", 19, 70),
                   text(EPS[(s, e)].upper(), 43, 64, 0.45)])
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=854x480:r=5:d=90",
                    "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", str(p)], check=True)

db.init()
# Saved by the previous version (codes only, no position): must be checked again.
sid = db.add_series(777, "Bluey", "2018", str(root), None, options={
    "title_cards": "on", "title_cards_status": {"recurring": ["S02E09"], "recurring_checked": True}})
db.update_series(sid, episodes=[{"season": s, "episode": e, "title": t, "overview": "", "runtime": 7}
                                for (s, e), t in EPS.items()])
j = db.add_job("scan", sid, {})
r = pipeline.job_scan(JobContext(j, sid), {})
log = db.get_job(j)["log"]
print("\n".join(l for l in log.splitlines() if "title card" in l.lower() or "opening" in l))
scan = db.latest_scan(sid)["results"]
by = {Path(f["rel"]).name: f for f in scan["files"]}
for name, f in sorted(by.items()):
    print(f"{f['status']:16} {name}  {[sg['code'] for sg in f['segments']]}")
st = db.get_series(sid)["options"]["title_cards_status"]
assert list(st.get("recurring") or {}) == ["S02E09"] and 15 <= st["recurring"]["S02E09"] <= 24, st
for (s, e) in FILES:
    f = by[f"Bluey (2018) - S{s:02d}E{e:02d} - {EPS[(s, e)]}.mkv"]
    assert [sg["code"] for sg in f["segments"]] == [f"S{s:02d}E{e:02d}"], f
    assert f["status"] == "OK", f
print("PASS")
