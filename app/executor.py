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
from .planner import backup_folder_name, backup_rel, backup_root, sidecars
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
    """Keyframe times between a and b, in seconds from the start of the file — the
    same clock as players, black/silence detection, frame grabs and the cutter. (A
    file's own timestamps may not start at zero: .ts recordings often start at 1.4 s,
    and audio priming can put an MKV's start slightly before zero.)"""
    t0 = _start_time(video)
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-read_intervals", f"{max(0.0, a + t0 - 1):.3f}%{b + t0 + 1:.3f}", "-show_entries",
           "frame=pts_time,best_effort_timestamp_time", "-of", "csv=p=0", str(video)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    out = []
    for line in r.stdout.splitlines():
        for part in line.split(","):
            try:
                out.append(round(float(part) - t0, 3))
                break
            except ValueError:
                continue
    return sorted({x for x in out if a - 1e-3 <= x <= b + 1e-3})


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


def cut_candidates(video: Path, prev_end: float, next_start: float, k: int = 6) -> dict:
    """Places a split between two episodes could go, for you to choose from: black or
    silent gaps near where the dialogue changes episode, each snapped to a keyframe
    (a lossless split can only start on one), best first. Includes the one Apply
    would pick by itself."""
    auto = find_cut(video, prev_end, next_start)["cut"]
    center = (prev_end + next_start) / 2
    lo, hi = min(prev_end, next_start) - 90, max(prev_end, next_start) + 90
    lo = max(0.0, lo)
    gaps = [(a, b, "black") for a, b in _blackdetect(video, lo, hi - lo) if b - a >= 0.25]
    for a, b in _silencedetect(video, lo, hi - lo):
        hit = next((g for g in gaps if g[0] - 0.5 <= b and a <= g[1] + 0.5), None)
        if hit:  # black and silent at once: the clearest kind of break
            gaps[gaps.index(hit)] = (min(a, hit[0]), max(b, hit[1]), "black + silence")
        else:
            gaps.append((a, b, "silence"))
    kfs = _keyframes(video, lo - 10, hi + 10)

    def snap(t: float, a: float | None = None, b: float | None = None) -> float:
        inside = [x for x in kfs if a is not None and a - 0.05 <= x <= b + 0.05]
        pool = inside or kfs
        return round(min(pool, key=lambda x: abs(x - t)), 3) if pool else round(t, 3)

    score = lambda g: (g[1] - g[0]) * 2 + (1.5 if g[2] == "black + silence" else 0) \
        - abs((g[0] + g[1]) / 2 - center) / 30  # noqa: E731
    out: list[dict] = []
    for a, b, kind in sorted(gaps, key=score, reverse=True):
        t = snap((a + b) / 2, a, b)
        if any(abs(t - c["t"]) < 1.0 for c in out):
            continue
        out.append({"t": t, "gap": [round(a, 2), round(b, 2)], "kind": kind,
                    "in_gap": a - 0.05 <= t <= b + 0.05})
        if len(out) >= k:
            break
    if not any(abs(c["t"] - auto) < 0.05 for c in out):
        out.insert(0, {"t": round(auto, 3), "gap": None, "kind": "estimate", "in_gap": False})
    for c in out:
        c["auto"] = abs(c["t"] - auto) < 0.05
    out.sort(key=lambda c: c["t"])
    return {"auto": round(auto, 3), "candidates": out, "window": [round(lo, 1), round(hi, 1)],
            "prev_end": prev_end, "next_start": next_start}


def snap_to_keyframe(video: Path, t: float) -> float:
    kfs = _keyframes(video, t - 10, t + 10)
    return round(min(kfs, key=lambda x: abs(x - t)), 3) if kfs else round(t, 3)


def frame_jpeg(video: Path, t: float, width: int = 320) -> bytes:
    """One frame of a video as a JPEG (cached)."""
    import hashlib
    from .config import CACHE_DIR
    from .media_text import _cache_key
    key = hashlib.sha1(f"{_cache_key(video)}|{t:.2f}|{width}".encode()).hexdigest()[:20]
    path = CACHE_DIR / "frames" / f"{key}.jpg"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(tmp)],
                       capture_output=True, timeout=120)
        if not tmp.exists():
            raise FileNotFoundError("no frame at that time")
        tmp.replace(path)
    return path.read_bytes()


def _start_time(src: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of",
                        "csv=p=0", str(src)], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip() or 0)
    except ValueError:
        return 0.0


def _duration(p: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "csv=p=0", str(p)], capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _check_piece(p: Path, want: float | None) -> str | None:
    """Why a split piece isn't right, or None if it is."""
    if not p.exists() or p.stat().st_size == 0:
        return "not written"
    got = _duration(p)
    if got is None:
        return "unreadable"
    if want and abs(got - want) > max(3.0, want * 0.02):
        return f"{got:.0f}s long, expected {want:.0f}s"
    for ss in (0.0, max(0.0, got - 3)):
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{ss:.2f}", "-i", str(p), "-t", "2",
                            "-map", "0:v:0", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return f"doesn't play at {'the start' if ss == 0 else 'the end'}"
    return None


