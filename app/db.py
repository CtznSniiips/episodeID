"""SQLite store. Rows are small; larger structures are stored as JSON text."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import CONFIG_DIR

DB_PATH = CONFIG_DIR / "episodeid.db"
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tvdb_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    year TEXT,
    path TEXT NOT NULL UNIQUE,
    imdb_id TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    episodes TEXT NOT NULL DEFAULT '[]',
    episodes_updated TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    created TEXT NOT NULL,
    results TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    scan_id INTEGER,
    created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    items TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    plan_id INTEGER,
    created TEXT NOT NULL,
    log TEXT NOT NULL,
    undone_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    result TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)
    # Jobs that were running when the container stopped will never finish.
    conn().execute(
        "UPDATE jobs SET status='failed', message='Interrupted by restart', finished=? "
        "WHERE status IN ('queued','running')", (now(),))


def _row(r: sqlite3.Row | None, json_fields=()) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d


# ---- series -------------------------------------------------------------

SERIES_JSON = ("options", "episodes")


def add_series(tvdb_id: int, name: str, year: str, path: str, imdb_id: str | None,
               options: dict | None = None) -> int:
    cur = conn().execute(
        "INSERT INTO series (tvdb_id, name, year, path, imdb_id, options, created) "
        "VALUES (?,?,?,?,?,?,?)",
        (tvdb_id, name, year, path, imdb_id, json.dumps(options or {}), now()))
    return cur.lastrowid


def get_series(series_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    return _row(r, SERIES_JSON)


def list_series() -> list[dict]:
    rows = conn().execute("SELECT * FROM series ORDER BY name").fetchall()
    return [_row(r, SERIES_JSON) for r in rows]


def update_series(series_id: int, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k in SERIES_JSON:
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(series_id)
    conn().execute(f"UPDATE series SET {', '.join(sets)} WHERE id=?", vals)


def delete_series(series_id: int) -> None:
    conn().execute("DELETE FROM series WHERE id=?", (series_id,))


# ---- scans / plans / applies -------------------------------------------

def add_scan(series_id: int, results: dict) -> int:
    return conn().execute(
        "INSERT INTO scans (series_id, created, results) VALUES (?,?,?)",
        (series_id, now(), json.dumps(results))).lastrowid


def latest_scan(series_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM scans WHERE series_id=? ORDER BY id DESC LIMIT 1",
                       (series_id,)).fetchone()
    return _row(r, ("results",))


def latest_scan_summary(series_id: int) -> dict | None:
    """Created time and status counts of the latest scan, without loading its results."""
    r = conn().execute(
        "SELECT id, created, json_extract(results, '$.counts') AS counts FROM scans "
        "WHERE series_id=? ORDER BY id DESC LIMIT 1", (series_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "created": r["created"], "counts": json.loads(r["counts"] or "{}")}


def add_plan(series_id: int, scan_id: int | None, items: list) -> int:
    return conn().execute(
        "INSERT INTO plans (series_id, scan_id, created, items) VALUES (?,?,?,?)",
        (series_id, scan_id, now(), json.dumps(items))).lastrowid


def get_plan(plan_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    return _row(r, ("items",))


def latest_plan(series_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM plans WHERE series_id=? ORDER BY id DESC LIMIT 1",
                       (series_id,)).fetchone()
    return _row(r, ("items",))


def update_plan(plan_id: int, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k == "items":
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(plan_id)
    conn().execute(f"UPDATE plans SET {', '.join(sets)} WHERE id=?", vals)


def add_apply(series_id: int, plan_id: int, log: list) -> int:
    return conn().execute(
        "INSERT INTO applies (series_id, plan_id, created, log) VALUES (?,?,?,?)",
        (series_id, plan_id, now(), json.dumps(log))).lastrowid


def update_apply_log(apply_id: int, log: list) -> None:
    conn().execute("UPDATE applies SET log=? WHERE id=?", (json.dumps(log), apply_id))


def get_apply(apply_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM applies WHERE id=?", (apply_id,)).fetchone()
    return _row(r, ("log",))


def list_applies(series_id: int) -> list[dict]:
    rows = conn().execute(
        "SELECT id, series_id, plan_id, created, undone_at, json_array_length(log) AS ops "
        "FROM applies WHERE series_id=? ORDER BY id DESC", (series_id,)).fetchall()
    return [dict(r) for r in rows]


def mark_undone(apply_id: int) -> None:
    conn().execute("UPDATE applies SET undone_at=? WHERE id=?", (now(), apply_id))


# ---- jobs ---------------------------------------------------------------

def add_job(kind: str, series_id: int | None, params: dict) -> int:
    return conn().execute(
        "INSERT INTO jobs (series_id, kind, status, created, params) VALUES (?,?,?,?,?)",
        (series_id, kind, "queued", now(), json.dumps(params))).lastrowid


def update_job(job_id: int, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("params", "result"):
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(job_id)
    conn().execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)


def append_job_log(job_id: int, line: str) -> None:
    conn().execute("UPDATE jobs SET log = log || ? WHERE id=?", (line + "\n", job_id))


def get_job(job_id: int, tail: int | None = None) -> dict | None:
    r = conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    job = _row(r, ("params", "result"))
    if job and tail:
        lines = job["log"].splitlines()
        job["log_lines"] = len(lines)
        job["log"] = "\n".join(lines[-tail:])
    return job


def list_jobs(series_id: int | None = None, limit: int = 30) -> list[dict]:
    q = ("SELECT id, series_id, kind, status, created, started, finished, progress, message "
         "FROM jobs")
    args: tuple = ()
    if series_id is not None:
        q += " WHERE series_id=?"
        args = (series_id,)
    q += " ORDER BY id DESC LIMIT ?"
    rows = conn().execute(q, args + (limit,)).fetchall()
    return [dict(r) for r in rows]


def active_job(series_id: int) -> dict | None:
    r = conn().execute(
        "SELECT id, kind, status FROM jobs WHERE series_id=? AND status IN ('queued','running') "
        "ORDER BY id LIMIT 1", (series_id,)).fetchone()
    return dict(r) if r else None
