"""Deleting backed-up files: only what was chosen, only inside the backup folder."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_backup"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import config, db, main  # noqa: E402

root = W / "media" / "Show"
bk = root / "_episodeid_backup"
for rel, size in (("duplicates/Season 1/a.mkv", 1000), ("duplicates/b.mkv", 500),
                  ("split_originals/c.mkv", 3000), ("metadata/a.nfo", 10), ("loose.txt", 5)):
    (bk / rel).parent.mkdir(parents=True, exist_ok=True)
    (bk / rel).write_bytes(b"x" * size)
keep = root / "Season 1" / "Show - S01E01.mkv"
keep.parent.mkdir(parents=True)
keep.write_bytes(b"episode")
outside = W / "precious.mkv"
outside.write_bytes(b"do not touch")
(bk / "duplicates" / "link.mkv").symlink_to(outside)          # must remove the link only
(bk / "duplicates" / "linkdir").symlink_to(root / "Season 1")  # never followed

db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
c = TestClient(main.app)

s = c.get(f"/api/series/{sid}/backup").json()
print({b["name"]: (b["files"], b["bytes"]) for b in s["buckets"]}, s["files"])
assert {b["name"] for b in s["buckets"]} == {"duplicates", "split_originals", "metadata", "(top level)"}

assert c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["duplicates"]}).status_code == 400  # no confirm
r = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["duplicates", "metadata"], "confirm": "DELETE"}).json()
print("deleted:", r)
assert r["deleted"] == 5 and not r["errors"], r            # 2 files + 2 links + 1 nfo
assert outside.read_bytes() == b"do not touch" and keep.exists() and (root / "Season 1").is_dir()
assert not (bk / "duplicates").exists() and not (bk / "metadata").exists()
assert (bk / "split_originals" / "c.mkv").exists() and (bk / "loose.txt").exists()

# Refused while a job runs for the series
j = db.add_job("scan", sid, {})
db.update_job(j, status="running")
assert c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["split_originals"], "confirm": "DELETE"}).status_code == 409
db.update_job(j, status="done")

# A backup-folder setting that isn't a plain subfolder is refused outright
for bad in ("", ".", "..", "../x"):
    config.save_settings({"backup_folder": bad})
    code = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["Season 1"], "confirm": "DELETE"}).status_code
    assert code == 400, (bad, code)
assert keep.exists()
config.save_settings({"backup_folder": "_episodeid_backup"})
r = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["split_originals", "(top level)"], "confirm": "DELETE"}).json()
assert r["deleted"] == 2 and not bk.exists() and keep.exists(), r
print("PASS")
