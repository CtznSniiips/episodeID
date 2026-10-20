"""Swapped references and per-season fetching.
1. Title cards confirm two files' names but each file's dialogue matched the other's
   reference: the pair is reported (scan log and the series' ref_swaps) and Swap
   references exchanges them — downloaded and uploaded alike — and trusts them.
2. "Fetch missing" for one season fetches only that season's aired episodes without
   a reference, trying "not found" ones again."""
import json
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_refswap"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"),
                  WHISPER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, main, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

root = W / "media" / "tv" / "Show"
(root / "Season 1").mkdir(parents=True)
db.init()
EPS = [{"season": 1, "episode": n, "title": f"Ep {n}", "aired": "2020-01-0%d" % n} for n in range(1, 5)] + \
      [{"season": 2, "episode": n, "title": f"Two {n}", "aired": "2021-01-0%d" % n} for n in range(1, 5)] + \
      [{"season": 2, "episode": 5, "title": "Future", "aired": "2999-01-01"}]
sid = db.add_series(7, "Show", "2020", str(root), None)
db.update_series(sid, episodes=EPS, options={})
references._save(7, "S01E01", {"source": "opensubtitles", "text": "two two two", "via": "IMDb",
                               "numbering_conflict": True, "os_number": "S01E02"})
references._save(7, "S01E02", {"source": "opensubtitles", "text": "one one one", "via": "IMDb"})
md = references.manual_dir(7)
md.mkdir(parents=True, exist_ok=True)
(md / "S01E03.txt").write_text("FOUR: four four four four four.")
(md / "S01E04.txt").write_text("THREE: three three three three three.")


def seg(code, dlg, ev="title+filename"):
    return {"start": 0, "end": 600, "code": code, "dialogue_code": dlg, "confidence": "high", "evidence": ev}


files = [{"rel": "Season 1/E01.mkv", "segments": [seg("S01E01", "S01E02")]},
         {"rel": "Season 1/E02.mkv", "segments": [seg("S01E02", "S01E01")]},
         {"rel": "Season 1/E03.mkv", "segments": [seg("S01E03", "S01E04")]},
         {"rel": "Season 1/E04.mkv", "segments": [seg("S01E04", "S01E03")]},
         # one-sided, or not settled by a title card: not a pair
         {"rel": "Season 2/A.mkv", "segments": [seg("S02E01", "S02E02")]},
         {"rel": "Season 2/B.mkv", "segments": [seg("S02E02", "S02E03", ev="dialogue")]}]
sw = pipeline.reference_swaps(files)
assert [p["codes"] for p in sw] == [["S01E01", "S01E02"], ["S01E03", "S01E04"]], sw
db.add_scan(sid, {"files": files, "counts": {}})
c = TestClient(main.app)
s = c.get(f"/api/series/{sid}").json()
assert [p["codes"] for p in s["ref_swaps"]] == [["S01E01", "S01E02"], ["S01E03", "S01E04"]], s["ref_swaps"]
assert c.post(f"/api/series/{sid}/swap-references", json={"codes": ["S01E01", "S09E09"]}).status_code == 400
assert c.post(f"/api/series/{sid}/swap-references", json={"codes": ["S01E01", "S01E02"]}).json()["swapped"] == 2
assert c.post(f"/api/series/{sid}/swap-references", json={"codes": ["S01E03", "S01E04"]}).json()["swapped"] == 2
refs = references.load_refs(7)
assert refs["S01E01"]["text"] == "one one one" and refs["S01E02"]["text"] == "two two two"
assert not references.is_low_trust(refs["S01E02"]) and refs["S01E02"]["swapped_from"] == "S01E01"
assert "four" in refs["S01E04"]["text"] and "three" in refs["S01E03"]["text"]
s = c.get(f"/api/series/{sid}").json()
assert s["ref_swaps"] == [], "both pairs are marked swapped until the next scan"
note = next(e for e in s["episodes"] if e["code"] == "S01E02")["ref_note"]
assert "swapped" in note, note
# A scan after the swap that still sees the pair shows it again (the swap didn't help).
db.add_scan(sid, {"files": files, "counts": {}})
assert len(c.get(f"/api/series/{sid}").json()["ref_swaps"]) in (0, 2)  # same second: either
db.conn().execute("UPDATE scans SET created='2999-01-01T00:00:00+00:00' WHERE id=(SELECT max(id) FROM scans)")
assert len(c.get(f"/api/series/{sid}").json()["ref_swaps"]) == 2
print("PASS (swapped references found and swapped)")

# 2. Season 2: S02E01 has a reference, S02E02 was "not found", S02E03–04 never tried,
#    S02E05 hasn't aired. Season 1's not-found S01E09-style entries stay untouched.
references._save(7, "S02E01", {"source": "manual", "text": "x"})
references._save_misses(7, {"S02E02": "nothing on OpenSubtitles", "S01E02": "kept"})
asked = []


def fake_refresh(series, ctx, force_codes=None, retry_misses=False, retry_codes=None, only_codes=None):
    asked.append((sorted(only_codes or []), sorted(retry_codes or [])))
    return {"fetched": 0}


pipeline.refresh_references = fake_refresh
pipeline._find_wiki = lambda series, ctx, force=False: series
pipeline.check_release_names = lambda *a, **k: {}
pipeline.verify_flagged = lambda *a, **k: {}
pipeline._ensure_episodes = lambda series, ctx, max_age_h=0: series
pipeline.job_fetch_refs(JobContext(db.add_job("fetch_refs", sid, {"season": 2}), sid), {"season": 2})
assert asked == [(["S02E02", "S02E03", "S02E04"], ["S02E02", "S02E03", "S02E04"])], asked
print("PASS (season fetch picks the right episodes)")

# The real refresh_references honours only_codes: nothing outside them is fetched.
pipeline.refresh_references = references.refresh_references
logged = []


class Ctx:
    def log(self, m): logged.append(m)
    def status(self, m): pass
    def progress(self, *a): pass
    def check_cancel(self): pass


series = db.get_series(sid)
references.refresh_references(series, Ctx(), only_codes=["S02E03"], retry_codes=["S02E03"])
assert any("1 to fetch" in m for m in logged), logged
assert json.loads(references._miss_file(7).read_text()).get("S01E02") == "kept"
print("PASS (only_codes limits the fetch)")
