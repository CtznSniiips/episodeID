"""A subtitle's own file name ("Paw.Patrol.S06E31.Pups.Save.the.Bears.1080p") settles
which episode it is: this episode's TVDB title verifies it, another's rules it out."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_names"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  OPENSUBTITLES_API_KEY="k")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import references as R  # noqa: E402
from app.jobs import JobContext  # noqa: E402,F401

EPS = [{"season": 6, "episode": n, "title": t} for n, t in (
    (30, "Pups Save the Beavers"), (31, "Pups Save the Bears"), (32, "Pups Save a Hoot"),
    (33, "Rain"), (34, "Pups Save a Hoot Again"))]
nc = lambda names, code: R.name_check(names, EPS, code, "Paw Patrol")[:2]  # noqa: E731

assert nc(["Paw.Patrol.S06E31.Pups.Save.the.Bears.1080p.WEB-DL.AAC2.0.H.264"], "S06E31") == ("match", None)
assert nc(["Paw Patrol - S06E31 - Pups Save the Beavers WEBDL-1080p.srt"], "S06E31") == ("other", "S06E30")
assert nc(["PAW.Patrol.S06E31.720p.HDTV.x264-W4F"], "S06E31") == (None, None), "number only: no verdict"
assert nc(["Paw.Patrol.S06E34.Pups.Save.a.Hoot.Again.720p"], "S06E32") == ("other", "S06E34"), "longest title wins"
assert nc(["Paw.Patrol.S06E34.Pups.Save.a.Hoot.Again.720p"], "S06E34")[0] == "match"
assert nc(["paw patrol 6x33 rain.srt"], "S06E33")[0] == "match", "short titles still confirm their own episode"
assert nc(["Paw.Patrol.S06E31.Rain.Check"], "S06E31") == (None, None), "too short to accuse another episode"
assert nc(["Pups Save the Bears.srt"], "S06E31")[0] == "match", "a bare title, no number"
assert nc(["Paw.Patrol.S06E30E31.Pups.Save.the.Beavers.+.Pups.Save.the.Bears"], "S06E31")[0] == "match"
print("PASS (name_check)")


def sub(i, release, files=(), season=6, ep=31, title="Pups Save the Bears", dl=10):
    return {"id": str(i), "attributes": {"release": release, "files": [{"file_id": i, "file_name": f} for f in files],
            "download_count": dl, "feature_details": {"season_number": season, "episode_number": ep, "title": title}}}


# The most downloaded subtitle is named for the wrong episode: skipped. IMDb's title
# disagrees with TVDB's, but the next one's file name carries TVDB's title: verified.
res = [sub(1, "Paw.Patrol.S06E31.Pups.Save.the.Beavers.720p", dl=900, title="Bears"),
       sub(2, "Paw.Patrol.S06E31.720p.HDTV", dl=500, title="Bears"),
       sub(3, "", ["Paw Patrol - S06E31 - Pups Save the Bears.srt"], dl=5, title="Bears")]
pick = R._pick(res, "Pups Save the Bears", 6, 31, True, EPS, "Paw Patrol")
assert pick[3]["id"] == "3" and pick[2][0] == "match" and pick[0], pick


class FakeOS:
    configured, remaining, token = True, 50, None
    results = res

    def __init__(self): pass
    def login(self): pass
    def close(self): pass
    def search(self, **q): return self.results
    def download(self, fid): return "1\n00:00:01,000 --> 00:00:02,000\n" + "Chase is on the case! " * 40


series = {"tvdb_id": 7, "name": "Paw Patrol", "imdb_id": "tt3121722", "episodes": EPS}
ref, how = R.fetch_opensubs(FakeOS(), series, EPS[1], None)
print(" ", how)
assert ref["numbering_conflict"] is False and ref["name_verified"] and "Pups Save the Bears" in ref["release_name"]
assert not R.is_low_trust(ref)
FakeOS.results = res[:1]
assert R.fetch_opensubs(FakeOS(), series, EPS[1], None)[0] is None, "only a wrongly named subtitle: none"

# Already-downloaded flagged references: looked up again (no download) and settled by name.
R._save(7, "S06E31", {"source": "opensubtitles", "text": "x " * 300, "numbering_conflict": True, "subtitle_id": "3"})
R._save(7, "S06E32", {"source": "opensubtitles", "text": "y " * 300, "numbering_conflict": True, "subtitle_id": "9"})
R._save(7, "S06E33", {"source": "opensubtitles", "text": "z " * 300, "numbering_conflict": True, "subtitle_id": "4"})
FakeOS.results = res + [sub(9, "Paw.Patrol.S06E32.Pups.Save.the.Beavers.1080p", ep=32),
                        sub(4, "Paw.Patrol.S06E33.1080p", ep=33)]
R.OpenSubtitles = FakeOS


class Ctx:
    def log(self, m): print("   ", m)
    def check_cancel(self): pass


out = R.check_release_names(series, Ctx())
print(" ", out)
refs = R.load_refs(7)
assert out == {"verified": 1, "removed": 1, "checked": 3}, out
assert not R.is_low_trust(refs["S06E31"]) and "S06E32" not in refs and R.is_low_trust(refs["S06E33"])
assert "S06E30" in R._load_misses(7)["S06E32"]
assert R.check_release_names(series, Ctx())["checked"] == 0, "each is looked up once"
print("PASS")
