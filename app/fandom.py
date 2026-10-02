"""Finding a series' Fandom wiki and its transcript pages automatically.

1. Candidate wiki addresses are built from the series name the way Fandom
   communities are usually named ("The Amazing World of Gumball" →
   theamazingworldofgumball, amazingworldofgumball, the-amazing-world-of-gumball,
   …, "SpongeBob SquarePants" → spongebob). Each is checked with one cheap
   MediaWiki siteinfo request.
2. For a wiki that exists, its transcript pages are listed straight from the wiki
   (transcript categories, then a title search, then common prefixes) — no
   guessing of page names.
3. Each transcript page is matched to a TVDB episode by title, tolerant of
   punctuation and part numbers ("The Re-Run" ↔ "The Rerun/Transcript",
   "The Origins (2)" ↔ "The Origins: Part Two/Transcript").
4. A wiki is accepted only if a good share of the series' episodes map to
   transcript pages, which also rules out a same-named wiki about something else.
"""
from __future__ import annotations

import difflib
import html
import re
import time

import httpx

from .references import BROWSER_UA

TRANSCRIPT_CATEGORIES = ("Transcripts", "Episode transcripts", "Transcript",
                         "Episode Transcripts", "Season transcripts")
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
              "i": "1", "ii": "2", "iii": "3", "iv": "4"}
_TRANSCRIPT_BITS = re.compile(
    r"(?i)/\s*transcripts?$|^transcripts?\s*[:/]\s*|\s*\((?:episode\s+)?transcript\)$|"
    r"\s+transcripts?$")


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": BROWSER_UA})


def _get_json(c: httpx.Client, url: str, params: dict) -> dict | None:
    for attempt in range(3):
        try:
            r = c.get(url, params=params)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None  # HTML, e.g. "this community doesn't exist"
        if r is not None and r.status_code == 404:
            return None
        time.sleep(1.5 + attempt * 3)  # rate-limited or flaky
    return None


# ---------------------------------------------------------------- titles