def _check_space(root: Path, split_sources: list[Path]) -> None:
    """Split pieces are written before the original moves out of the way, so they need
    as much space again as the originals (plus a margin)."""
    need = sum(p.stat().st_size for p in split_sources if p.exists())
    if not need:
        return
    free = shutil.disk_usage(root).free
    margin = max(200 * 1024 ** 2, need // 20)
    if free < need + margin:
        gb = lambda n: f"{n / 1024 ** 3:.1f} GB"  # noqa: E731
        raise ApplyError(f"Not enough free space to split {len(split_sources)} file(s): needs about "
                         f"{gb(need + margin)}, {gb(free)} free. Nothing was changed. Free some space, "
                         "or untick the splits and apply the rest.")


def _ffmpeg_copy(src: Path, dst: Path, start: float | None, dur: float | None) -> None:
    # Seek near the cut first (fast), then trim on the file's own timestamps. Seeking
    # straight to the cut lands on the nearest *indexed* point, often a keyframe or two
    # early, and a stream copy keeps everything from there — the next episode would
    # begin with the last seconds of the previous one.
    t0 = _start_time(src)
    base = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    if start:
        base += ["-ss", f"{max(0.0, start - 15):.3f}"]
    base += ["-copyts", "-i", str(src)]
    if start:
        base += ["-ss", f"{t0 + start:.3f}"]
    if dur:
        base += ["-to", f"{t0 + (start or 0) + dur:.3f}"]
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
    log.add({"op": "move", "src": _rel(src, root), "dst": _rel(dst, root), "note": note})


def _rel(p: Path, root: Path) -> str:
    """Path relative to the series folder; the backup folder sits beside it ("../…")."""
    return os.path.relpath(p, root)


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
    root = Path(series["path"])
    migrate_legacy_backup(series, ctx)
    backup = backup_root(root)
    legacy, new = backup_folder_name() + "/", backup_rel(root) + "/"
    items = [it for it in plan["items"] if it.get("selected") and
             it["kind"] in ("rename", "split", "aside")]
    for it in items:  # plans made before the backup folder moved out of the series
        for t in it.get("targets") or []:
            if legacy != new and t["path"].startswith(legacy):
                t["path"] = new + t["path"][len(legacy):]
    if not items:
        return {"applied": 0}

    # Validate first: every source must still exist, targets must be in the series folder.
    for it in items:
        src = root / it["source"]
        if not src.exists():
            raise ApplyError(f"Source no longer exists: {it['source']} — rescan first.")
        for t in it.get("targets") or []:
            p = (root / t["path"]).resolve()
            if root.resolve() not in p.parents and backup.resolve() not in p.parents:
                raise ApplyError(f"Target escapes the series folder: {t['path']}")

    _check_space(root, [root / it["source"] for it in items if it["kind"] == "split"])

    apply_id = db.add_apply(series["id"], plan["id"], [])
    log = _Log(apply_id)
    ctx.log(f"Apply #{apply_id}: {len(items)} items")
    pending: list[tuple[Path, Path, str]] = []  # (temp, final, note)
    tmp_n = 0

    def tmp_name(near: Path, suffix: str) -> Path:
        nonlocal tmp_n
        tmp_n += 1
        return near.parent / f".episodeid_tmp_{apply_id}_{tmp_n}{suffix}"

    # 1. Splits: write every piece to a temp name and check it (right length, plays at the
    # start and the end) before ANY original moves. A failed write (disk full, a damaged
    # source) stops here with nothing changed.
    splits = []
    try:
        for it in [i for i in items if i["kind"] == "split"]:
            ctx.check_cancel()
            src = root / it["source"]
            ctx.status(f"Splitting {src.name}")
            ctx.log(f"Split {it['source']} → {len(it['targets'])} files")
            targets = sorted(it["targets"], key=lambda t: t["start"])
            chosen = it.get("cuts") or []
            cuts = [0.0]
            for n, (a, b) in enumerate(zip(targets, targets[1:])):
                if n < len(chosen) and chosen[n] is not None:
                    cuts.append(float(chosen[n]))
                    ctx.log(f"    cut at {float(chosen[n]):.2f}s (chosen)")
                else:
                    cuts.append(find_cut(src, a["end"], b["start"], ctx)["cut"])
            cuts.append(None)
            it["cuts_used"] = cuts[1:-1]
            total = _duration(src)
            pieces = []
            splits.append((it, src, pieces))
            for i, t in enumerate(targets):
                final = root / t["path"]
                final.parent.mkdir(parents=True, exist_ok=True)
                tmp = tmp_name(final, src.suffix)
                start = cuts[i]
                dur = (cuts[i + 1] - start) if cuts[i + 1] is not None else None
                pieces.append((tmp, final, t, dur if dur is not None else (total - start if total else None)))
                _ffmpeg_copy(src, tmp, start, dur)
            problems = [f"{t['path']}: {why}" for tmp, _f, t, want in pieces
                        for why in [_check_piece(tmp, want)] if why]
            if problems:
                raise ApplyError(f"Split of {it['source']} didn't come out right: {'; '.join(problems)}. "
                                 "Nothing was changed; every file is where it was.")
            ctx.log("    pieces checked: right length, play at start and end")
    except BaseException:
        for _it, _src, pieces in splits:
            for tmp, *_ in pieces:
                tmp.unlink(missing_ok=True)
        db.delete_apply(apply_id)
        raise
    for it, src, pieces in splits:
        for tmp, final, t, _want in pieces:
            log.add({"op": "create", "dst": _rel(tmp, root), "note": f"split piece of {it['source']}"})
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

    db.update_plan(plan["id"], status="applied", items=plan["items"])
    return {"apply_id": apply_id, "applied": len(items), "operations": len(log.ops)}


# ---------------------------------------------------------------------- undo

def undo_apply(series: dict, apply: dict, ctx) -> dict:
    root = Path(series["path"])
    migrate_legacy_backup(series, ctx)
    backup = backup_root(root)
    ops = db.get_apply(apply["id"])["log"]  # re-read: migration may have updated the paths
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
            f"{_rel(backup / 'undone', root)}.")
    return {"restored": restored, "parked": parked, "problems": problems}


