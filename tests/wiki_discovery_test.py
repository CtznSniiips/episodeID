"""Wiki discovery against a mocked Fandom (no network).

- Gumball: found at theamazingworldofgumball.fandom.com via Category:Transcripts
  (paginated); "The Re-Run", "The Origins" and "The Origins (2)" map to
  "The Rerun/Transcript", "The Origins/Transcript", "The Origins: Part Two/Transcript"
  with no manual overrides; a same-named-ish wiki without transcripts is rejected.
- SpongeBob SquarePants: found at spongebob.fandom.com via title search
  ("Transcript:..." pages, no category).
- A show with no wiki: nothing found.
- End to end: Fetch references uses the mapped page names.
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_wiki"))
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"),
                  CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, fandom, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

GUMBALL_PAGES = ["The DVD/Transcript", "The Responsible/Transcript", "The Third/Transcript",
                 "The Debt/Transcript", "The End/Transcript", "The Dress/Transcript",
                 "The Quest/Transcript", "The Spoon/Transcript", "The Rerun/Transcript",
                 "The Origins/Transcript", "The Origins: Part Two/Transcript",
                 "The Voice/Transcript", "The Promise/Transcript", "Darwin's Yearbook/Transcript"]
GUMBALL_EPS = ["The DVD", "The Responsible", "The Third", "The Debt", "The End", "The Dress",
               "The Quest", "The Spoon", "The Re-Run", "The Origins", "The Origins (2)",
               "The Voice", "The Promise", "Darwin's Yearbook - Teachers", "The Unknown Ep"]
SPONGE_PAGES = ["Transcript:Help Wanted", "Transcript:Reef Blower", "Transcript:Tea at the Treedome",
                "Transcript:Bubblestand", "Transcript:Ripped Pants", "Transcript:Jellyfishing",
                "Transcripts"]
SPONGE_EPS = ["Help Wanted", "Reef Blower", "Tea at the Treedome", "Bubblestand", "Ripped Pants",
              "Jellyfishing", "Plankton!"]
requests = []


def handler(req: httpx.Request) -> httpx.Response:
    host, q = req.url.host, {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    requests.append((host, q.get("list") or q.get("meta")))
    wikis = {"theamazingworldofgumball.fandom.com": ("The Amazing World of Gumball Wiki", "cat"),
             "amazingworldofgumball.fandom.com": ("Some Other Wiki", None),
             "spongebob.fandom.com": ("Encyclopedia SpongeBobia", "search")}
    if host not in wikis:
        return httpx.Response(200, text="<html>Not a valid community</html>")
    sitename, style = wikis[host]
    if q.get("meta") == "siteinfo":
        return httpx.Response(200, json={"query": {"general": {"sitename": sitename}}})
    if q.get("list") == "categorymembers":
        if style != "cat" or q["cmtitle"] != "Category:Transcripts":
            return httpx.Response(200, json={"query": {"categorymembers": []}})
        half = len(GUMBALL_PAGES) // 2
        if "cmcontinue" not in q:
            return httpx.Response(200, json={
                "query": {"categorymembers": [{"title": t} for t in GUMBALL_PAGES[:half]]},
                "continue": {"cmcontinue": "page2", "continue": "-||"}})
        return httpx.Response(200, json={
            "query": {"categorymembers": [{"title": t} for t in GUMBALL_PAGES[half:]]}})
    if q.get("list") == "search":
        hits = SPONGE_PAGES if style == "search" else []
        return httpx.Response(200, json={"query": {"search": [{"title": t} for t in hits]}})
    if q.get("list") == "allpages":
        return httpx.Response(200, json={"query": {"allpages": []}})
    return httpx.Response(404)


fandom._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
references.fandom_page = lambda wiki, page: (None, "missing")  # used by the old fallback only
eps = lambda titles: [{"season": 1 if i < 9 else 4, "episode": i + 1, "title": t}  # noqa: E731
                      for i, t in enumerate(titles)]

# --- unit checks
assert fandom.norm_ep("The Origins (2)") == fandom.norm_ep("The Origins: Part Two") == "origins part 2"
assert fandom.page_core("Transcript:Help Wanted") == "Help Wanted"
print("slugs:", fandom.slug_candidates("The Amazing World of Gumball"))
print("slugs:", fandom.slug_candidates("SpongeBob SquarePants"))

g = fandom.discover("The Amazing World of Gumball", eps(GUMBALL_EPS))
print("gumball:", json.dumps({k: v for k, v in g.items() if k != "page_map"}))
m = g["page_map"]
for code in sorted(m):
    print(f"   {code} → {m[code]}")
assert g["wiki"] == "theamazingworldofgumball" and g["found_via"] == "Category:Transcripts"
titles = {f"S{e['season']:02d}E{e['episode']:02d}": e["title"] for e in eps(GUMBALL_EPS)}
inv = {titles[c]: p for c, p in m.items()}
assert inv["The Re-Run"] == "The Rerun/Transcript"
assert inv["The Origins"] == "The Origins/Transcript"
assert inv["The Origins (2)"] == "The Origins: Part Two/Transcript"
assert "The Unknown Ep" not in inv and len(m) == 13
assert g["pattern"] == "{title}/Transcript"

s = fandom.discover("SpongeBob SquarePants", eps(SPONGE_EPS))
print("spongebob:", s and {k: v for k, v in s.items() if k != "page_map"})
assert s["wiki"] == "spongebob" and s["mapped"] == 6 and s["pattern"] == "Transcript:{title}"

assert fandom.discover("Some Obscure Drama", eps(["Pilot", "Second"])) is None

# --- end to end: discovery on fetch, then references come from the mapped pages
fetched = []
references.fandom_page = lambda wiki, page: (fetched.append(page) or
                                             "\n".join(f"Gumball: line {i} of {page}" for i in range(40)),
                                             "ok")
db.init()
sid = db.add_series(1, "The Amazing World of Gumball", "2011", str(W), None)
db.update_series(sid, episodes=[{**e, "overview": ""} for e in eps(GUMBALL_EPS)])
j = db.add_job("fetch_refs", sid, {})
pipeline.job_fetch_refs(JobContext(j, sid), {})
print(db.get_job(j)["log"])
opts = db.get_series(sid)["options"]
assert opts["fandom_wiki"] == "theamazingworldofgumball" and opts["fandom_discovery"]["mapped"] == 13
assert "The Rerun/Transcript" in fetched and "The Origins: Part Two/Transcript" in fetched
print("PASS")
