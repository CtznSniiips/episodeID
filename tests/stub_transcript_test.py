"""Stub wiki pages must not become references.

Uses a real PAW Patrol stub page ("The transcript for this episode isn't available
yet" + a navbox listing every episode) as cached by an earlier version, plus a real
looking transcript and a long transcript without speaker names.
"""
import json
import os
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_stub"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shutil  # noqa: E402

from app import db, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

STUB = Path(__file__).with_name("fixtures") / "pawpatrol_stub_S10E18.json"
stub = json.loads(STUB.read_text())
good = "\n".join(f"{['Ryder', 'Chase', 'Marshall', 'Skye'][i % 4]}: line number {i} about saving "
                 f"the day with a rescue and a whole lot of words" for i in range(60))
unlabelled = "\n".join(f"We need to get to the farm before the storm number {i} arrives today now"
                       for i in range(90))

ok, why = references.transcript_quality(stub["text"])
print("stub:", ok, "-", why)
assert not ok and "stub" in why
assert references.transcript_quality(good)[0]
assert references.transcript_quality(unlabelled)[0]
assert not references.transcript_quality("Short page with a few words.")[0]

# A navbox line never survives cleaning of an unlabelled page
navbox = "Season 1 \n Pups Make a Splash | Pups Fall Festival | Pups Save the Sea Turtles | Pups and the Very Big Baby"
assert "Splash" not in references.clean_transcript(unlabelled + "\n" + navbox)

if W.exists():
    shutil.rmtree(W)
refs = W / "cache" / "refs" / "4242"
refs.mkdir(parents=True)
(refs / "S10E18.json").write_text(json.dumps(stub))
(refs / "S10E19.json").write_text(json.dumps({"source": "fandom", "text": good, "page": "x"}))
loaded = references.load_refs(4242)
print("loaded refs:", sorted(loaded))
assert "S10E18" not in loaded and "S10E19" in loaded

# Fetch: the wiki still serves the stub → falls back to OpenSubtitles (mocked here)
references.fandom_page = lambda wiki, page: (stub["text"], "ok")
calls = []


class FakeOS:
    configured = True
    remaining = 10

    def login(self): pass
    def close(self): pass


references.OpenSubtitles = FakeOS
references.fetch_opensubs = lambda os_, series, ep, ctx: (
    calls.append(ep["episode"]) or ({"source": "opensubtitles", "text": good, "via": "series + episode number",
                                     "title_verified": True}, "series + episode number"))
db.init()
sid = db.add_series(4242, "PAW Patrol", "2013", str(W), "tt1")
db.update_series(sid, episodes=[
    {"season": 10, "episode": 18, "title": "Pups Save a Flying Farmer Yumi", "overview": ""},
    {"season": 10, "episode": 19, "title": "Pups Stop the Falling Space Junk", "overview": ""}],
    options={"fandom_wiki": "pawpatrol", "fandom_checked": True, "fandom_page_map": {}})
j = db.add_job("fetch_refs", sid, {})
pipeline.job_fetch_refs(JobContext(j, sid), {})
print(db.get_job(j)["log"])
assert calls == [18], calls
assert references.load_refs(4242)["S10E18"]["source"] == "opensubtitles"
print("PASS")
