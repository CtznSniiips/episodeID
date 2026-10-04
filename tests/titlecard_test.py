"""End-to-end test for title-card detection.

Library (TVDB S02E31–S02E37 titled like Gumball episodes):
  S02E31 - The Voice     correct file, but its reference holds S02E32's dialogue (swapped)
  S02E32 - The Promise   correct file, reference holds S02E31's dialogue
  S02E35 - The Fraud     correct, normal reference           (probe file)
  S02E36 - The Job       correct, normal reference; also shows a small "THE CAR" sign
  S02E34 - The Castle    no subtitles, no reference; the card says THE CASTLE → S02E33
  S02E37 - Two eps       no subtitles; cards THE BOSS then THE MOVE → split S02E37 + S02E38

Expected: the swapped pair stays as named (title card + filename beat the bad
reference), the no-subtitle files are identified from their cards, the small sign
is ignored, and the series is detected as having title cards with a learned window.
"""
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_tc"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"), WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, pipeline  # noqa: E402
from app.jobs import JobContext  # noqa: E402

FONT = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-BoldItalic.ttf",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if Path(f).exists()))
SMALL = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EP = 240  # seconds per synthetic episode
CARD_AT = 25

random.seed(5)
SYL = ["ka", "lo", "mi", "ra", "tu", "ne", "so", "vi", "da", "pe", "zu", "gor", "fin", "bel"]
TITLES = {31: "The Voice", 32: "The Promise", 33: "The Castle", 34: "The Boombox",
          35: "The Fraud", 36: "The Job", 37: "The Boss", 38: "The Move", 39: "The Car"}
LINES = {n: [" ".join("".join(random.choice(SYL) for _ in range(3)) for _ in range(8))
             for _ in range(45)] for n in TITLES}


def srt(parts):
    out, k = [], 1
    for p, n in enumerate(parts):
        for i, l in enumerate(LINES[n]):
            st = p * EP + 40 + i * 4.2
            f = lambda t: f"00:{int(t // 60):02d}:{int(t % 60):02d},000"  # noqa: E731
            out.append(f"{k}\n{f(st)} --> {f(st + 3)}\n{l}\n")
            k += 1
    return "\n".join(out)


def card_filter(text, at):
    words = text.upper().split(" ", 1)
    return (f"drawtext=fontfile={FONT}:text='{words[0]}':fontsize=40:fontcolor=yellow:borderw=4:"
            f"bordercolor=black:x=(w-tw)/2:y=h*0.30:enable='between(t,{at},{at + 4})',"
            f"drawtext=fontfile={FONT}:text='{words[1]}':fontsize=90:fontcolor=white:borderw=6:"
            f"bordercolor=red:x=(w-tw)/2:y=h*0.42:enable='between(t,{at},{at + 4})'")


def make(path: Path, parts: list[int], subs: bool, sign: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    total = EP * len(parts)
    vf = [f"drawbox=c=black:t=fill:enable='between(t,{i * EP - 2},{i * EP + 2})'"
          for i in range(1, len(parts))]
    vf += [card_filter(TITLES[n], i * EP + CARD_AT) for i, n in enumerate(parts)]
    if sign:  # small background text that must NOT count as a title card
        vf.append(f"drawtext=fontfile={SMALL}:text='{sign}':fontsize=11:fontcolor=white:"
                  f"x=20:y=h-30:enable='between(t,5,60)'")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=854x480:r=5:d={total}",
           "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono", "-t", str(total),
           "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
           "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    if subs:
        path.with_suffix(".en.srt").write_text(srt(parts))


if W.exists():
    shutil.rmtree(W)
root = W / "media" / "tv" / "Show"
s2 = root / "Season 02"
make(s2 / "Show - S02E31 - The Voice.mkv", [31], True)
make(s2 / "Show - S02E32 - The Promise.mkv", [32], True)
make(s2 / "Show - S02E35 - The Fraud.mkv", [35], True)
make(s2 / "Show - S02E36 - The Job.mkv", [36], True, sign="THE CAR")
make(s2 / "Show - S02E34 - The Boombox.mkv", [33], False)
make(s2 / "Show - S02E37 - The Boss.mkv", [37, 38], False)

refs = W / "cache" / "refs" / "555"
refs.mkdir(parents=True)
for n in (35, 36):
    (refs / f"S02E{n}.json").write_text(json.dumps({"source": "opensubtitles",
                                                    "text": " ".join(LINES[n]),
                                                    "title_verified": True}))
for n, other in ((31, 32), (32, 31)):
    (refs / f"S02E{n}.json").write_text(json.dumps({
        "source": "opensubtitles", "text": " ".join(LINES[other]), "via": "episode IMDb id",
        "os_title": TITLES[other], "os_number": f"S02E{other}", "title_verified": True,
        "numbering_conflict": True}))

db.init()
sid = db.add_series(555, "Show", "2011", str(root), "tt1")
db.update_series(sid, episodes=[{"season": 2, "episode": n, "title": t, "overview": "",
                                 "runtime": EP // 60} for n, t in TITLES.items()])

j = db.add_job("scan", sid, {})
r = pipeline.job_scan(JobContext(j, sid), {})
print(db.get_job(j)["log"])
plan = db.get_plan(r["plan_id"])["items"]
by = {it["source"].split("/")[-1]: it for it in plan}
for name, it in sorted(by.items()):
    print(f"{it['kind']:7} sel={it['selected']!s:5} {name} -> "
          f"{[t['path'].split('/')[-1] for t in it.get('targets') or []]}  ({it['reason']})")

opts = db.get_series(sid)["options"]
print("title card status:", opts.get("title_cards_status"))
assert opts["title_cards_status"]["has_cards"], "series should be detected as having title cards"
assert by["Show - S02E31 - The Voice.mkv"]["kind"] == "ok", "The Voice must stay as named"
assert by["Show - S02E32 - The Promise.mkv"]["kind"] == "ok", "The Promise must stay as named"
assert by["Show - S02E36 - The Job.mkv"]["kind"] == "ok", "small 'THE CAR' sign must be ignored"
castle = by["Show - S02E34 - The Boombox.mkv"]
assert castle["kind"] == "rename" and "S02E33 - The Castle" in castle["targets"][0]["path"]
assert castle["selected"], "title-card identification should be pre-ticked"
pair = by["Show - S02E37 - The Boss.mkv"]
assert pair["kind"] == "rename" and "S02E37-E38" in pair["targets"][0]["path"], pair
# "Rescan, re-reading title cards": same result, read from the videos again.
j = db.add_job("scan", sid, {"reread_cards": True})
r2 = pipeline.job_scan(JobContext(j, sid), {"reread_cards": True})
log = db.get_job(j)["log"] if isinstance(db.get_job(j).get("log"), str) else str(db.get_job(j))
assert "re-reading from the video files" in log, log[-500:]
print("re-read scan ok")
print("PASS")
