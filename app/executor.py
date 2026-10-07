"""Carrying out a plan, and undoing it.

Safety rules:
  * Nothing is ever deleted. Displaced files go to the backup folder, split
    originals go to <backup>/split_originals, undo moves generated files to
    <backup>/undone.
  * Every disk operation is written to the apply log as it happens, so even a
    crash halfway through can be undone.
  * Renames run in two passes through temporary names, so swaps and rotation
    chains (A→B, B→C, C→A) are safe.
  * Splits are lossless stream copies cut on a keyframe inside the black gap
    between episodes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import db
from .config import get_settings
from .planner import sidecars
from .media_text import SUB_EXTS


class ApplyError(RuntimeError):
    pass


# ------------------------------------------------------------------ splitting

def _blackdetect(video: Path, start: float, length: float) -> list[tuple[float, float]]:
    cmd = ["ffmpeg", "-v", "info", "-nostdin", "-ss", f"{max(0, start):.3f}", "-t",
           f"{length:.3f}", "-i", str(video), "-an", "-sn", "-dn",
           "-vf", "blackdetect=d=0.25:pix_th=0.12", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    out = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr):
        out.append((max(0, start) + float(m[1]), max(0, start) + float(m[2])))
    return out


def _silencedetect(video: Path, start: float, length: float) -> list[tuple[float, float]]:
    cmd = ["ffmpeg", "-v", "info", "-nostdin", "-ss", f"{max(0, start):.3f}", "-t",
           f"{length:.3f}", "-i", str(video), "-vn", "-sn", "-dn",
           "-af", "silencedetect=n=-45dB:d=0.4", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [(max(0, start) + a, max(0, start) + b) for a, b in zip(starts, ends)]


def _keyframes(video: Path, a: float, b: float) -> list[float]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-read_intervals", f"{max(0, a):.3f}%{b:.3f}", "-show_entries",
           "frame=pts_time,best_effort_timestamp_time", "-of", "csv=p=0", str(video)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    out = []
    for line in r.stdout.splitlines():
        for part in line.split(","):
            try:
                out.append(float(part))
                break
            except ValueError:
                continue
    return sorted(set(out))


def find_cut(video: Path, prev_end: float, next_start: float, ctx=None) -> dict:
    """Pick a keyframe inside the gap between two episodes."""
    center = (prev_end + next_start) / 2
    lo, hi = min(prev_end, next_start) - 75, max(prev_end, next_start) + 75
    blacks = [b for b in _blackdetect(video, lo, hi - lo) if b[1] - b[0] >= 0.25]
    method = "black"
    if not blacks:
        blacks = _silencedetect(video, lo, hi - lo)
        method = "silence"
    if blacks:
        # Prefer long gaps close to the dialogue boundary.
        gap = max(blacks, key=lambda b: (b[1] - b[0]) * 2 - abs((b[0] + b[1]) / 2 - center) / 30)
        target = (gap[0] + gap[1]) / 2
    else:
        gap, target, method = None, center, "estimate"
    kfs = _keyframes(video, target - 15, target + 15)
    if gap:
        inside = [k for k in kfs if gap[0] - 0.05 <= k <= gap[1] + 0.05]
        if inside:
            kfs = inside
    cut = min(kfs, key=lambda k: abs(k - target)) if kfs else target
    info = {"cut": round(cut, 3), "method": method,
            "gap": [round(gap[0], 2), round(gap[1], 2)] if gap else None}
    if ctx:
        ctx.log(f"    cut at {cut:.2f}s ({method}{' gap ' + str(info['gap']) if gap else ''})")
    return info


def _ffmpeg_copy(src: Path, dst: Path, start: float | None, dur: float | None) -> None:
    base = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    if start:
        base += ["-ss", f"{start:.3f}"]
    base += ["-i", str(src)]
    if dur:
        base += ["-t", f"{dur:.3f}"]
    tail = ["-c", "copy", "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size",
            "4096", str(dst)]
    for maps in (["-map", "0"], ["-map", "0:v", "-map", "0:a?", "-map", "0:s?"]):
        r = subprocess.run(base + maps + tail, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return
        dst.unlink(missing_ok=True)
    raise ApplyError(f"ffmpeg failed splitting {src.name}: {r.stderr.strip()[-400:]}")


# --------------------------------------------------------------------- apply

class _Log:
    def __init__(self, apply_id: int):
        self.apply_id = apply_id
        self.ops: list[dict] = []

    def add(self, op: dict) -> None:
        self.ops.append(op)
        db.update_apply_log(self.apply_id, self.ops)


def _move(src: Path, dst: Path, log: _Log, root: Path, note: str = "") -> None:
    if dst.exists():
        raise ApplyError(f"Refusing to overwrite {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst) if _same_fs(src, dst.parent) else shutil.move(str(src), str(dst))
    log.add({"op": "move", "src": str(src.relative_to(root)), "dst": str(dst.relative_to(root)),
             "note": note})


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _free(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _sidecar_dst(side: Path, old_video: Path, new_video: Path) -> Path:
    rest = side.name[len(old_video.with_suffix("").name):]
    return new_video.parent / (new_video.with_suffix("").name + rest)


def apply_plan(series: dict, plan: dict, ctx) -> dict:
    s = get_settings()
    root = Path(series["path"])
    backup = root / s["backup_folder"]
    items = [it for it in plan["items"] if it.get("selected") and
             it["kind"] in ("rename", "split", "aside")]
    if not items:
        return {"applied": 0}

    # Validate first: every source must still exist, targets must be in the series folder.
    for it in items:
        src = root / it["source"]
        if not src.exists():
            raise ApplyError(f"Source no longer exists: {it['source']} — rescan first.")
        for t in it.get("targets") or []:
            p = (root / t["path"]).resolve()
            if root.resolve() not in p.parents:
                raise ApplyError(f"Target escapes the series folder: {t['path']}")

    apply_id = db.add_apply(series["id"], plan["id"], [])
    log = _Log(apply_id)
    ctx.log(f"Apply #{apply_id}: {len(items)} items")
    pending: list[tuple[Path, Path, str]] = []  # (temp, final, note)
    tmp_n = 0

    def tmp_name(near: Path, suffix: str) -> Path:
        nonlocal tmp_n
        tmp_n += 1
        return near.parent / f".episodeid_tmp_{apply_id}_{tmp_n}{suffix}"

    # 1. Splits: write pieces to temp names, then move the original into the backup.
    for it in [i for i in items if i["kind"] == "split"]:
        ctx.check_cancel()
        src = root / it["source"]
        ctx.status(f"Splitting {src.name}")
        ctx.log(f"Split {it['source']} → {len(it['targets'])} files")
        targets = sorted(it["targets"], key=lambda t: t["start"])
        cuts = [0.0]
        for a, b in zip(targets, targets[1:]):
            cuts.append(find_cut(src, a["end"], b["start"], ctx)["cut"])
        cuts.append(None)
        for i, t in enumerate(targets):
            final = root / t["path"]
            final.parent.mkdir(parents=True, exist_ok=True)
            tmp = tmp_name(final, src.suffix)
            start = cuts[i]
            dur = (cuts[i + 1] - start) if cuts[i + 1] is not None else None
            _ffmpeg_copy(src, tmp, start, dur)
            log.add({"op": "create", "dst": str(tmp.relative_to(root)),
                     "note": f"split piece of {it['source']}"})
            pending.append((tmp, final, f"split {t['codes'][0]}"))
        dst = _free(backup / "split_originals" / it["source"])
        for side in sidecars(src):
            _move(side, _free(_sidecar_dst(side, src, dst)), log, root, "sidecar")
        _move(src, dst, log, root, "split original")

    # 2. Aside moves (duplicates / unverified) — straight to the backup folder.
    for it in [i for i in items if i["kind"] == "aside"]:
        src = root / it["source"]
        dst = _free(root / it["targets"][0]["path"])
        for side in sidecars(src):
            _move(side, _free(_sidecar_dst(side, src, dst)), log, root, "sidecar")
        _move(src, dst, log, root, it.get("bucket", "aside"))
        ctx.log(f"Aside {it['source']} → {dst.relative_to(root)}")

    # 3. Renames, pass one: everything to a temp name next to where it is now.
    for it in [i for i in items if i["kind"] == "rename"]:
        src = root / it["source"]
        final = root / it["targets"][0]["path"]
        tmp = tmp_name(src, src.suffix)
        for side in sidecars(src):
            if side.suffix.lower() in SUB_EXTS:
                stmp = tmp_name(side, side.suffix)
                _move(side, stmp, log, root, "sidecar temp")
                pending.append((stmp, _sidecar_dst(side, src, final), "sidecar"))
            else:  # .nfo / thumbnails describe the old episode — keep them out of the way
                _move(side, _free(backup / "metadata" / side.relative_to(root)), log, root,
                      "stale metadata")
        _move(src, tmp, log, root, "temp")
        pending.append((tmp, final, f"rename {it['source']}"))

    # 4. Pass two: temp names to their final names.
    for tmp, final, note in pending:
        ctx.check_cancel()
        if final.exists():
            # Something not in the plan is sitting there; never overwrite it.
            aside = _free(backup / "conflicts" / final.relative_to(root))
            _move(final, aside, log, root, "conflict")
            ctx.log(f"  ! {final.relative_to(root)} was in the way; moved to "
                    f"{aside.relative_to(root)}")
        _move(tmp, final, log, root, note)
        ctx.log(f"  → {final.relative_to(root)}")

    db.update_plan(plan["id"], status="applied")
    return {"apply_id": apply_id, "applied": len(items), "operations": len(log.ops)}


# ---------------------------------------------------------------------- undo

def undo_apply(series: dict, apply: dict, ctx) -> dict:
    s = get_settings()
    root = Path(series["path"])
    backup = root / s["backup_folder"]
    ops = apply["log"]
    restored, parked, problems = 0, 0, []
    for op in reversed(ops):
        ctx.check_cancel()
        if op["op"] == "move":
            cur, orig = root / op["dst"], root / op["src"]
            if not cur.exists():
                problems.append(f"missing {op['dst']}")
                continue
            if orig.exists():
                alt = _free(backup / "undo_conflicts" / op["src"])
                alt.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), str(alt))
                problems.append(f"{op['src']} was occupied; restored copy is at "
                                f"{alt.relative_to(root)}")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(orig))
            restored += 1
        elif op["op"] == "create":
            cur = root / op["dst"]
            if cur.exists():
                dst = _free(backup / "undone" / Path(op["dst"]).name.lstrip("."))
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), str(dst))
                parked += 1
    for p in problems:
        ctx.log(f"  ! {p}")
    db.mark_undone(apply["id"])
    ctx.log(f"Restored {restored} moves; {parked} generated files parked in "
            f"{s['backup_folder']}/undone.")
    return {"restored": restored, "parked": parked, "problems": problems}


# -------------------------------------------------------------------- backup

def backup_dir(series: dict) -> Path:
    """The series' backup folder. Refuses settings that would point anywhere but a
    plain subfolder of the series (empty, ".", "..", nested paths)."""
    name = (get_settings()["backup_folder"] or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"Backup folder setting {name!r} isn't a plain folder name")
    root = Path(series["path"]).resolve()
    d = root / name
    if d.is_symlink() or (d.exists() and d.resolve().parent != root):
        raise ValueError(f"{d} isn't a folder inside the series")
    return d


def _walk_files(d: Path):
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for n in filenames + [x for x in dirnames if os.path.islink(os.path.join(dirpath, x))]:
            yield Path(dirpath) / n


def backup_summary(series: dict) -> dict:
    d = backup_dir(series)
    buckets: dict[str, dict] = {}
    if d.is_dir():
        for f in _walk_files(d):
            rel = f.relative_to(d)
            b = rel.parts[0] if len(rel.parts) > 1 else "(top level)"
            e = buckets.setdefault(b, {"name": b, "files": 0, "bytes": 0})
            e["files"] += 1
            try:
                e["bytes"] += 0 if f.is_symlink() else f.stat().st_size
            except OSError:
                pass
    out = sorted(buckets.values(), key=lambda b: b["name"])
    return {"folder": d.name, "buckets": out, "files": sum(b["files"] for b in out),
            "bytes": sum(b["bytes"] for b in out)}


def delete_backup(series: dict, buckets: list[str]) -> dict:
    """Permanently delete the backed-up files in the chosen subfolders."""
    d = backup_dir(series)
    if not d.is_dir():
        return {"deleted": 0, "bytes": 0, "errors": []}
    wanted = set(buckets)
    deleted, freed, errors = 0, 0, []
    for f in list(_walk_files(d)):
        rel = f.relative_to(d)
        b = rel.parts[0] if len(rel.parts) > 1 else "(top level)"
        if b not in wanted:
            continue
        try:
            size = 0 if f.is_symlink() else f.stat().st_size
            f.unlink()  # a symlink is removed itself, never followed
            deleted, freed = deleted + 1, freed + size
        except OSError as e:
            errors.append(f"{rel}: {e.strerror or e}")
    for dirpath, _dirs, _files in sorted(os.walk(d, followlinks=False), key=lambda w: -len(w[0])):
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass
    return {"deleted": deleted, "bytes": freed, "errors": errors[:50]}


# -------------------------------------------------------------------- Sonarr

def sonarr_rescan(series: dict, ctx) -> None:
    import httpx
    s = get_settings()
    if not (s["sonarr_url"] and s["sonarr_api_key"]):
        return
    base = s["sonarr_url"].rstrip("/")
    h = {"X-Api-Key": s["sonarr_api_key"]}
    try:
        r = httpx.get(f"{base}/api/v3/series", params={"tvdbId": series["tvdb_id"]},
                      headers=h, timeout=30)
        r.raise_for_status()
        found = r.json()
        if not found:
            ctx.log("Sonarr: series not found by TVDB ID; skipped rescan.")
            return
        sid = found[0]["id"]
        httpx.post(f"{base}/api/v3/command", json={"name": "RescanSeries", "seriesId": sid},
                   headers=h, timeout=30).raise_for_status()
        ctx.log(f"Sonarr: rescan queued for '{found[0].get('title')}'.")
    except Exception as e:  # noqa: BLE001
        ctx.log(f"Sonarr rescan failed: {e}")
