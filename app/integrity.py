"""File integrity: does each video decode cleanly from start to end?

A file can be the right episode and still be damaged — visual glitches, then audio and
video drifting apart. *Check files* decodes every frame (GPU when there is one) and
reads the container's stream lengths and start times:

  * decode errors, with roughly where they happen;
  * decoding that stops well before the file's stated length (a truncated download);
  * audio and video of clearly different lengths, or starting far apart.

Nothing is moved or deleted: results show in the plan and on the series card, and
re-downloading is left to you. Results are cached by file size and modified time, so
only new or replaced files are decoded again.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import CACHE_DIR, CONFIG_DIR, get_settings
from .jobs import handler

VERSION = 1
BAD_ERRORS = 5          # this many decode errors → damaged (fewer → worth a look)
MAX_PROBLEMS = 40       # problems kept per file (the count says how many in all)
# Warnings that point at damage (the rest of ffmpeg's warnings are routine).
_WARN_KEEP = re.compile(r"(?i)corrupt|invalid|non[- ]?monoton|out of order|concealing|missing|"
                        r"truncat|error|could not find ref|mismatch|damaged|overread|skipp")
_ADDR = re.compile(r" @ 0x[0-9a-f]+")


def _key(video: Path) -> str:
    st = video.stat()
    return hashlib.sha1(f"{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()


def _cache_file(key: str) -> Path:
    return CACHE_DIR / "integrity" / f"{key}.json"


def cached(video: Path) -> dict | None:
    try:
        r = json.loads(_cache_file(_key(video)).read_text())
        return r if r.get("version") == VERSION else None
    except (OSError, ValueError):
        return None


def _fmt(t: float | None) -> str:
    if t is None:
        return "?"
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def probe(video: Path) -> dict:
    """Container facts: duration, and each video/audio stream's start time and length."""
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                        str(video)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffprobe failed").strip().splitlines()[-1][:300])
    data = json.loads(r.stdout or "{}")
    f = lambda v: float(v) if v not in (None, "N/A", "") else None  # noqa: E731
    streams = []
    for s in data.get("streams") or []:
        if s.get("codec_type") in ("video", "audio") and not (s.get("disposition") or {}).get("attached_pic"):
            dur = f(s.get("duration"))
            # Matroska keeps stream lengths in tags ("00:22:01.123000000")
            tag = (s.get("tags") or {}).get("DURATION") or (s.get("tags") or {}).get("DURATION-eng")
            if dur is None and tag:
                m = re.match(r"(\d+):(\d+):([\d.]+)", tag)
                if m:
                    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            streams.append({"type": s["codec_type"], "codec": s.get("codec_name"),
                            "start": f(s.get("start_time")), "duration": dur})
    return {"duration": f((data.get("format") or {}).get("duration")), "streams": streams}


