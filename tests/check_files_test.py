"""Check files: a clean file passes; corrupted, truncated and audio/video-mismatched
files are caught (with where, roughly); results are cached by size and date, follow a
renamed file, drop out for a replaced one, and show on the series."""
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_checkfiles"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false", TITLECARD_HWACCEL="off")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, integrity, main  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Show" / "Season 1"
root.mkdir(parents=True)


def make(path, seconds=30, audio_seconds=None, codec="libx265"):
    af = ["-af", f"atrim=0:{audio_seconds}"] if audio_seconds else []
    extra = ["-x265-params", "log-level=error"] if codec == "libx265" else []
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds),
                        "-c:v", codec, *extra, "-preset", "ultrafast", "-c:a", "aac", *af, str(path)],
                       capture_output=True, text=True)
    return r.returncode == 0


codec = "libx265" if make(root / "probe.mkv", 2) else "libx264"
(root / "probe.mkv").unlink(missing_ok=True)
ep = lambda n: root / f"Show - S01E{n:02d}.mkv"  # noqa: E731
assert make(ep(1), codec=codec)
shutil.copy(ep(1), ep(2))
random.seed(1)
n = ep(2).stat().st_size
with open(ep(2), "r+b") as f:  # damage the middle
    for frac in (0.4, 0.42, 0.6, 0.75):
        f.seek(int(n * frac))
        f.write(bytes(random.getrandbits(8) for _ in range(3000)))
ep(3).write_bytes(ep(1).read_bytes()[: int(n * 0.55)])  # cut short
assert make(ep(4), audio_seconds=18, codec=codec)  # audio stops at 0:18 of 0:30

db.init()
sid = db.add_series(1, "Show", "2020", str(root.parent), None)
r = integrity.job_check_files(JobContext(db.add_job("check_files", sid, {}), sid), {})
st = integrity.current(db.get_series(sid))
got = {Path(k).name: v["status"] for k, v in st.items()}
print(" ", codec, got, r)
for k, v in st.items():
    print("   ", Path(k).name, "—", v["summary"][:150])
assert got == {ep(1).name: "ok", ep(2).name: "bad", ep(3).name: "bad", ep(4).name: "warn"}, got
assert "cut short" in st["Season 1/" + ep(3).name]["summary"]
assert "lengths differ" in st["Season 1/" + ep(4).name]["summary"]
assert st["Season 1/" + ep(2).name]["errors"] >= integrity.BAD_ERRORS
print("PASS (problems found)")

# Cached: a second run decodes nothing.
calls = []
real = integrity._decode
integrity._decode = lambda *a, **k: calls.append(a) or real(*a, **k)
integrity.job_check_files(JobContext(db.add_job("check_files", sid, {}), sid), {})
assert not calls, "unchanged files aren't decoded again"
# Renamed: the result follows the file. Replaced: it drops out.
ep(2).rename(root / "Show - S01E02 - renamed.mkv")
shutil.copy(ep(1), ep(3))
os.utime(ep(3), (1, 1))
st = integrity.current(db.get_series(sid))
names = {Path(k).name: v["status"] for k, v in st.items()}
assert names.get("Show - S01E02 - renamed.mkv") == "bad" and ep(3).name not in names, names
print("PASS (cached; follows renames; drops replaced files)")

c = TestClient(main.app)
s = c.get("/api/series").json()[0]
assert s["integrity"]["bad"] == 1 and s["integrity"]["warn"] == 1, s["integrity"]  # E03 replaced, E02 renamed
full = c.get(f"/api/series/{sid}").json()
assert full["integrity_files"]["Season 1/Show - S01E02 - renamed.mkv"]["status"] == "bad"
db.conn().execute("UPDATE jobs SET status='done'")  # the jobs above were run directly
assert c.post(f"/api/series/{sid}/jobs", json={"kind": "check_files", "params": {}}).status_code == 200
print("PASS (API)")