# -------------------------------------------------------------------- backup

def backup_dir(series: dict) -> Path:
    """The series' backup folder (see planner.backup_root). Refuses anything that
    isn't a plain folder of its own: never the series folder or the TV folder."""
    root = Path(series["path"]).resolve()
    d = backup_root(root)
    if d.is_symlink() or d in (root, root.parent) or d.resolve() in (root, root.parent):
        raise ValueError(f"{d} isn't a backup folder of its own")
    return d


def migrate_legacy_backup(series: dict, ctx=None) -> int:
    """Move a backup folder from inside the series (where Sonarr sees it) to its place
    beside the series, and update the History so Undo still finds every file."""
    root = Path(series["path"]).resolve()
    old, new = root / backup_folder_name(), backup_root(root)
    if old == new or not old.is_dir() or old.is_symlink():
        return 0
    moved = 0
    for f in list(_walk_files(old)):
        dst = _free(new / f.relative_to(old))
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(f, dst) if _same_fs(f, dst.parent) else shutil.move(str(f), str(dst))
        moved += 1
    for dirpath, _d, _f in sorted(os.walk(old, followlinks=False), key=lambda w: -len(w[0])):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    a, b = backup_folder_name() + "/", os.path.relpath(new, root) + "/"
    for ap in db.list_applies(series["id"]):
        full = db.get_apply(ap["id"])
        changed = False
        for op in full["log"]:
            for k in ("src", "dst"):
                if isinstance(op.get(k), str) and op[k].startswith(a):
                    op[k], changed = b + op[k][len(a):], True
        if changed:
            db.update_apply_log(ap["id"], full["log"])
    msg = (f"Moved {moved} backed-up files from {old.name}/ inside the series to "
           f"{os.path.relpath(new, root.parent)}/ beside it, so Sonarr doesn't see them.")
    (ctx.log if ctx else log_info)(msg)
    return moved


def log_info(msg: str) -> None:
    import logging
    logging.getLogger("episodeid").info(msg)


def _walk_files(d: Path):
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for n in filenames + [x for x in dirnames if os.path.islink(os.path.join(dirpath, x))]:
            yield Path(dirpath) / n


def backup_summary(series: dict) -> dict:
    migrate_legacy_backup(series)
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
    from .config import MEDIA_ROOT
    shown = os.path.relpath(d, MEDIA_ROOT) if MEDIA_ROOT in d.parents else d.name
    return {"folder": shown, "buckets": out, "files": sum(b["files"] for b in out),
            "bytes": sum(b["bytes"] for b in out)}


def delete_backup(series: dict, buckets: list[str]) -> dict:
    """Permanently delete the backed-up files in the chosen subfolders."""
    migrate_legacy_backup(series)
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

def sonarr_series_type_note(series: dict) -> str | None:
    """A warning when Sonarr names this series differently (anime: absolute numbers;
    daily: air dates). EpisodeID always names by season and episode."""
    import httpx
    s = get_settings()
    if not (s["sonarr_url"] and s["sonarr_api_key"]):
        return None
    try:
        r = httpx.get(f"{s['sonarr_url'].rstrip('/')}/api/v3/series", params={"tvdbId": series["tvdb_id"]},
                      headers={"X-Api-Key": s["sonarr_api_key"]}, timeout=15)
        found = r.json() if r.status_code == 200 else []
    except Exception:  # noqa: BLE001 — only a warning
        return None
    found = [x for x in found if isinstance(x, dict) and x.get("tvdbId") == series["tvdb_id"]]
    kind = (found[0].get("seriesType") or "standard").lower() if found else "standard"
    if kind == "standard":
        return None
    how = "absolute episode numbers" if kind == "anime" else "air dates"
    return (f"Note: Sonarr treats this as a{'n' if kind == 'anime' else ''} {kind} series and names its files "
            f"with {how}. EpisodeID names by season and episode, so names won't match Sonarr's; "
            "Sonarr's Rename Files can put them back to its format after applying.")


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
