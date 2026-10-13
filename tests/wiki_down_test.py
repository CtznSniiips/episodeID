"""LazyTown log: the wiki stopped answering, so 59 episodes got nothing — OpenSubtitles
was never asked, and each episode took ~36 s of retries. Now: OpenSubtitles stands in,
the wiki is left alone after 3 failures in a row, and nothing is recorded as "not found".
Also: subtitles OpenSubtitles files under other numbers are found by episode title."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_wdown"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  OPENSUBTITLES_API_KEY="k")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import references as R  # noqa: E402

EPS = [{"season": 1, "episode": n, "title": t} for n, t in (
    (1, "Welcome to LazyTown"), (2, "Crystal Caper"), (3, "Dr. Rottenstein"), (4, "Rottenbeard"),
    (5, "Cry Dinosaur"))]
wiki_calls = []


def down(wiki, page):
    wiki_calls.append(page)
    R.LAST_WIKI_ERROR["why"] = "HTTP 429"
    return None, "error"


def sub(i, season, ep, title, release=""):
    return {"id": str(i), "attributes": {"release": release, "download_count": 5,
            "files": [{"file_id": i, "file_name": release}],
            "feature_details": {"season_number": season, "episode_number": ep, "title": title,
                                "parent_title": "LazyTown", "parent_imdb_id": 343218}}}


class FakeOS:
    configured, remaining, token = True, 99, None
    searches = []

    def __init__(self): pass
    def login(self): pass
    def close(self): pass

    def search(self, **q):
        FakeOS.searches.append(q)
        if "season_number" in q:  # IMDb numbers LazyTown differently: S01E02 there is "Rottenbeard"
            n = int(q["episode_number"])
            return [sub(100 + n, 1, n, EPS[(n + 1) % 5]["title"])]
        if q.get("query"):
            t = q["query"].replace("LazyTown ", "")
            hit = [e for e in EPS if e["title"] == t]
            return [sub(200 + hit[0]["episode"], 2, 9, t, f"LazyTown.S02E09.{t.replace(' ', '.')}.DVDRip")] if hit else []
        return []

    def download(self, fid): return "1\n00:00:01,000 --> 00:00:02,000\n" + f"Sportacus line {fid}! " * 40


R.OpenSubtitles = FakeOS
R.fandom_page = down


class Ctx:
    def log(self, m): print("   ", m)
    def progress(self, *a): pass
    def status(self, *a): pass
    def check_cancel(self): pass


series = {"tvdb_id": 9, "name": "LazyTown", "imdb_id": "tt0343218", "episodes": EPS,
          "options": {"fandom_wiki": "lazytown"}}
out = R.refresh_references(series, Ctx())
print(" ", out)
refs = R.load_refs(9)
assert len(wiki_calls) == 3, wiki_calls  # stopped asking after 3 failures in a row
assert set(refs) == {f"S01E0{n}" for n in range(1, 6)}, refs.keys()
assert all(r["via"].startswith("episode title") for r in refs.values()), [r["via"] for r in refs.values()]
assert all(not R.is_low_trust(r) for r in refs.values()), "file names carry the TVDB titles"
assert R._load_misses(9) == {}
# a title the series doesn't have isn't taken from another show
FakeOS.searches.clear()
series2 = {"tvdb_id": 10, "name": "Other Show", "imdb_id": None, "options": {},
           "episodes": [{"season": 1, "episode": 1, "title": "Crystal Caper"}]}
o2 = R.refresh_references(series2, Ctx())
assert o2["fetched"] == 0 and "S01E01" in R._load_misses(10)
assert any(q.get("query") for q in FakeOS.searches)
print("PASS")
