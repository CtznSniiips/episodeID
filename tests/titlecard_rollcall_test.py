"""An intro roll call that names an episode (Thomas & Friends, season 4 release).

The intro shows engine names; "RUSTY" (nameplate) + "Rusty" (caption) at 0:09 reads
like "Trusty Rusty" (S07E25). The real card "RUSTY TO / THE RESCUE" comes at 0:31.
Expected: the early read doesn't end the search, the card that fits the filename
wins, and a file whose card really contradicts its name is still caught.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_roll"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"), WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, pipeline  # noqa: E402
from app.jobs import JobContext  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EPS = {(4, 15): "Rusty to the Rescue", (7, 25): "Trusty Rusty", (4, 7): "Peter Sam and the Refreshment Lorry",
       (5, 13): "Stepney Gets Lost", (4, 9): "Bulgy", (4, 10): "Henry and the Elephant"}


def t(text, at, until, size=44, y=0.42):
    return (f"drawtext=fontfile={FONT}:text='{text}':fontsize={size}:fontcolor=white:borderw=4:"
            f"bordercolor=black:x=(w-tw)/2:y=h*{y}:enable='between(t,{at},{until})'")


def make(name, card):
    p = W / "media" / "Thomas" / "Season 4" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    vf = [t("Duke", 0, 1.9), t("Skarloey", 2, 5.9), t("Duncan", 6, 8.9),
          t("RUSTY", 9, 11.9, 28, 0.30), t("Rusty", 9, 11.9, 44, 0.45),
          t("Peter Sam", 18, 20.9), t("NEXT STORY COMING UP SOON!", 28, 29.9, 30)]
    lines = card.split("/")
    vf += [t(l.strip(), 31, 34.9, 50, 0.38 + 0.14 * i) for i, l in enumerate(lines)]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x480:r=5:d=80",
                    "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", str(p)], check=True)


if W.exists():
    shutil.rmtree(W)
make("Thomas - S04E15 - Rusty to the Rescue.mp4", "RUSTY TO / THE RESCUE")
make("Thomas - S04E09 - Bulgy.mp4", "HENRY AND / THE ELEPHANT")      # really S04E10: must be caught

db.init()
sid = db.add_series(888, "Thomas", "1984", str(W / "media" / "Thomas"), None, options={"title_cards": "on"})
db.update_series(sid, episodes=[{"season": s, "episode": e, "title": x, "overview": "", "runtime": 5}
                                for (s, e), x in EPS.items()])
j = db.add_job("scan", sid, {})
pipeline.job_scan(JobContext(j, sid), {})
print("\n".join(l for l in db.get_job(j)["log"].splitlines() if "title card " in l))
by = {Path(f["rel"]).name: f for f in db.latest_scan(sid)["results"]["files"]}
for n, f in sorted(by.items()):
    print(f"{f['status']:10} {n} {[sg['code'] for sg in f['segments']]}")
assert [sg["code"] for sg in by["Thomas - S04E15 - Rusty to the Rescue.mp4"]["segments"]] == ["S04E15"]
assert by["Thomas - S04E15 - Rusty to the Rescue.mp4"]["status"] == "OK"
assert [sg["code"] for sg in by["Thomas - S04E09 - Bulgy.mp4"]["segments"]] == ["S04E10"]
print("PASS")
