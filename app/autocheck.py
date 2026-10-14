"""Daily check for added, replaced or removed episode files.

Each series' video files are listed (name, size, modified time — no reading) and
compared with what the last scan, apply or undo left. When something changed, the
series' card is flagged and references are fetched for any new episodes (cheap: only
missing ones). It does NOT scan — that's Whisper and OCR work, left for you to start
with Scan files. The flag clears when the series is scanned."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, jobs
from .config import CONFIG_DIR, get_settings
from .planner import backup_folder_name, list_videos

log = logging.getLogger("episodeid.autocheck")
TICK = 600  # seconds between looks at the schedule


def _state_file(sid: int) -> Path:
    return CONFIG_DIR / "autocheck" / f"{sid}.json"


def load_state(sid: int) -> dict:
    try:
        return json.loads(_state_file(sid).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(sid: int, st: dict) -> None:
    f = _state_file(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(f)


def snapshot(series: dict) -> dict[str, str]:
    root = Path(series["path"])
    out = {}
    for v in list_videos(root, backup_folder_name()):
        try:
            st = v.stat()
        except OSError:
            continue
        out[str(v.relative_to(root))] = f"{st.st_size}:{st.st_mtime_ns}"
    return out


def record(series: dict) -> None:
    """Remember the files as they are now (after a full scan, an apply or an undo)."""
    if not Path(series["path"]).is_dir():
        return
    st = load_state(series["id"])
    st.update(files=snapshot(series), recorded_at=db.now())
    st.pop("pending", None)  # scanned / applied: nothing waiting any more
    _save_state(series["id"], st)


def diff(old: dict, new: dict) -> dict:
    return {"added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "changed": sorted(k for k in set(old) & set(new) if not _same(old[k], new[k]))}


def _same(a: str, b: str) -> bool:
    if a.endswith(":"):  # recorded from a scan: size only
        return a.split(":")[0] == b.split(":")[0]
    return a == b


def check(series: dict, submit: bool = True) -> dict | None:
    """Compare the series' files with the last record; queue a refresh if they differ.
    Returns the differences, or None when nothing could be compared."""
    sid = series["id"]
    st = load_state(sid)
    root = Path(series["path"])
    if not root.is_dir():
        return None  # unmounted share: don't treat every file as removed
    if "files" not in st:
        scan = db.latest_scan(sid)
        if scan is None:
            return None  # never scanned: leave it to you
        # First check after updating: compare with the last scan (names and sizes).
        st["files"] = {f["rel"]: f"{f.get('size')}:" for f in scan["results"].get("files", [])}
    now_files = snapshot(series)
    d = diff(st["files"], now_files)
    st["checked_at"] = db.now()
    pending = {k: len(v) for k, v in d.items()} if any(d.values()) else None
    if pending:
        pending["examples"] = (d["added"] + d["changed"] + d["removed"])[:8]
        prev = st.get("pending") or {}
        pending["since"] = prev.get("since") if prev.get("files") == d else db.now()
        # fetch once per new set of changes (again if it couldn't be queued last time)
        fetch = bool(d["added"] or d["changed"]) and not (prev.get("files") == d and prev.get("fetched"))
        pending["files"] = d
        st["pending"] = pending
    else:
        st.pop("pending", None)
        fetch = False
    _save_state(sid, st)
    if fetch and submit and not db.active_job(sid):
        jobs.submit("fetch_refs", sid, {"auto": True})
        st["pending"]["fetched"] = True
        _save_state(sid, st)
    if pending:
        log.info("%s: %s added, %s replaced, %s removed since the last scan", series["name"],
                 len(d["added"]), len(d["changed"]), len(d["removed"]))
    return d


def _due(st: dict, hours: float) -> bool:
    at = st.get("checked_at")
    if not at:
        return True
    try:
        last = datetime.fromisoformat(at)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= hours * 3600


def run_due() -> int:
    hours = float(get_settings()["auto_check_hours"] or 0)
    if hours <= 0:
        return 0
    n = 0
    for s in db.list_series():
        if not _due(load_state(s["id"]), hours):
            continue
        try:
            check(db.get_series(s["id"]))
            n += 1
        except Exception as e:  # noqa: BLE001 — one series must not stop the rest
            log.warning("series %s: file check failed: %s", s.get("name"), e)
    return n


def _loop():
    time.sleep(120)  # let startup work (backup moves, cache warm-up) go first
    while True:
        try:
            run_due()
        except Exception as e:  # noqa: BLE001
            log.warning("file check failed: %s", e)
        time.sleep(TICK)


def start() -> None:
    threading.Thread(target=_loop, name="autocheck", daemon=True).start()
