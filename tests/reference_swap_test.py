"""Regression test for swapped references (the Gumball "The Voice" / "The Promise" case).

Two correctly named files; their OpenSubtitles references hold each other's
dialogue because IMDb numbers the pair the other way round. Expected:
  1. the plan proposes the swap but does NOT pre-tick it, and says why;
  2. with a wiki configured, Fetch references replaces both references with
     transcripts (looked up by title) and a rescan shows both files as OK;
  3. a wiki that errors (rate-limited) never falls back to OpenSubtitles.
"""
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_swap"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"), WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

random.seed(11)
SYL = ["ka", "lo", "mi", "ra", "tu", "ne", "so", "vi", "da", "pe", "zu", "gor"]
lines = {n: [" ".join("".join(random.choice(SYL) for _ in range(3)) for _ in range(8))
             for _ in range(60)] for n in (31, 32)}


def srt(ls):
    out = []
    for i, l in enumerate(ls):
        st = 5 + i * 4.5
        f = lambda t: f"00:{int(t // 60):02d}:{int(t % 60):02d},000"  # noqa: E731
        out.append(f"{i + 1}\n{f(st)} --> {f(st + 3)}\n{l}\n")
    return "\n".join(out)


if W.exists():
    shutil.rmtree(W)
root = W / "media" / "tv" / "Show" / "Season 02"
root.mkdir(parents=True)
for n, title in ((31, "The Voice"), (32, "The Promise")):
    v = root / f"Show - S02E{n} - {title} WEBDL-1080p.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=5:d=280",
                    "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "280",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(v)], check=True)
    v.with_suffix(".en.srt").write_text(srt(lines[n]))

refdir = W / "cache" / "refs" / "777"
refdir.mkdir(parents=True)
for n, other, title in ((31, 32, "The Promise"), (32, 31, "The Voice")):
    # OpenSubtitles reference for TVDB S02E{n} actually contains episode {other}'s dialogue
    (refdir / f"S02E{n}.json").write_text(json.dumps({
        "source": "opensubtitles", "text": " ".join(lines[other]), "via": "episode IMDb id",
        "os_title": title, "os_number": f"S02E{other}", "title_verified": True,
        "numbering_conflict": True}))

db.init()
sid = db.add_series(777, "Show", "2011", str(W / "media" / "tv" / "Show"), "tt1")
db.update_series(sid, episodes=[{"season": 2, "episode": 31, "title": "The Voice", "overview": ""},
                                {"season": 2, "episode": 32, "title": "The Promise", "overview": ""}],
                 options={"fandom_wiki": "showwiki", "fandom_checked": True})


def scan():
    j = db.add_job("scan", sid, {})
    r = pipeline.job_scan(JobContext(j, sid), {})
    return db.get_job(j)["log"], db.get_plan(r["plan_id"])["items"]


log, items = scan()
print(log)
for it in items:
    print(it["kind"], "selected=", it["selected"], "|", it["reason"])
assert all(it["kind"] == "rename" and not it["selected"] and it.get("ref_warning") for it in items), \
    "swap must be proposed but not pre-ticked"

# 3. wiki rate-limited → deferred, no OpenSubtitles call
calls = []
references.fandom_page = lambda wiki, page: (None, "error")
references.fetch_opensubs = lambda *a, **k: calls.append(a) or (None, "should not be called")
j = db.add_job("fetch_refs", sid, {})
pipeline.job_fetch_refs(JobContext(j, sid), {})
print(db.get_job(j)["log"])
assert not calls

# 2. wiki answers → OpenSubtitles references replaced by transcripts
transcript = {"The Voice/Transcript": lines[31], "The Promise/Transcript": lines[32]}
references.fandom_page = lambda wiki, page: (
    "\n".join(f"Gumball: {l}" for l in transcript[page]), "ok")
j = db.add_job("fetch_refs", sid, {})
pipeline.job_fetch_refs(JobContext(j, sid), {})
print(db.get_job(j)["log"])
log, items = scan()
print(log)
assert all(it["kind"] == "ok" for it in items), "both files should now be OK"
print("PASS")
