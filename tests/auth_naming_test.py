"""Optional login, and copying naming settings from Sonarr."""
import base64
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_auth"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
(W / "media").mkdir(parents=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import config, db, main, planner  # noqa: E402

db.init()
c = TestClient(main.app)
assert c.get("/api/settings").status_code == 200, "open by default"
config.save_settings({"auth_username": "steve", "auth_password": "pä55:word"})
r = c.get("/api/settings")
assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], r
assert c.get("/").status_code == 401, "the page itself too"
assert c.get("/api/settings", auth=("steve", "wrong")).status_code == 401
assert c.get("/api/settings", headers={"Authorization": "Basic !!!"}).status_code == 401
ok = c.get("/api/settings", auth=("steve", "pä55:word"))
assert ok.status_code == 200 and ok.json()["settings"]["auth_password"] == "********", ok.text
assert c.get("/api/status").status_code == 401, "status needs the login from outside"
local = TestClient(main.app, client=("127.0.0.1", 50000))
assert local.get("/api/status").status_code == 200, "the container healthcheck still works"
assert local.get("/api/settings").status_code == 401, "only /api/status is open locally"
# Saving the masked password back doesn't change it; clearing it turns the login off.
c.put("/api/settings", json={"auth_password": "********"}, auth=("steve", "pä55:word"))
assert c.get("/api/settings", auth=("steve", "pä55:word")).status_code == 200
c.put("/api/settings", json={"auth_password": ""}, auth=("steve", "pä55:word"))
assert c.get("/api/settings").status_code == 200
print("PASS (login)")

# ---- Copy naming from Sonarr (v4 sends enums as numbers, older ones as names)
sid = db.add_series(81189, "Bluey", "2018", str(W / "media" / "Bluey"), None)
db.add_series(999, "Naruto", "2002", str(W / "media" / "Naruto"), None)
config.save_settings({"sonarr_url": "http://sonarr:8989", "sonarr_api_key": "k"})
NAMING = {"renameEpisodes": True, "replaceIllegalCharacters": False, "colonReplacementFormat": 2,
          "customColonReplacementFormat": "", "multiEpisodeStyle": 1,
          "standardEpisodeFormat": "{Series TitleYear} - S{season:00}E{episode:00} - {Episode Title} [{Quality Full}]{-Release Group}",
          "seasonFolderFormat": "Season {season}", "specialsFolderFormat": "Specials"}
SERIES = [{"tvdbId": 81189, "title": "Bluey", "seriesType": "standard"},
          {"tvdbId": 999, "title": "Naruto", "seriesType": "anime"}]
calls = []


def fake_get(url, headers=None, params=None, timeout=None):
    calls.append((url, headers))
    body = NAMING if url.endswith("/config/naming") else \
        [x for x in SERIES if not params or x["tvdbId"] == params["tvdbId"]]
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


real_get = httpx.get
httpx.get = fake_get
try:
    r = c.post("/api/sonarr/import-naming").json()
    from app import executor
    note = executor.sonarr_series_type_note(db.get_series(2))
    assert executor.sonarr_series_type_note(db.get_series(sid)) is None
finally:
    httpx.get = real_get
print(" ", r)
s = config.get_settings()
assert s["colon_replacement"] == "space_dash" and s["multi_episode_style"] == "duplicate"
assert s["replace_illegal_characters"] is False and s["season_folder_format"] == "Season {season}"
assert s["naming_format"] == NAMING["standardEpisodeFormat"] and "naming_format" in r["changed"]
assert any("{-Release Group}" in n for n in r["notes"]) and any("Naruto" in n and "anime" in n for n in r["notes"])
assert all(h == {"X-Api-Key": "k"} for _u, h in calls)
assert note and "absolute" in note, note
eps = [{"season": 1, "episode": 1, "title": "Magic Xylophone"}, {"season": 1, "episode": 2, "title": "Hospital"}]
got = planner.episode_filename("Bluey", eps, ".mkv", "old WEBDL-1080p.mkv", s, 2018)
print(" ", got)
assert got == "Bluey (2018) - S01E01 - S01E02 - Magic Xylophone + Hospital [WEBDL-1080p].mkv", got
assert planner.season_dir(Path("/x"), 3, s, {"flat": False, "season_dirs": {}}) == Path("/x/Season 3")

# Sonarr v3 sends names; an env-locked setting is left alone.
NAMING.update(colonReplacementFormat="spaceDashSpace", multiEpisodeStyle="prefixedRange")
os.environ["MULTI_EPISODE_STYLE"] = "repeat"
httpx.get = fake_get
try:
    r = c.post("/api/sonarr/import-naming").json()
finally:
    httpx.get = real_get
    del os.environ["MULTI_EPISODE_STYLE"]
s = config.get_settings()
assert s["colon_replacement"] == "space_dash_space" and s["multi_episode_style"] == "duplicate", s
assert any("environment" in n for n in r["notes"]), r
print("PASS (Sonarr naming)")
