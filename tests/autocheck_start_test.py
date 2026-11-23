"""The scheduled file check actually runs: starting the app starts its loop, and a
series whose last check is older than AUTO_CHECK_HOURS is checked on the next tick."""
import os
import shutil
import sys
import threading
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_autostart"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import autocheck, db, main  # noqa: E402

with TestClient(main.app):
    names = {t.name for t in threading.enumerate()}
assert "autocheck" in names, names
print("PASS (startup starts the file check)")

root = W / "media" / "tv" / "Show" / "Season 1"
root.mkdir(parents=True)
(root / "Show - S01E01.mkv").write_bytes(b"x" * 10)
sid = db.add_series(1, "Show", "2020", str(root.parent), None)
db.add_scan(sid, {"files": [{"rel": "Season 1/Show - S01E01.mkv", "size": 10}]})
st = autocheck.load_state(sid)
st["checked_at"] = "2026-11-18T12:00:00+00:00"  # five days ago
autocheck._save_state(sid, st)
assert autocheck.run_due() == 1
assert autocheck.load_state(sid)["checked_at"] > "2026-11-18T12:00:00+00:00"
assert autocheck.run_due() == 0, "just checked: not due again"
print("PASS (an overdue series is checked)")
