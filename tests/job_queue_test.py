"""A big Sonarr import doesn't pile up hundreds of jobs: files imported while a scan
is waiting join that scan. Cancelling a waiting job takes effect at once, "Cancel
waiting" clears a series' queue, and the job list shows the running job first."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_queue"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, jobs, main  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
for n in range(1, 43):
    (root / f"Season 1/Show - S01E{n:02d}.mkv").write_bytes(b"x")
db.init()
sid = db.add_series(1, "Show", "2020", str(root), None)
other = db.add_series(2, "Other", "2020", str(W / "media" / "tv" / "Other"), None)
db.add_scan(sid, {"files": []})  # imports are only scanned for series scanned before
jobs._q.put = lambda job_id: None  # no worker in this test: jobs stay queued
c = TestClient(main.app)


def hook(n, tvdb=1, path="/tv/Show"):
    rel = f"Season 1/Show - S01E{n:02d}.mkv"
    return c.post("/api/sonarr/webhook", json={"eventType": "Download", "series": {"tvdbId": tvdb, "path": path},
                                               "episodeFile": {"relativePath": rel}}).json()


# 1. 40 imports → one waiting scan with all 40 files.
first = hook(1)["queued"]
for n in range(2, 41):
    r = hook(n)
    assert r["queued"] == first and r.get("batched"), r
hook(5)  # the same file twice is listed once
waiting = [j for j in db.list_jobs(sid, 100) if j["status"] == "queued"]
assert len(waiting) == 1, waiting
only = db.get_job(first)["params"]["only"]
assert len(only) == 40 and only[0].endswith("E01.mkv"), only
print("PASS (imports batched into one scan)")

# 2. Once that scan has started, the next import queues a new one.
db.update_job(first, status="running", started=db.now())
second = hook(41)["queued"]
assert second != first and db.get_job(second)["params"]["only"] == ["Season 1/Show - S01E41.mkv"]
# A user-started scan waiting in the queue isn't an import scan: don't add to it.
db.update_job(second, status="done")
mine = jobs.submit("scan", sid, {})
third = hook(42)["queued"]
assert third != mine and db.get_job(mine)["params"] == {}
print("PASS (new batch once the scan has started)")

# 3. The running job is listed first even behind a long queue.
for _ in range(40):
    jobs.submit("replan", sid, {})
lst = c.get(f"/api/jobs?series_id={sid}").json()
assert lst[0]["id"] == first and lst[0]["status"] == "running", lst[0]
assert c.get(f"/api/series/{sid}/queue").json()["waiting"] == 42
print("PASS (running job listed first)")

# 4. Cancelling a waiting job is immediate; a running one is flagged for its next check.
assert c.post(f"/api/jobs/{mine}/cancel").json()["ok"]
assert db.get_job(mine)["status"] == "cancelled"
c.post(f"/api/jobs/{first}/cancel")
assert db.get_job(first)["status"] == "running" and first in jobs._cancel
jobs._cancel.discard(first)
print("PASS (cancel)")

# 5. Cancel waiting clears this series' queue only.
o = jobs.submit("scan", other, {})
assert c.post(f"/api/series/{sid}/jobs/cancel-waiting").json()["cancelled"] == 41
assert c.get(f"/api/series/{sid}/queue").json()["waiting"] == 0
assert db.get_job(o)["status"] == "queued" and db.get_job(first)["status"] == "running"

# 6. The worker skips a job cancelled while it waited.
ran = []
jobs._handlers["scan"] = lambda ctx, p: ran.append(ctx.job_id)
jobs._run(mine)
jobs._run(o)
assert ran == [o] and db.get_job(mine)["status"] == "cancelled", ran
print("PASS (cancel waiting)")