def _decode(video: Path, method: str, duration: float | None, ctx=None) -> dict:
    """Decode every video and audio frame. Returns {problems, count, decoded_to, rc}."""
    from .titlecard import _hw_args
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "level+warning", "-progress", "pipe:1",
           "-stats_period", "0.25", *_hw_args(method), "-i", str(video),
           "-map", "0:v:0?", "-map", "0:a?", "-f", "null", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    now = {"t": 0.0}

    def progress():
        for line in p.stdout:
            if line.startswith("out_time_us="):
                try:
                    now["t"] = max(now["t"], int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
            elif line.startswith("progress=") and ctx is not None and duration:
                try:
                    ctx.progress_file(min(1.0, now["t"] / duration))
                except AttributeError:
                    pass

    th = threading.Thread(target=progress, daemon=True)
    th.start()
    problems, count, seen = [], 0, {}
    try:
        for line in p.stderr:
            if ctx is not None:
                try:
                    ctx.check_cancel()
                except Exception:
                    p.kill()
                    raise
            line = line.strip()
            lvl = msg = None
            for lv in ("fatal", "error", "warning"):
                tag = f"[{lv}]"
                if tag in line:
                    lvl, msg = lv, line.replace(tag, "").strip()
                    break
            if not lvl:
                continue
            if lvl == "warning" and not _WARN_KEEP.search(msg):
                continue
            if msg.startswith("[null]") or "to muxer in stream" in msg:
                continue  # the null output's own timestamp complaints, not the file's
            msg = _ADDR.sub("", re.sub(r"\s+", " ", msg))[:200]
            count += 1
            k = re.sub(r"\d+", "#", msg)
            if k in seen and now["t"] - seen[k] < 2:
                continue  # the same complaint about the same moment
            seen[k] = now["t"]
            if len(problems) < MAX_PROBLEMS:
                problems.append({"t": round(now["t"], 1), "msg": msg, "level": lvl})
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
    th.join(timeout=5)
    return {"problems": problems, "count": count, "decoded_to": now["t"], "rc": p.returncode}


def check_file(video: Path, ctx=None, force: bool = False) -> dict:
    """Integrity result for one file (cached by size and modified time)."""
    if not force:
        c = cached(video)
        if c:
            return c
    from .titlecard import decode_method
    key = _key(video)
    t0 = time.time()
    res: dict = {"version": VERSION, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "issues": [], "problems": [], "errors": 0}
    try:
        info = probe(video)
    except Exception as e:  # noqa: BLE001
        res.update(status="bad", issues=[f"can't be read: {e}"])
        _save(key, res)
        return res
    dur = info["duration"]
    res["duration"] = dur
    method = decode_method()
    d = _decode(video, method, dur, ctx)
    if method != "cpu" and d["rc"] != 0 and d["decoded_to"] < 1 and \
            any(re.search(r"(?i)hwaccel|cuda|vaapi|device", x["msg"]) for x in d["problems"]):
        method = "cpu"  # the GPU couldn't take this file: decode it on the CPU instead
        d = _decode(video, method, dur, ctx)
    res.update(method=method, seconds=round(time.time() - t0, 1), errors=d["count"], problems=d["problems"],
               decoded_to=round(d["decoded_to"], 1))
    issues = []
    if dur and d["decoded_to"] < dur - max(10.0, 0.03 * dur):
        issues.append(f"decoding stopped at {_fmt(d['decoded_to'])} of {_fmt(dur)} — the file may be cut short")
    vids = [s for s in info["streams"] if s["type"] == "video"]
    auds = [s for s in info["streams"] if s["type"] == "audio"]
    if not vids:
        issues.append("no video stream")
    if vids and auds:
        v, a = vids[0], auds[0]
        if v["duration"] and a["duration"] and abs(v["duration"] - a["duration"]) > max(3.0, 0.02 * v["duration"]):
            issues.append(f"audio and video lengths differ ({_fmt(a['duration'])} audio, {_fmt(v['duration'])} video)")
        if v["start"] is not None and a["start"] is not None and abs(v["start"] - a["start"]) > 1.0:
            first = "audio" if a["start"] > v["start"] else "video"
            issues.append(f"{first} starts {abs(v['start'] - a['start']):.1f} s after the "
                          f"{'video' if first == 'audio' else 'audio'}")
    res["issues"] = issues
    truncated = any("cut short" in i for i in issues)
    res["status"] = "bad" if truncated or d["count"] >= BAD_ERRORS or (d["rc"] not in (0, None) and not
                                                                     d["decoded_to"]) else \
        "warn" if d["count"] or issues else "ok"
    _save(key, res)
    return res


def _save(key: str, res: dict) -> None:
    f = _cache_file(key)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(res))


def summary_line(res: dict) -> str:
    """One line for the log and the plan: what's wrong, and where."""
    parts = list(res.get("issues") or [])
    if res.get("errors"):
        times = sorted({_fmt(p["t"]) for p in res.get("problems") or []}, key=lambda s: [int(x) for x in s.split(":")])
        where = ", ".join(times[:4]) + (f" (+{len(times) - 4} more)" if len(times) > 4 else "")
        first = (res.get("problems") or [{}])[0].get("msg", "")
        parts.insert(0, f"{res['errors']} error{'s' if res['errors'] != 1 else ''} while decoding"
                        + (f" around {where}" if where else "") + (f" — e.g. “{first}”" if first else ""))
    return "; ".join(parts) or "decodes cleanly"


# ------------------------------------------------------------ per-series state

def _state_file(sid: int) -> Path:
    return CONFIG_DIR / "integrity" / f"{sid}.json"


def load_state(sid: int) -> dict:
    try:
        return json.loads(_state_file(sid).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(sid: int, st: dict) -> None:
    f = _state_file(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st))


