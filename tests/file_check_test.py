"""Daily file check: added / replaced / removed files flag the series' card and queue a
reference fetch (once per set of changes) — never a scan; nothing changed, a
never-scanned series or a missing (unmounted) folder do nothing; scans, applies and
undos record the files as they are and clear the flag."""
import os
import shutil
import sys
import time
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_fcheck"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import autocheck, config, db, jobs, main  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
for n in (1, 2, 3):
    (root / "Season 1" / f"Show - S01E0{n}.mkv").write_bytes(b"x" * (100 + n))
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
submitted = []
jobs.submit = lambda kind, s, params=None: submitted.append((kind, s, params)) or len(submitted)

assert autocheck.check(db.get_series(sid)) is None and not submitted, "never scanned: left alone"

# A scan made before this feature: the first check compares with it (names + sizes).
db.add_scan(sid, {"counts": {}, "files": [{"rel": f"Season 1/Show - S01E0{n}.mkv", "size": 100 + n} for n in (1, 2, 3)]})
d = autocheck.check(db.get_series(sid))
assert d == {"added": [], "removed": [], "changed": []} and not submitted, d

time.sleep(0.01)
(root / "Season 1" / "Show - S01E04.mkv").write_bytes(b"new")
(root / "Season 1" / "Show - S01E02.mkv").write_bytes(b"replacement download")
(root / "Season 1" / "Show - S01E03.mkv").unlink()
d = autocheck.check(db.get_series(sid))
print(" ", d)
assert d["added"] == ["Season 1/Show - S01E04.mkv"] and d["changed"] == ["Season 1/Show - S01E02.mkv"]
assert d["removed"] == ["Season 1/Show - S01E03.mkv"]
assert submitted == [("fetch_refs", sid, {"auto": True})], submitted
st = autocheck.load_state(sid)
assert st["pending"]["added"] == 1 and st["pending"]["fetched"] and st["checked_at"]
submitted.clear()
autocheck.check(db.get_series(sid))
assert not submitted and autocheck.load_state(sid)["pending"]["since"] == st["pending"]["since"], "same changes: no refetch"

# A scan / apply / undo records the files: no change next time.
submitted.clear()
autocheck.record(db.get_series(sid))
assert "pending" not in autocheck.load_state(sid), "a scan clears the flag"
assert not any(autocheck.check(db.get_series(sid)).values()) and not submitted

# Not due yet → not checked; due → checked. 0 hours = off.
(root / "Season 1" / "Show - S01E05.mkv").write_bytes(b"five")
assert autocheck.run_due() == 0 and not submitted
st = autocheck.load_state(sid); st["checked_at"] = "2020-01-01T00:00:00+00:00"; autocheck._save_state(sid, st)
config.save_settings({"auto_check_hours": 0})
assert autocheck.run_due() == 0 and not submitted
config.save_settings({"auto_check_hours": 24})
assert autocheck.run_due() == 1 and [k for k, *_ in submitted] == ["fetch_refs"]

# A job already running for the series: noted, not queued twice.
submitted.clear()
(root / "Season 1" / "Show - S01E06.mkv").write_bytes(b"six")
j = db.add_job("scan", sid, {}); db.update_job(j, status="running")
assert autocheck.check(db.get_series(sid))["added"] and not submitted
db.update_job(j, status="done")
autocheck.check(db.get_series(sid))
assert [k for k, *_ in submitted] == ["fetch_refs"], "queued once the job has finished"

# Unmounted share: no "everything was removed".
submitted.clear()
shutil.move(str(root), str(W / "away"))
assert autocheck.check(db.get_series(sid)) is None and not submitted
shutil.move(str(W / "away"), str(root))

c = TestClient(main.app)
s = c.get("/api/series").json()[0]
fc = s["file_check"]
assert fc["checked_at"] and fc["pending"]["added"] == 2 and "files" not in fc["pending"], fc  # E05 + E06: not scanned
(root / "Season 1" / "Show - S01E07.mkv").write_bytes(b"seven")
r = c.post("/api/file-check").json()
assert r["changed"] and r["changed"][0]["added"] == 3, r  # still unscanned E05, E06 + new E07
print("PASS")