def norm_ep(t: str) -> str:
    """Normalise an episode title for matching, keeping part numbers."""
    t = html.unescape(t or "").lower().replace("&", " and ")
    t = re.sub(r"\((\d+)\)\s*$", r" part \1", t)                       # "(2)" → part 2
    t = re.sub(r"\bpart\s+(one|two|three|four|five|i{1,3}|iv)\b",
               lambda m: "part " + _NUM_WORDS[m.group(1)], t)
    t = re.sub(r"\bpt\.?\s*(\d)", r"part \1", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return t


def page_core(page: str) -> str:
    """Episode-title part of a transcript page name."""
    core = _TRANSCRIPT_BITS.sub("", page.strip())
    core = re.sub(r"\s*\((?:episode|tv episode|[^)]*season[^)]*)\)\s*$", "", core, flags=re.I)
    return core.strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def map_pages(episodes: list[dict], pages: list[str]) -> dict[str, str]:
    """code -> transcript page. One page per episode, best matches first."""
    eps = [(f"S{e['season']:02d}E{e['episode']:02d}", norm_ep(e.get("title") or ""))
           for e in episodes if e.get("title") and not re.fullmatch(r"(?i)episode \d+", e["title"])]
    cores = [(p, norm_ep(page_core(p))) for p in dict.fromkeys(pages)]
    pairs = []
    for code, et in eps:
        if len(et) < 2:
            continue
        for page, pt in cores:
            if abs(len(pt) - len(et)) > max(6, len(et) // 2):
                continue
            sc = _similar(et, pt)
            if sc >= 0.85:
                pairs.append((sc, code, page))
    pairs.sort(key=lambda x: -x[0])
    used_codes, used_pages, out = set(), set(), {}
    for sc, code, page in pairs:
        if code in used_codes or page in used_pages:
            continue
        out[code] = page
        used_codes.add(code)
        used_pages.add(page)
    return out


def infer_pattern(mapping: dict[str, str], episodes: list[dict]) -> str | None:
    """The page-name pattern most mapped pages follow, e.g. "{title}/Transcript"."""
    titles = {f"S{e['season']:02d}E{e['episode']:02d}": e.get("title") or "" for e in episodes}
    counts: dict[str, int] = {}
    for code, page in mapping.items():
        t = titles.get(code, "")
        if t and t in page:
            pat = page.replace(t, "{title}", 1)
            counts[pat] = counts.get(pat, 0) + 1
    return max(counts, key=counts.get) if counts else None


# ------------------------------------------------------------------ wikis

def slug_candidates(series_name: str) -> list[str]:
    name = re.sub(r"\(\d{4}\)|\[[^\]]*\]|\{[^}]*\}", " ", series_name or "")
    name = html.unescape(name).lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", name)
    if not words:
        return []
    no_the = words[1:] if words[0] in ("the", "a", "an") and len(words) > 1 else words
    c = ["".join(words), "".join(no_the), "-".join(words), "-".join(no_the)]
    if len(no_the) > 1:
        c += [no_the[0], "".join(no_the[:2]), "-".join(no_the[:2])]
    c += [x + "wiki" for x in ("".join(no_the),)]
    return [s for s in dict.fromkeys(c) if len(s) >= 3 and s not in ("the", "and", "show")]


def wiki_host(wiki: str) -> str:
    wiki = re.sub(r"^https?://", "", wiki.strip()).rstrip("/")
    return wiki if "." in wiki else f"{wiki}.fandom.com"


def site_info(c: httpx.Client, wiki: str) -> dict | None:
    j = _get_json(c, f"https://{wiki_host(wiki)}/api.php",
                  {"action": "query", "meta": "siteinfo", "format": "json"})
    gen = ((j or {}).get("query") or {}).get("general")
    return gen or None


def list_transcript_pages(c: httpx.Client, wiki: str, limit: int = 3000) -> tuple[list[str], str]:
    """All transcript page titles on a wiki, and how they were found."""
    api = f"https://{wiki_host(wiki)}/api.php"
    for cat in TRANSCRIPT_CATEGORIES:
        pages, cont = [], {}
        while len(pages) < limit:
            j = _get_json(c, api, {"action": "query", "list": "categorymembers",
                                   "cmtitle": f"Category:{cat}", "cmlimit": "500",
                                   "cmtype": "page", "format": "json", **cont})
            if not j:
                break
            pages += [m["title"] for m in (j.get("query") or {}).get("categorymembers", [])]
            cont = j.get("continue") or {}
            if not cont:
                break
        if len(pages) >= 3:
            return pages, f"Category:{cat}"
    # Title search (finds "X/Transcript", "Transcript:X", "X (transcript)")
    pages, offset = [], 0
    while len(pages) < limit:
        j = _get_json(c, api, {"action": "query", "list": "search", "srsearch": "transcript",
                               "srwhat": "title", "srlimit": "500", "sroffset": offset,
                               "format": "json"})
        hits = ((j or {}).get("query") or {}).get("search", [])
        pages += [h["title"] for h in hits if "transcript" in h["title"].lower()]
        nxt = ((j or {}).get("continue") or {}).get("sroffset")
        if not hits or nxt is None:
            break
        offset = nxt
    if len(pages) >= 3:
        return pages, "title search"
    # Prefix listing ("Transcript:..." pages in the main namespace)
    j = _get_json(c, api, {"action": "query", "list": "allpages", "apprefix": "Transcript",
                           "aplimit": "500", "format": "json"})
    pages = [p["title"] for p in ((j or {}).get("query") or {}).get("allpages", [])]
    return pages, "page prefix"


def discover(series_name: str, episodes: list[dict], ctx=None,
             min_share: float = 0.15, min_pages: int = 5) -> dict | None:
    """Find the wiki and map episodes to transcript pages.
    Returns {"wiki", "sitename", "page_map", "pattern", "mapped", "found_via"} or None."""
    regular = [e for e in episodes if e["season"] >= 1]
    need = max(min_pages, int(min_share * len(regular))) if regular else min_pages
    best = None
    with _client() as c:
        for slug in slug_candidates(series_name):
            info = site_info(c, slug)
            if not info:
                continue
            pages, how = list_transcript_pages(c, slug)
            mapping = map_pages(regular, pages) if pages else {}
            if ctx:
                ctx.log(f"  {slug}.fandom.com ({info.get('sitename')}): {len(pages)} transcript "
                        f"pages, {len(mapping)} match episodes of this series")
            if len(mapping) >= need and (best is None or len(mapping) > len(best["page_map"])):
                best = {"wiki": slug, "sitename": info.get("sitename"), "page_map": mapping,
                        "pattern": infer_pattern(mapping, regular), "mapped": len(mapping),
                        "found_via": how}
                if len(mapping) >= 0.8 * len(regular):
                    break  # good enough; don't keep probing
    return best