def accept(series: dict, rel: str, undo: bool = False) -> dict:
    """Mark a file's playback warning as fine (you've watched it): it counts as OK from
    now on. Tied to the file itself (size and date), so it follows renames and drops off
    if the file is replaced."""
    p = Path(series["path"]) / rel
    if not p.is_file():
        return {"ok": False, "note": "file not found"}
    st = load_state(series["id"])
    acc = st.setdefault("accepted", {})
    key = _key(p)
    if undo:
        acc.pop(key, None)
    else:
        acc[key] = {"rel": rel, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save_state(series["id"], st)
    return {"ok": True}


def current(series: dict) -> dict:
    """{rel: result} for the series' files as they are now: results for files that were
    renamed (same size and date) follow them; replaced or removed files drop out. Files
    you marked as fine count as "ok" (with accepted, and their checked status kept as was)."""
    out = _current(series)
    acc = load_state(series["id"]).get("accepted") or {}
    for rel, v in out.items():
        if v.get("key") in acc and v.get("status") != "ok":
            out[rel] = {**v, "was": v["status"], "status": "ok", "accepted": acc[v["key"]].get("at")}
    return out


def _current(series: dict) -> dict:
    st = load_state(series["id"])
    if not st.get("files"):
        return {}
    by_key = {v.get("key"): v for v in st["files"].values() if v.get("key")}
    root = Path(series["path"])
    out = {}
    for rel, v in st["files"].items():
        p = root / rel
        try:
            if p.is_file() and _key(p) == v.get("key"):
                out[rel] = v
        except OSError:
            pass
    if len(out) < len(st["files"]):
        # renamed by an apply: find them by size and date
        from .planner import list_videos
        for p in list_videos(root, get_settings()["backup_folder"]):
            rel = str(p.relative_to(root))
            if rel not in out:
                try:
                    v = by_key.get(_key(p))
                except OSError:
                    continue
                if v:
                    out[rel] = v
    return out


def card_summary(series: dict) -> dict | None:
    st = load_state(series["id"])
    if not st.get("files"):
        return None
    bad = warn = 0
    for v in current(series).values():  # files as they are now (renamed ones follow; replaced drop out)
        bad += v.get("status") == "bad"
        warn += v.get("status") == "warn"
    if not st.get("checked_at") and not (bad or warn):
        return None  # only new downloads checked so far, all fine: nothing to say yet
    return {"checked_at": st.get("checked_at"), "checked": len(st.get("files") or {}), "bad": bad, "warn": warn}


@handler("check_files")
def job_check_files(ctx, params):
    from .planner import list_videos
    series = db.get_series(ctx.series_id)
    root = Path(series["path"])
    videos = list_videos(root, get_settings()["backup_folder"])
    if params.get("only"):
        only = set(params["only"])
        videos = [v for v in videos if str(v.relative_to(root)) in only]
    return run_check(series, videos, ctx, force=bool(params.get("force")), keep_others=bool(params.get("only")))


def check_paths(series: dict, rels: list[str], ctx) -> dict:
    """Check just these files (new downloads), keeping the series' other results."""
    root = Path(series["path"])
    videos = [root / r for r in rels if (root / r).is_file()]
    return run_check(series, videos, ctx, force=False, keep_others=True, quiet_start=True)


def run_check(series: dict, videos: list[Path], ctx, force: bool = False, keep_others: bool = False,
              quiet_start: bool = False) -> dict:
    from .titlecard import decode_method
    root = Path(series["path"])
    todo = [v for v in videos if force or not cached(v)]
    if quiet_start:
        ctx.log(f"Checking {len(videos)} new download(s) decode cleanly…")
    else:
        ctx.log(f"Checking {len(videos)} files decode cleanly ({len(todo)} to decode, "
                f"{len(videos) - len(todo)} already checked and unchanged; decoding on {decode_method().upper()}).")
    st = load_state(series["id"])
    files = dict(st.get("files") or {}) if keep_others else {}
    counts = {"ok": 0, "warn": 0, "bad": 0}
    for i, v in enumerate(videos):
        ctx.check_cancel()
        rel = str(v.relative_to(root))
        base = i / max(1, len(videos))
        ctx.progress(base, f"Checking {v.name}")
        ctx.progress_file = lambda frac, base=base, name=v.name: ctx.progress(
            base + frac / max(1, len(videos)), f"Checking {name}")
        try:
            r = check_file(v, ctx, force=force)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "Cancelled":
                raise
            ctx.log(f"  ? {rel}: couldn't check — {e}")
            continue
        counts[r["status"]] += 1
        files[rel] = {"key": _key(v), "status": r["status"], "summary": summary_line(r),
                      "issues": r.get("issues") or [], "errors": r.get("errors", 0),
                      "problems": (r.get("problems") or [])[:12], "checked_at": r.get("checked_at")}
        if r["status"] != "ok":
            ctx.log(f"  {'⚠' if r['status'] == 'bad' else '?'} {rel}: {summary_line(r)}")
        elif quiet_start:
            ctx.log(f"  ✓ {rel}: decodes cleanly")
        # save as we go: a cancelled or interrupted check keeps what it found
        # checked_at is when the whole series was last checked (new downloads alone don't count)
        _save_state(series["id"], {"checked_at": st.get("checked_at") if quiet_start else db.now(),
                                   "files": files})
    if not quiet_start:
        ctx.log(f"Done: {counts['ok']} clean, {counts['warn']} worth a look, {counts['bad']} damaged."
                + (" Damaged files need a new copy — Re-download… in the Plan tab (Damaged)." if counts["bad"] else ""))
    return counts
