"""Mark OK: a flagged file you've checked counts as fine from then on — it follows a
rename, drops off when the file is replaced, and can be undone."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_accept"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, integrity, main  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
f = root / "Season 1" / "Show - S01E01.mkv"
f.write_bytes(b"x" * 1000)
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
integrity._save_state(sid, {"checked_at": "2026-11-02T00:00:00+00:00", "files": {
    "Season 1/Show - S01E01.mkv": {"key": integrity._key(f), "status": "warn", "summary": "video starts 1.2 s after the audio"}}})
series = db.get_series(sid)
rel = "Season 1/Show - S01E01.mkv"
assert integrity.current(series)[rel]["status"] == "warn"
assert integrity.card_summary(series)["warn"] == 1

c = TestClient(main.app)
assert c.post(f"/api/series/{sid}/integrity/accept", json={"rel": rel}).json()["ok"]
v = integrity.current(series)[rel]
assert v["status"] == "ok" and v["was"] == "warn" and v["accepted"], v
assert integrity.card_summary(series)["warn"] == 0
assert c.get(f"/api/series/{sid}").json()["integrity_files"][rel]["status"] == "ok"
print("PASS (marked OK)")

# follows a rename
new = root / "Season 1" / "Show - S01E01 - Pilot.mkv"
f.rename(new)
assert integrity.current(series)["Season 1/Show - S01E01 - Pilot.mkv"]["status"] == "ok"
print("PASS (follows a rename)")

# undo
assert c.post(f"/api/series/{sid}/integrity/accept", json={"rel": "Season 1/Show - S01E01 - Pilot.mkv", "undo": True}).json()["ok"]
assert integrity.current(series)["Season 1/Show - S01E01 - Pilot.mkv"]["status"] == "warn"
print("PASS (undo)")

# a replaced file isn't covered (and its old result drops out anyway)
c.post(f"/api/series/{sid}/integrity/accept", json={"rel": "Season 1/Show - S01E01 - Pilot.mkv"})
time.sleep(0.01)
new.write_bytes(b"y" * 2000)
assert "Season 1/Show - S01E01 - Pilot.mkv" not in integrity.current(series)
st = integrity.load_state(sid)
st["files"]["Season 1/Show - S01E01 - Pilot.mkv"] = {"key": integrity._key(new), "status": "warn", "summary": "s"}
integrity._save_state(sid, st)
assert integrity.current(series)["Season 1/Show - S01E01 - Pilot.mkv"]["status"] == "warn"
print("PASS (a replaced file is flagged again)")
