"""Background job runner. One worker thread runs jobs in order, so a scan and
an apply can never touch the same files at the same time."""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Callable

from . import db

log = logging.getLogger("episodeid.jobs")

_q: "queue.Queue[int]" = queue.Queue()
_handlers: dict[str, Callable[["JobContext", dict], object]] = {}
_cancel: set[int] = set()


class Cancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: int, series_id: int | None):
        self.job_id = job_id
        self.series_id = series_id
        self._last_progress = 0.0

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        log.info("job %s: %s", self.job_id, msg)
        db.append_job_log(self.job_id, line)

    def status(self, msg: str) -> None:
        db.update_job(self.job_id, message=msg)

    def progress(self, frac: float, msg: str | None = None) -> None:
        now = time.time()
        if msg is None and now - self._last_progress < 0.5 and frac < 1:
            return
        self._last_progress = now
        fields = {"progress": max(0.0, min(1.0, frac))}
        if msg is not None:
            fields["message"] = msg
        db.update_job(self.job_id, **fields)

    def check_cancel(self) -> None:
        if self.job_id in _cancel:
            raise Cancelled()


def handler(kind: str):
    def deco(fn):
        _handlers[kind] = fn
        return fn
    return deco


def submit(kind: str, series_id: int | None, params: dict | None = None) -> int:
    if kind not in _handlers:
        raise ValueError(f"Unknown job kind {kind}")
    job_id = db.add_job(kind, series_id, params or {})
    _q.put(job_id)
    return job_id


def cancel(job_id: int) -> None:
    _cancel.add(job_id)


def _run(job_id: int) -> None:
    job = db.get_job(job_id)
    if job is None:
        return
    if job_id in _cancel:
        db.update_job(job_id, status="cancelled", finished=db.now(), message="Cancelled")
        return
    ctx = JobContext(job_id, job["series_id"])
    db.update_job(job_id, status="running", started=db.now())
    try:
        result = _handlers[job["kind"]](ctx, job["params"])
        db.update_job(job_id, status="done", finished=db.now(), progress=1.0,
                      result=result if isinstance(result, (dict, list)) else {})
    except Cancelled:
        ctx.log("Cancelled.")
        db.update_job(job_id, status="cancelled", finished=db.now(), message="Cancelled")
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI
        ctx.log(f"ERROR: {e}")
        ctx.log(traceback.format_exc())
        db.update_job(job_id, status="failed", finished=db.now(), message=str(e)[:500])
    finally:
        _cancel.discard(job_id)


def _worker() -> None:
    while True:
        job_id = _q.get()
        try:
            _run(job_id)
        except Exception:  # noqa: BLE001
            log.exception("worker crashed on job %s", job_id)


def start() -> None:
    threading.Thread(target=_worker, name="episodeid-worker", daemon=True).start()
