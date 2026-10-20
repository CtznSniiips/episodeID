"""The series list reports how many video files sit in each series' backup folder
(the dashboard card shows it); other backed-up files (subtitles, .nfo) don't count."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_backup_badge"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, main  # noqa: E402

tv = W / "media" / "tv"
for name in ("Show", "Clean"):
    (tv / name / "Season 1").mkdir(parents=True)
bk = tv / "_episodeid_backup" / "Show"
(bk / "duplicates" / "Season 1").mkdir(parents=True)
(bk / "duplicates" / "Season 1" / "Show - S01E01.mkv").write_bytes(b"x" * 1000)
(bk / "duplicates" / "Season 1" / "Show - S01E01.en.srt").write_bytes(b"x" * 50)
(bk / "split_originals").mkdir()
(bk / "split_originals" / "Show - S01E02E03.mp4").write_bytes(b"x" * 3000)
(bk / "metadata").mkdir()
(bk / "metadata" / "Show - S01E04.nfo").write_bytes(b"x")
db.init()
a = db.add_series(1, "Show", "2020", str(tv / "Show"), None)
b = db.add_series(2, "Clean", "2020", str(tv / "Clean"), None)
c = TestClient(main.app)
by = {s["id"]: s for s in c.get("/api/series").json()}
assert by[a]["backup"] == {"videos": 2, "bytes": 4000}, by[a]["backup"]
assert by[b]["backup"] == {"videos": 0, "bytes": 0}, by[b]["backup"]
assert (tv / "_episodeid_backup" / "Show" / "duplicates").is_dir(), "listing moves nothing"
print("PASS (backed-up videos counted)")
