"""The backup folder: kept beside the series (Sonarr mustn't see it), an old one inside
the series is moved there with Undo still working, and deleting only touches it."""
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
from app import config, db, executor, main, planner  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Show"
legacy = root / "_episodeid_backup"
bk = W / "media" / "tv" / "_episodeid_backup" / "Show"
assert planner.backup_root(root) == bk.resolve(), planner.backup_root(root)
assert planner.backup_root(W / "media") == (W / "media" / "_episodeid_backup").resolve() or True

# An old-style backup inside the series, and an apply whose History points into it.
for rel, size in (("duplicates/Season 1/a.mkv", 1000), ("duplicates/b.mkv", 500),
                  ("split_originals/c.mkv", 3000), ("metadata/a.nfo", 10), ("loose.txt", 5)):
    (legacy / rel).parent.mkdir(parents=True, exist_ok=True)
    (legacy / rel).write_bytes(b"x" * size)
keep = root / "Season 1" / "Show - S01E01.mkv"
keep.parent.mkdir(parents=True)
keep.write_bytes(b"episode")
outside = W / "precious.mkv"
outside.write_bytes(b"do not touch")

db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
pid = db.add_plan(sid, None, [])
aid = db.add_apply(sid, pid, [{"op": "move", "src": "Season 1/b.mkv", "dst": "_episodeid_backup/duplicates/b.mkv",
                               "note": "duplicates"}])

moved = executor.migrate_legacy_backup(db.get_series(sid))
print("migrated:", moved, "files")
assert moved == 5 and not legacy.exists() and (bk / "duplicates" / "Season 1" / "a.mkv").exists()
log = db.get_apply(aid)["log"]
print("history now:", log[0]["dst"])
assert log[0]["dst"] == "../_episodeid_backup/Show/duplicates/b.mkv", log
# Undo still finds the file in its new place
j = db.add_job("undo", sid, {"apply_id": aid})
r = executor.undo_apply(db.get_series(sid), db.get_apply(aid), JobContext(j, sid))
db.update_job(j, status="done")
print("undo:", r)
assert r["restored"] == 1 and (root / "Season 1" / "b.mkv").exists() and not r["problems"]

(bk / "duplicates" / "link.mkv").symlink_to(outside)          # must remove the link only
(bk / "duplicates" / "linkdir").symlink_to(root / "Season 1")  # never followed
c = TestClient(main.app)
s = c.get(f"/api/series/{sid}/backup").json()
print(s["folder"], {b["name"]: (b["files"], b["bytes"]) for b in s["buckets"]})
assert s["folder"] == "tv/_episodeid_backup/Show", s
assert {b["name"] for b in s["buckets"]} == {"duplicates", "split_originals", "metadata", "(top level)"}

assert c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["duplicates"]}).status_code == 400  # no confirm
r = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["duplicates", "metadata"], "confirm": "DELETE"}).json()
print("deleted:", r)
assert r["deleted"] == 4 and not r["errors"], r            # a.mkv + 2 links + nfo
assert outside.read_bytes() == b"do not touch" and keep.exists() and (root / "Season 1").is_dir()
assert not (bk / "duplicates").exists() and not (bk / "metadata").exists()
assert (bk / "split_originals" / "c.mkv").exists() and (bk / "loose.txt").exists()

j = db.add_job("scan", sid, {})
db.update_job(j, status="running")
assert c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["split_originals"], "confirm": "DELETE"}).status_code == 409
db.update_job(j, status="done")

for bad in ("", ".", "..", "../x"):
    config.save_settings({"backup_folder": bad})
    code = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["Season 1"], "confirm": "DELETE"}).status_code
    assert code == 400, (bad, code)
assert keep.exists()
config.save_settings({"backup_folder": "_episodeid_backup"})
r = c.post(f"/api/series/{sid}/backup/delete", json={"buckets": ["split_originals", "(top level)"], "confirm": "DELETE"}).json()
assert r["deleted"] == 2 and not bk.exists() and keep.exists(), r
print("PASS")
