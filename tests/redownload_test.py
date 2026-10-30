"""Re-download: releases are chosen by the episode titles in their names, whatever
numbers they carry; grabbing tells Sonarr which episodes they hold, then moves the
damaged file to the backup folder and has Sonarr rescan."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_redl"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import config, db, integrity, main, redownload, sonarr  # noqa: E402
from app.jobs import JobContext  # noqa: E402

T = {(2, 1): "The Remote", (2, 2): "The Colossus", (2, 3): "The Knights", (2, 4): "The Fridge",
     (2, 5): "The Flower", (2, 6): "The Banana", (2, 7): "The Phone", (2, 8): "The Job",
     (2, 9): "Halloween", (2, 10): "The Treasure", (4, 7): "The Pest", (4, 8): "The Sale"}
EPS = [{"season": s, "episode": e, "title": t} for (s, e), t in T.items()]
SN = "The Amazing World of Show"
tir = lambda n: redownload.titles_in_release(n, EPS, SN)  # noqa: E731

# Titles found whatever the numbers say; series name and quality words ignored.
assert tir("The.Amazing.World.of.Show.S02E05.The.Fridge.1080p.WEB-DL.DDP2.0.H.264-GRP") == ["S02E04"]
assert tir("The Amazing World of Show S02E04E05 The Fridge + The Flower 1080p WEBRip") == ["S02E04", "S02E05"]
assert tir("Show.S02E09-E10.Halloween.and.The.Treasure.720p.HDTV") == ["S02E09", "S02E10"]
assert tir("The.Amazing.World.of.Show.S02E04.1080p.WEB-DL") == []          # numbers only
assert tir("The.Amazing.World.of.Show.S02.1080p.WEB-DL") == []             # season pack
assert tir("Show.S04E07.The.Pest.Control.Unit.1080p") == ["S04E07"]
print("PASS (titles in release names)")


def rel_(title, mapped, guid, q="WEBDL-1080p", full=False, rej=(), size=500_000_000, proto="usenet"):
    return {"guid": guid, "indexerId": 3, "indexer": "NZBgeek", "title": title, "size": size, "protocol": proto,
            "quality": {"quality": {"name": q}}, "languages": [{"id": 1, "name": "English"}], "fullSeason": full,
            "mappedEpisodeInfo": [{"seasonNumber": s, "episodeNumber": e} for s, e in mapped],
            "rejections": list(rej), "age": 100}


RELEASES = {2: [
    rel_("Show.S02E05.The.Fridge.1080p.WEB-DL", [(2, 5)], "g-fridge-misnumbered"),                 # right title, wrong number
    rel_("Show.S02E04.1080p.WEB-DL", [(2, 4)], "g-numbers"),                                       # numbers only
    rel_("Show.S02E04E05.The.Fridge.+.The.Flower.1080p.WEB-DL", [(2, 4), (2, 5)], "g-pair",
         rej=["Existing file on disk meets quality cutoff"]),
    rel_("Show.S02E04E05.The.Fridge.+.The.Flower.720p.HDTV", [(2, 4), (2, 5)], "g-pair-720", q="HDTV-720p"),
    rel_("Show.S02E05E06.The.Flower.+.The.Banana.1080p.WEB-DL", [(2, 5), (2, 6)], "g-flower-banana"),
    rel_("Show.S02.1080p.WEB-DL", [], "g-pack", full=True),
    rel_("Show.S02E01.The.Remote.1080p.WEB-DL", [(2, 1)], "g-remote", rej=["Release is blocklisted"]),
    rel_("Show.S02E02.The.Remote.1080p.WEB-DL", [(2, 2)], "g-remote2"),
]}

want = ["S02E04", "S02E05"]
c = redownload.candidates(want, RELEASES[2], EPS, SN, "webdl-1080p")
order = [x["guid"] for x in c]
print("  ", order)
assert order[0] == "g-pair" and order[1] == "g-pair-720", order       # both titles first, then quality
assert order.index("g-fridge-misnumbered") < order.index("g-numbers") < order.index("g-pack")
assert redownload.preselect(want, c) == ["g-pair"]
one = redownload.candidates(["S02E01"], RELEASES[2], EPS, SN, "webdl-1080p")
assert [x["guid"] for x in one][:2] == ["g-remote2", "g-remote"], "a blocklisted one comes after"
assert redownload.preselect(["S02E01"], one) == ["g-remote2"]
print("PASS (ranking)")

# --- end to end with a fake Sonarr
root = W / "media" / "tv" / "Show"
(root / "Season 2").mkdir(parents=True)
bad = "Season 2/Show - S02E04E05 - The Fridge + The Flower WEBDL-1080p.mkv"
(root / bad).write_bytes(b"x" * 1000)
db.init()
sid = db.add_series(77, "Show", "2011", str(root), None)
db.update_series(sid, episodes=EPS, episodes_updated=db.now(), options={"title_cards": "off"})
db.add_scan(sid, {"files": [{"rel": bad, "expected": want, "segments": [
    {"code": "S02E04", "confidence": "high"}, {"code": "S02E05", "confidence": "high"}]}], "counts": {}})
config.save_settings({"sonarr_url": "http://sonarr:8989", "sonarr_api_key": "k"})
calls = []
sonarr.find_series = lambda tvdb: {"id": 9, "tvdbId": tvdb, "title": "Show"}
sonarr.season_releases = lambda series_id, season: calls.append(("search", season)) or RELEASES.get(season, [])
sonarr.episodes = lambda series_id: [{"id": 1000 + s * 100 + e, "seasonNumber": s, "episodeNumber": e} for (s, e) in T]
sent = []
sonarr._send = lambda method, path, body, timeout=60: sent.append((method, path, body)) or {}
sonarr.history_since = lambda date: []
real_settings = redownload.sonarr_redownload_settings
redownload.sonarr_redownload_settings = lambda: None
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
st = redownload.summary(db.get_series(sid))
f = st["files"][bad]
assert calls == [("search", 2)] and f["preselect"] == ["g-pair"] and f["titled"] >= 3, f
db.conn().execute("UPDATE jobs SET status='done'")

c = TestClient(main.app)
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-pair"}]}).json()
print("  ", r)
assert [g["guid"] for g in r["grabbed"]] == ["g-pair"] and r["moved"] == [bad] and not r["failed"]
grab = next(b for m, p, b in sent if p == "release")
assert grab["shouldOverride"] and grab["seriesId"] == 9 and grab["episodeIds"] == [1204, 1205], grab
assert grab["guid"] == "g-pair" and grab["indexerId"] == 3 and grab["quality"]["quality"]["name"] == "WEBDL-1080p"
assert ("POST", "command", {"name": "RescanSeries", "seriesId": 9}) in sent
assert not (root / bad).exists()
assert (W / "media" / "tv" / "_episodeid_backup" / "Show" / "damaged" / bad).is_file()
st = redownload.summary(db.get_series(sid))
assert bad in st["requested"] and bad not in st["files"]
print("PASS (grab with episode override, damaged file moved, Sonarr rescans)")

# Sonarr refusing the override: a plain grab, and the result says so.
(root / bad).write_bytes(b"y" * 1000)
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
db.conn().execute("UPDATE jobs SET status='done'")
sent.clear()


def picky(method, path, body, timeout=60):
    if path == "release" and body.get("shouldOverride"):
        raise sonarr.SonarrError("400 Bad Request")
    sent.append((method, path, body))
    return {}


sonarr._send = picky
# Numbered S02E05 but holding S02E04 (The Fridge): grabbed plainly, Sonarr would import it
# over S02E05's file — so it isn't grabbed, and the damaged file stays.
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-fridge-misnumbered"}]}).json()
assert not r["grabbed"] and not r["moved"] and "another episode" in r["failed"][0]["error"], r
assert not [b for m, p, b in sent if p == "release"] and (root / bad).exists()
# Numbered as the episodes it holds: a plain grab is safe.
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-pair"}]}).json()
assert r["grabbed"][0]["override"] is False and [b for m, p, b in sent if p == "release"] == [
    {"guid": "g-pair", "indexerId": 3}], sent
print("PASS (without the override, only releases numbered as the right episodes are grabbed)")

# A failed grab moves nothing.
(root / bad).write_bytes(b"z" * 1000)
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
db.conn().execute("UPDATE jobs SET status='done'")
sonarr._send = lambda *a, **k: (_ for _ in ()).throw(sonarr.SonarrError("Sonarr is down"))
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-pair"}]}).json()
assert not r["grabbed"] and not r["moved"] and r["failed"] and (root / bad).exists(), r
print("PASS (nothing moved when the grab fails)")

# New downloads are checked for damage after their import scan.
seen = []
integrity.check_paths = lambda series, rels, ctx: seen.append(rels) or {}
from app import pipeline  # noqa: E402
pipeline.get_dialogue = lambda v, ctx=None, allow_whisper=True: {"source": "embedded", "duration": 600.0, "cues": []}
pipeline.cached_dialogue = lambda v: None
pipeline.job_scan(JobContext(db.add_job("scan", sid, {}), sid), {"only": [bad], "imported": True})
assert seen == [[bad]], seen
print("PASS (imports are checked)")

# --- following the grabs: failed → put back; imported → stays replaced; never seen → put back
from datetime import datetime, timedelta, timezone  # noqa: E402

HIST = []
sonarr.history_since = lambda date: list(HIST)
HAS = set()
sonarr.episodes = lambda series_id: [{"id": 1000 + s * 100 + e, "seasonNumber": s, "episodeNumber": e,
                                      "hasFile": (1000 + s * 100 + e) in HAS} for (s, e) in T]
sent.clear()
sonarr._send = lambda method, path, body, timeout=60: sent.append((method, path, body)) or {}


def fresh_grab(guid="g-pair"):
    (root / bad).write_bytes(b"d" * 1000)
    redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
    db.conn().execute("UPDATE jobs SET status='done'")
    r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": guid}],
                                                     "auto_retry": False}).json()
    assert r["moved"] == [bad] and not (root / bad).exists(), r
    return "Show.S02E04E05.The.Fridge.+.The.Flower.1080p.WEB-DL"


st0 = redownload.load_state(sid)
st0["requested"] = {}  # start the follow-up tests clean
redownload._save_state(sid, st0)
title = fresh_grab()
HIST[:] = [{"eventType": "grabbed", "episodeId": 1204, "sourceTitle": title, "downloadId": "SAB_1"},
           {"eventType": "downloadFailed", "episodeId": 1204, "sourceTitle": title, "downloadId": "SAB_1",
            "data": {"message": "Aborted, cannot be completed"}}]
redownload.follow_up(db.get_series(sid))
r = redownload.load_state(sid)["requested"][bad]
assert r["status"] == "failed" and r["restored"] and (root / bad).exists(), r
assert "Aborted" in r["grabs"][0]["reason"]
assert ("POST", "command", {"name": "RescanSeries", "seriesId": 9}) in sent
print("PASS (failed download: damaged file put back)")

title = fresh_grab()
HIST[:] = [{"eventType": "grabbed", "episodeId": 1205, "sourceTitle": title, "downloadId": "SAB_2"},
           {"eventType": "downloadFolderImported", "episodeId": 1205, "sourceTitle": title, "downloadId": "SAB_2"}]
s = c.get(f"/api/series/{sid}/redownload").json()  # opening the dialog follows up too
assert s["requested"][bad]["status"] == "imported" and not (root / bad).exists()
assert (W / "media" / "tv" / "_episodeid_backup" / "Show" / "damaged" / bad).is_file(), "damaged copy kept"
print("PASS (imported: replaced, damaged copy kept in the backup folder)")

fresh_grab()
HIST[:] = []
st = redownload.load_state(sid)
old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
st["requested"][bad]["grabs"][-1]["at"] = old
redownload._save_state(sid, st)
HAS.add(1204)  # …but Sonarr has a file for it again: leave things alone
redownload.follow_up(db.get_series(sid))
r = redownload.load_state(sid)["requested"][bad]
assert r["status"] == "failed" and not r["restored"] and "Sonarr has a file" in r["restore_note"], r
assert not (root / bad).exists()
HAS.clear()
assert c.post(f"/api/series/{sid}/redownload/put-back", json={"rel": bad}).json()["ok"] and (root / bad).exists()
print("PASS (never grabbed: put back unless Sonarr has a file again; Put back by hand)")

# The Sonarr setting that would search again by number.
redownload.sonarr_redownload_settings = real_settings
sonarr._get = lambda path, timeout=60, **p: {"autoRedownloadFailed": True,
                                             "autoRedownloadFailedFromInteractiveSearch": False}
assert redownload.sonarr_redownload_settings() == {"failed": True, "interactive": False}
assert c.get(f"/api/series/{sid}/redownload").json()["sonarr_redownload"]["failed"] is True
assert c.get(f"/api/series/{sid}").json()["redownloads"] == 0, "put back by hand: nothing in progress"
print("PASS (Sonarr's re-download setting read)")

# --- automatic retry: next release with the title, never numbers-only or ones holding other episodes
sonarr._get = lambda path, timeout=60, **p: {}
sent.clear()
sonarr._send = lambda method, path, body, timeout=60: sent.append((method, path, body)) or {}
HIST[:] = []
(root / bad).write_bytes(b"r" * 1000)
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
db.conn().execute("UPDATE jobs SET status='done'")
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-pair"}]}).json()
assert r["moved"] == [bad]
grabs_sent = lambda: [b["guid"] for m, p, b in sent if p == "release"]  # noqa: E731


def fail_last():
    g = redownload.load_state(sid)["requested"][bad]["grabs"][-1]
    n = len(HIST)
    HIST.extend([{"eventType": "grabbed", "episodeId": 1204, "sourceTitle": g["title"], "downloadId": f"D{n}"},
                 {"eventType": "downloadFailed", "episodeId": 1204, "sourceTitle": g["title"], "downloadId": f"D{n}",
                  "data": {"message": "Aborted"}}])
    redownload.follow_up(db.get_series(sid))
    return redownload.load_state(sid)["requested"][bad]


r = fail_last()
assert r["status"] == "waiting" and grabs_sent() == ["g-pair", "g-pair-720"], grabs_sent()   # next: both titles
assert not (root / bad).exists(), "still in the backup folder while retrying"
r = fail_last()
# Left: g-fridge-misnumbered (only one title), g-flower-banana (holds S02E06 too), g-numbers, g-pack — none qualify.
assert r["status"] == "failed" and r["restored"] and (root / bad).exists(), r
assert grabs_sent() == ["g-pair", "g-pair-720"], grabs_sent()
print("PASS (retries the next titled release only; puts the file back when none are left)")

# Retry off: a failure puts the file back straight away.
(root / bad).unlink()
(root / bad).write_bytes(b"s" * 1000)
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"files": [bad]})
db.conn().execute("UPDATE jobs SET status='done'")
sent.clear()
c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": bad, "guid": "g-pair"}], "auto_retry": False})
r = fail_last()
assert r["status"] == "failed" and r["restored"] and grabs_sent() == ["g-pair"]
print("PASS (no retry when turned off)")

# --- missing episodes: found by title, a release holding two missing ones grabbed once, nothing moved
sent.clear()
HIST[:] = []
st0 = redownload.load_state(sid)
st0["requested"] = {}
redownload._save_state(sid, st0)
db.update_series(sid, episodes=[{**e, "aired": "2012-01-01"} for e in EPS])
db.add_scan(sid, {"files": [{"rel": bad, "expected": want, "segments": []},
                            {"rel": "Season 2/Show - S02E01.mkv", "expected": ["S02E01"], "segments": []},
                            {"rel": "Season 2/Show - S02E02.mkv", "expected": ["S02E02"], "segments": []},
                            {"rel": "Season 2/Show - S02E03.mkv", "expected": ["S02E03"], "segments": []},
                            {"rel": "Season 2/Show - S02E06.mkv", "expected": ["S02E06"], "segments": []},
                            {"rel": "Season 2/Show - S02E09E10.mkv", "expected": ["S02E09", "S02E10"], "segments": []}],
                  "counts": {}})
RELEASES[2] += [rel_("Show.S02E07E08.The.Phone.+.The.Job.1080p.WEB-DL", [(2, 7), (2, 8)], "g-phone-job"),
                rel_("Show.S02E07.The.Phone.720p.HDTV", [(2, 7)], "g-phone-720", q="HDTV-720p")]
miss = [m["code"] for m in redownload.missing_episodes(db.get_series(sid))]
assert miss == ["S02E07", "S02E08", "S04E07", "S04E08"], miss
redownload.job_find_releases(JobContext(db.add_job("find_releases", sid, {}), sid), {"missing": ["S02E07", "S02E08"]})
db.conn().execute("UPDATE jobs SET status='done'")
s = c.get(f"/api/series/{sid}/redownload").json()
f7 = s["files"]["missing:S02E07"]
assert f7["missing"] and f7["candidates"][0]["guid"] == "g-phone-job" and f7["candidates"][0]["fills"] == ["S02E08"]
assert f7["candidates"][0]["extra"] == [] and f7["preselect"] == ["g-phone-job"]
assert s["files"]["missing:S02E08"]["preselect"] == ["g-phone-job"]
assert c.get(f"/api/series/{sid}").json()["missing_count"] == 4
r = c.post(f"/api/series/{sid}/redownload", json={"picks": [{"rel": "missing:S02E07", "guid": "g-phone-job"},
                                                           {"rel": "missing:S02E08", "guid": "g-phone-job"}]}).json()
assert len(r["grabbed"]) == 2 and not r["moved"] and not r["failed"], r
grabs = [b for m, p, b in sent if p == "release"]
assert len(grabs) == 1 and grabs[0]["episodeIds"] == [1207, 1208], grabs
s = c.get(f"/api/series/{sid}/redownload").json()
assert s["requested"]["missing:S02E07"]["status"] == "waiting" and "missing:S02E07" not in s["files"]
HIST[:] = [{"eventType": "grabbed", "episodeId": 1207, "sourceTitle": "Show.S02E07E08.The.Phone.+.The.Job.1080p.WEB-DL", "downloadId": "M1"},
           {"eventType": "downloadFolderImported", "episodeId": 1207, "sourceTitle": "x", "downloadId": "M1"}]
redownload.follow_up(db.get_series(sid))
req = redownload.load_state(sid)["requested"]
assert req["missing:S02E07"]["status"] == "imported" and req["missing:S02E08"]["status"] == "imported"
print("PASS (missing episodes: by title, a two-episode release fills both, grabbed once)")
