"""Builds a synthetic library + references and runs scan → plan → apply → undo
against the real code paths (TVDB is bypassed by inserting episodes directly)."""
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

W = Path(os.environ["EPID_TEST_DIR"])
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"), WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app import pipeline  # noqa: E402,F401
from app.jobs import JobContext  # noqa: E402
from app.planner import build_plan  # noqa: E402

random.seed(7)
EP_LEN = 300
SYL = ["ka", "lo", "mi", "ra", "tu", "ne", "so", "vi", "da", "pe", "zu", "gor", "fin", "bel", "tro"]
COMMON = "the you and what is it we that this are do not have go here there now".split()


def word():
    return "".join(random.choice(SYL) for _ in range(random.randint(2, 3)))


def episode_lines(n_lines=70):
    vocab = [word() for _ in range(60)]
    lines = []
    for _ in range(n_lines):
        k = random.randint(5, 10)
        lines.append(" ".join(random.choice(COMMON + vocab * 2) for _ in range(k)))
    return lines


def srt(lines, offset=0.0, length=EP_LEN):
    out = []
    step = (length - 10) / len(lines)
    for i, l in enumerate(lines):
        st = offset + 5 + i * step
        en = st + step * 0.8
        f = lambda t: f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(t * 1000 % 1000):03d}"
        out.append(f"{i + 1}\n{f(st)} --> {f(en)}\n{l}\n")
    return "\n".join(out)


def make_video(path: Path, parts: list, with_subs=True):
    """parts: list of episode indexes; each part = 2s black + grey body + 2s black."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = W / "tmp"
    tmp.mkdir(exist_ok=True)
    total = EP_LEN * len(parts)
    # grey video with black frames at every episode boundary
    black = "+".join(f"between(t,{i * EP_LEN - 2},{i * EP_LEN + 2})" for i in range(1, len(parts)))
    vf = f"drawbox=c=black:t=fill:enable='{black}'" if black else "null"
    srt_lines = []
    for i, p in enumerate(parts):
        srt_lines.append(srt(EPS[p], offset=i * EP_LEN))
    sub = tmp / (path.stem + ".srt")
    # renumber cues
    blocks = "\n".join(srt_lines).split("\n\n")
    renum = []
    n = 1
    for b in blocks:
        ls = b.strip().split("\n")
        if len(ls) >= 3:
            renum.append(f"{n}\n" + "\n".join(ls[1:]))
            n += 1
    sub.write_text("\n\n".join(renum) + "\n")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"color=c=gray:s=160x90:r=10:d={total}", "-f", "lavfi", "-i",
           f"sine=f=440:d={total}"]
    if with_subs:
        cmd += ["-i", str(sub)]
    cmd += ["-vf", vf, "-g", "20", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-b:a", "32k"]
    if with_subs:
        cmd += ["-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:s", "srt",
                "-metadata:s:s:0", "language=eng"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- build
if W.exists():
    shutil.rmtree(W)
EPS = {i: episode_lines() for i in range(1, 11)}
TITLES = {i: f"Title {chr(64 + i)}" for i in range(1, 11)}
root = W / "media" / "tv" / "Test Show"
s1 = root / "Season 01"

make_video(s1 / "Test Show - S01E01 - Title A WEBDL-1080p.mkv", [1])
make_video(s1 / "Test Show - S01E02 - Title B WEBDL-1080p.mkv", [5])   # really E05
make_video(s1 / "Test Show - S01E05 - Title E WEBDL-1080p.mkv", [2])   # really E02
make_video(s1 / "Test Show - S01E03-E04 - Title C + Title D.mkv", [4, 3])  # order swapped
make_video(s1 / "Test Show - S01E06 - Title F.mkv", [6], with_subs=False)  # no text
make_video(s1 / "Test Show - S01E07 - Title G.mkv", [8])              # really E08
make_video(s1 / "Test Show - S01E08 - Title H.mkv", [9], with_subs=False)  # unverified, name needed
make_video(s1 / "Test Show - S01E10 - Title J.mkv", [1])              # duplicate of E01
(s1 / "Test Show - S01E02 - Title B WEBDL-1080p.en.srt").write_text(srt(EPS[5]))
(s1 / "Test Show - S01E02 - Title B WEBDL-1080p.nfo").write_text("<episodedetails/>")

# references: perturbed copies (drop ~25% of words) for every episode except 9, 10
refdir = W / "cache" / "refs" / "999"
refdir.mkdir(parents=True)
for i in range(1, 9):
    words = " ".join(EPS[i]).split()
    kept = [w for w in words if random.random() > 0.25]
    (refdir / f"S01E{i:02d}.json").write_text(json.dumps({"source": "opensubtitles",
                                                          "text": " ".join(kept)}))

db.init()
sid = db.add_series(999, "Test Show", "2020", str(root), "tt0000001")
db.update_series(sid, episodes=[{"season": 1, "episode": i, "title": TITLES[i],
                                 "overview": f"Overview {i}", "tvdb_episode_id": i}
                                for i in range(1, 11)])

# ----------------------------------------------------------------- scan
job = db.add_job("scan", sid, {})
ctx = JobContext(job, sid)
res = pipeline.job_scan(ctx, {})
print(db.get_job(job)["log"])
plan = db.get_plan(res["plan_id"])
for it in plan["items"]:
    tg = [t["path"] for t in it.get("targets") or []]
    print(f"{it['kind']:7} sel={it['selected']!s:5} {it['source']}\n         -> {tg}  ({it['reason']})")

before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
# ---------------------------------------------------------------- apply
job = db.add_job("apply", sid, {"plan_id": plan["id"]})
ctx = JobContext(job, sid)
r = pipeline.job_apply(ctx, {"plan_id": plan["id"]})
print(db.get_job(job)["log"])
print("APPLY", r)
after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
print("\nFILES AFTER APPLY:\n  " + "\n  ".join(after))

# verify split pieces by rescanning
job = db.add_job("scan", sid, {})
ctx = JobContext(job, sid)
res2 = pipeline.job_scan(ctx, {})
print("\nRESCAN:\n" + db.get_job(job)["log"])

# ----------------------------------------------------------------- undo
job = db.add_job("undo", sid, {"apply_id": r["apply_id"]})
ctx = JobContext(job, sid)
print(pipeline.job_undo(ctx, {"apply_id": r["apply_id"]}))
restored = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                  and "_episodeid_backup" not in str(p))
before_nb = [b for b in before]
print("UNDO RESTORED ORIGINAL SET:", restored == before_nb)
if restored != before_nb:
    print(set(restored) ^ set(before_nb))
