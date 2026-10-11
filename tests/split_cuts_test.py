"""Choosing where a split goes: candidates with pictures, a custom time, and Apply
cutting where you chose."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_cuts"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, executor, main  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Show"
src = root / "Season 01" / "Show - S01E01-E02 - Two.mkv"
src.parent.mkdir(parents=True)
# Episode 1 (testsrc) to 5:00, 2 s of black + silence, episode 2 (a different pattern).
# A 0.5 s black flash at 4:10 is a decoy. Keyframes every 2 s.
vf = ("drawbox=c=black:t=fill:enable='between(t,250,250.5)+between(t,299,301)'")
af = "volume=enable='between(t,299,301)':volume=0"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=300",
                "-f", "lavfi", "-i", "smptebars=s=640x360:r=10:d=300",
                "-f", "lavfi", "-i", "sine=f=440:r=8000:d=600",
                "-filter_complex", f"[0:v][1:v]concat=n=2:v=1[c];[c]{vf}[v];[2:a]{af}[a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "20",
                "-c:a", "aac", str(src)], check=True)

db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
db.update_series(sid, episodes=[{"season": 1, "episode": 1, "title": "One"}, {"season": 1, "episode": 2, "title": "Two"}])
rel = str(src.relative_to(root))
item = {"id": 0, "kind": "split", "source": rel, "selected": True, "expected": ["S01E01", "S01E02"],
        "status": "OK_ORDER_DIFFERS", "segments": [], "codes": ["S01E01", "S01E02"],
        "targets": [{"path": "Season 01/Show - S01E01 - One.mkv", "codes": ["S01E01"], "start": 0, "end": 290},
                    {"path": "Season 01/Show - S01E02 - Two.mkv", "codes": ["S01E02"], "start": 305, "end": 600}]}
pid = db.add_plan(sid, None, [item])
c = TestClient(main.app)

r = c.get(f"/api/plans/{pid}/items/0/cut-candidates").json()
b = r["boundaries"][0]
for x in b["candidates"]:
    print(f"  {x['t']:7.2f}  {x['kind']:16} gap={x['gap']} auto={x['auto']}")
best = max(b["candidates"], key=lambda x: x["kind"] == "black + silence")
assert best["kind"] == "black + silence" and 299 <= best["t"] <= 301 and best["auto"], b
assert any(abs(x["t"] - 250) < 1.5 for x in b["candidates"]), "the decoy flash is offered too"

img = c.get(f"/api/series/{sid}/frame", params={"source": rel, "t": 120}).content
assert img[:2] == b"\xff\xd8", "a JPEG"
assert c.get(f"/api/series/{sid}/frame", params={"source": "../../etc/passwd", "t": 1}).status_code in (400, 404)
snap = c.get(f"/api/plans/{pid}/items/0/snap", params={"t": 241.3}).json()["t"]
print("  241.3 snapped to", snap)
assert abs(snap - 242.128) < 0.01, snap  # keyframe at 242.0 in the file, which starts at -0.128 (audio priming)

assert c.put(f"/api/plans/{pid}/items/0/cuts", json={"cuts": [-5]}).status_code == 400
assert c.put(f"/api/plans/{pid}/items/0/cuts", json={"cuts": [snap]}).json()["cuts"] == [snap]

j = db.add_job("apply", sid, {"plan_id": pid})
res = executor.apply_plan(db.get_series(sid), db.get_plan(pid), JobContext(j, sid))
print("  apply:", res, "|", [l for l in db.get_job(j)["log"].splitlines() if "cut at" in l])


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "csv=p=0", str(p)], capture_output=True, text=True).stdout)


d1, d2 = dur(root / item["targets"][0]["path"]), dur(root / item["targets"][1]["path"])
print(f"  pieces: {d1:.1f}s + {d2:.1f}s")
orig = next((W / "media" / "tv" / "_episodeid_backup").rglob("*.mkv"))  # the original, kept
assert abs(d1 - 242) < 0.3 and abs(d1 + d2 - dur(orig)) < 0.3, (d1, d2, dur(orig))


def first_video_packet(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", "-read_intervals", "%+#1", str(p)], capture_output=True, text=True).stdout
    return out.splitlines()[0]


second = root / item["targets"][1]["path"]
print("  second piece starts with:", first_video_packet(second))
assert first_video_packet(second).endswith("K__") or ",K" in first_video_packet(second), "starts on a keyframe"

# Same with a file whose timestamps start at 1.4 s (MPEG-TS).
ts = W / "t.ts"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(orig), "-map", "0", "-c", "copy", str(ts)], check=True)
k = executor.snap_to_keyframe(ts, 241.3)
executor._ffmpeg_copy(ts, W / "a.ts", 0.0, k)
executor._ffmpeg_copy(ts, W / "b.ts", k, None)
print(f"  .ts: keyframe {k}, pieces {dur(W / 'a.ts'):.2f} + {dur(W / 'b.ts'):.2f} of {dur(ts):.2f}; b starts {first_video_packet(W / 'b.ts')}")
assert abs(dur(W / "a.ts") + dur(W / "b.ts") - dur(ts)) < 0.3 and ",K" in first_video_packet(W / "b.ts")
assert db.get_plan(pid)["items"][0]["cuts_used"] == [snap]
print("PASS")
