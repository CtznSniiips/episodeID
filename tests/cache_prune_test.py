"""Clear unused cache: keeps entries for files in a series (or its backup folder), drops
ones for replaced/removed files and all preview pictures, never touches anything else,
and refuses when a series folder is missing (an unmounted share)."""
import os
import shutil
import sys
import time
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_cache"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, main, media_text  # noqa: E402
from app.config import CACHE_DIR  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
keep = root / "Season 1" / "Show - S01E01.mkv"
replaced = root / "Season 1" / "Show - S01E02.mkv"
backed = W / "media" / "tv" / "_episodeid_backup" / "Show" / "duplicates" / "old.mkv"
backed.parent.mkdir(parents=True)
for f, body in ((keep, b"a" * 100), (replaced, b"b" * 100), (backed, b"c" * 100)):
    f.write_bytes(body)
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)


def entries(p):
    k = media_text._cache_key(p)
    return [CACHE_DIR / "text" / f"{k}.json", CACHE_DIR / "titlecards" / f"{k}_abc123.json"]


old_entries = entries(replaced)
time.sleep(0.01)
replaced.write_bytes(b"new download" * 20)  # replaced: new size + mtime
files = entries(keep) + entries(backed) + old_entries + entries(replaced) + \
    [CACHE_DIR / "frames" / "f1.jpg", CACHE_DIR / "text" / "gone.json"]
other = [CACHE_DIR / "whisper" / "model.bin", CACHE_DIR / "refs" / "1" / "S01E01.json"]
for f in files + other:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")

c = TestClient(main.app)
before = c.get("/api/cache").json()
assert before["kinds"][0]["files"] == 5, before
r = c.post("/api/cache/prune").json()
print(" ", {k: r[k] for k in ("deleted", "bytes", "videos")})
assert r["deleted"] == 4 and r["videos"] == 3, r  # 2 old entries of the replaced file, 1 orphan, 1 frame
for f in entries(keep) + entries(backed) + entries(replaced) + other:
    assert f.exists(), f
for f in old_entries + [CACHE_DIR / "frames" / "f1.jpg", CACHE_DIR / "text" / "gone.json"]:
    assert not f.exists(), f

# An unmounted share: refuse, delete nothing.
shutil.move(str(root), str(W / "away"))
r = c.post("/api/cache/prune")
assert r.status_code == 400 and "mounted" in r.json()["detail"], r.text
assert all(f.exists() for f in entries(W / "away" / "Season 1" / "Show - S01E01.mkv"))
shutil.move(str(W / "away"), str(root))

j = db.add_job("scan", sid, {})
db.update_job(j, status="running")
assert c.post("/api/cache/prune").status_code == 409
print("PASS")
