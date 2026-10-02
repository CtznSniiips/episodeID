"""Reference text per episode — what each TVDB episode is supposed to say.

Sources, in priority order:
  1. manual    — files you drop in /config/references/<tvdb_id>/S01E02*.srt|.txt
  2. opensubs  — OpenSubtitles.com, looked up by the episode's IMDb ID (from TVDB),
                 falling back to series IMDb ID + season/episode with a title check
  3. fandom    — a Fandom wiki transcript page (optional, per series)

Everything is cached in /cache/refs/<tvdb_id>/SxxEyy.json, so each episode is
only downloaded once — important with OpenSubtitles' daily download cap.
"""
from __future__ import annotations

import difflib
import html
import json
import re
import threading
import time
from pathlib import Path

import httpx

from . import tvdb
from .config import CACHE_DIR, CONFIG_DIR, get_settings
from .media_text import SUB_EXTS, parse_subtitle_text, read_text_file
from . import __version__

OS_BASE = "https://api.opensubtitles.com/api/v1"
UA = f"EpisodeID v{__version__}"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def ep_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def refs_dir(tvdb_id: int) -> Path:
    return CACHE_DIR / "refs" / str(tvdb_id)


def manual_dir(tvdb_id: int) -> Path:
    return CONFIG_DIR / "references" / str(tvdb_id)


def norm_title(t: str) -> str:
    t = html.unescape(t or "").lower()
    t = re.sub(r"\(\d+\)$", "", t.strip())
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def titles_match(a: str, b: str) -> bool:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75


# ------------------------------------------------------- transcript cleaning

# "Gumball: text", "Mr. Small: text", "Nicole (whispering): text"
_SPEAKER = re.compile(r"^\s*([A-Z][\w.'’&\- ]{0,40}?)(?:\s*\([^)]{0,40}\))?\s*:\s+(\S.*)$")
_DIRECTION = re.compile(r"\[[^\]]*\]|\{\{[^}]*\}\}")
_WIKI = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]|'{2,}|<[^>]+>")


def transcript_stats(text: str) -> tuple[int, int]:
    """(speaker-labelled lines, substantive lines)."""
    lines = [l for l in text.splitlines() if len(l.strip()) > 3]
    return sum(1 for l in lines if _SPEAKER.match(_WIKI.sub(r"\1", l))), len(lines)


def clean_transcript(text: str) -> str:
    """Reduce a wiki/plain-text transcript to spoken dialogue only, so it looks like
    the subtitles it is compared against. Transcript pages typically label every
    line with a speaker ("Gumball: ...") and add [stage directions], section
    headings and an episode navigation box — none of that is in a subtitle, and
    speaker names in particular make episodes with the same characters look alike."""
    text = _WIKI.sub(r"\1", text)
    labelled, total = transcript_stats(text)
    out = []
    if total and labelled >= 10 and labelled >= 0.3 * total:
        # Speaker-labelled transcript: keep only dialogue lines. This also drops
        # headings, navboxes, categories and free-standing scene descriptions.
        for line in text.splitlines():
            m = _SPEAKER.match(line)
            if m:
                out.append(m.group(2))
    else:
        out = [l for l in text.splitlines() if not _NAV_LINE.search(l)]
    joined = " ".join(_DIRECTION.sub(" ", l) for l in out)
    joined = re.sub(r"\([^)]{0,80}\)", " ", joined)
    return re.sub(r"\s+", " ", joined).strip()


_STUB = re.compile(
    r"(?i)\bis a stub\b|transcript (for this episode )?(isn'?t|is not) (available|complete)|"
    r"help the .{0,40}wiki by adding|this transcript is (empty|incomplete)|no transcript (yet|available)")
_NAV_LINE = re.compile(r"(?:[^|\n]{2,80}\|){3,}")  # "Ep A | Ep B | Ep C | …" navigation lists

MIN_TRANSCRIPT_WORDS = 250


def transcript_quality(raw: str, cleaned: str | None = None) -> tuple[bool, str]:
    """Is this wiki page an actual transcript? Stub pages ("The transcript for this
    episode isn't available yet") are mostly an episode-list navbox, which would look
    like a reference to the matcher — and every stub shares the same navbox."""
    labelled, _ = transcript_stats(_WIKI.sub(r"\1", raw))
    words = len((cleaned if cleaned is not None else clean_transcript(raw)).split())
    if labelled >= 15 and words >= MIN_TRANSCRIPT_WORDS:
        return True, ""
    if _STUB.search(raw):
        return False, "wiki transcript is a stub"
    if labelled < 15 and words >= 4 * MIN_TRANSCRIPT_WORDS:
        return True, ""  # long unlabelled transcript (some wikis don't name speakers)
    return False, f"wiki page has no usable transcript ({labelled} dialogue lines, {words} words)"


_ref_cache: dict[str, tuple[int, int, dict | None]] = {}
_ref_cache_lock = threading.Lock()


def _load_ref_file(f: Path) -> dict | None:
    """One reference file → {"source", "text" (cleaned), ...} or None if unusable.
    Parsed, quality-checked and cleaned once per file version (cached by mtime+size),
    because the series pages ask for every reference's status on every load."""
    try:
        st = f.stat()
    except OSError:
        return None
    key = str(f)
    with _ref_cache_lock:
        hit = _ref_cache.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    entry: dict | None = None
    if f.suffix == ".json":
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        if data.get("text"):
            if data.get("source") == "fandom":
                cleaned = clean_transcript(data["text"])
                if transcript_quality(data["text"], cleaned)[0]:
                    entry = {**data, "text": cleaned}
                # else: a stub page cached by an older version; re-fetched next time
            else:
                entry = data
    else:
        raw = read_text_file(f)
        if f.suffix.lower() == ".txt":
            text = clean_transcript(raw)
        else:
            text = " ".join(c.text for c in parse_subtitle_text(raw, f.suffix.lower()))
        if text.strip():
            entry = {"source": f"manual:{f.name}", "text": text}
    with _ref_cache_lock:
        _ref_cache[key] = (st.st_mtime_ns, st.st_size, entry)
    return entry


def load_refs(tvdb_id: int) -> dict[str, dict]:
    """code -> {"source", "text", ...}. Manual files override cached downloads.
    Transcript text (Fandom, manual .txt) is cleaned to dialogue here, at load time,
    so references cached by older versions benefit without being re-downloaded.
    Callers get the cached dicts — treat them as read-only."""
    out: dict[str, dict] = {}
    d = refs_dir(tvdb_id)
    if d.exists():
        for f in d.glob("S*E*.json"):
            entry = _load_ref_file(f)
            if entry:
                out[f.stem] = entry
    md = manual_dir(tvdb_id)
    if md.exists():
        for f in md.iterdir():
            m = re.match(r"(?i)s(\d+)e(\d+)", f.name)
            if not m or f.suffix.lower() not in SUB_EXTS + (".txt",):
                continue
            entry = _load_ref_file(f)
            if entry:
                out[ep_code(int(m[1]), int(m[2]))] = entry
    return out


def warm_reference_cache() -> None:
    """Read every series' references once at startup so the first page load is fast."""
    root = CACHE_DIR / "refs"
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and d.name.isdigit():
                load_refs(int(d.name))


def _save(tvdb_id: int, code: str, data: dict) -> None:
    d = refs_dir(tvdb_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{code}.json").write_text(json.dumps(data))


def _miss_file(tvdb_id: int) -> Path:
    return refs_dir(tvdb_id) / "_misses.json"


def _load_misses(tvdb_id: int) -> dict:
    try:
        return json.loads(_miss_file(tvdb_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_misses(tvdb_id: int, misses: dict) -> None:
    f = _miss_file(tvdb_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(misses, indent=1))


def clear_reference(tvdb_id: int, code: str) -> None:
    (refs_dir(tvdb_id) / f"{code}.json").unlink(missing_ok=True)
    misses = _load_misses(tvdb_id)
    if misses.pop(code, None) is not None:
        _save_misses(tvdb_id, misses)


# ----------------------------------------------------------- OpenSubtitles

class QuotaExhausted(Exception):
    pass


class OpenSubtitlesAuthError(RuntimeError):
    """The API key or account was rejected — no point trying further episodes."""


def _invalid_domain(r: httpx.Response) -> bool:
    return r.status_code == 401 and "invalid domain" in r.text.lower()


class OpenSubtitles:
    def __init__(self):
        s = get_settings()
        self.api_key = s["opensubtitles_api_key"]
        self.username = s["opensubtitles_username"]
        self.password = s["opensubtitles_password"]
        self.lang = s["subtitle_language"] or "en"
        self.token = None
        # Searches always go to the main API. The server returned at login (vip-api… for
        # VIP accounts) is used for downloads only: it answers searches with
        # 401 "Invalid domain".
        self.base = OS_BASE
        self.download_base = OS_BASE
        self.remaining = None
        self.client = httpx.Client(timeout=30, follow_redirects=True, headers={
            "Api-Key": self.api_key, "User-Agent": UA, "Accept": "application/json"})

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def close(self):
        self.client.close()

    def _req(self, method: str, path: str, base: str | None = None, **kw) -> httpx.Response:
        for attempt in range(4):
            r = self.client.request(method, f"{base or self.base}{path}", **kw)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            time.sleep(0.25)  # stay well under the per-second limit
            return r
        return r

    def login(self) -> None:
        if not (self.username and self.password):
            return
        r = self._req("POST", "/login", json={"username": self.username,
                                              "password": self.password})
        if r.status_code in (401, 403):
            raise OpenSubtitlesAuthError(
                f"OpenSubtitles rejected the login ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"OpenSubtitles login failed ({r.status_code}): {r.text[:200]}")
        j = r.json()
        self.token = j.get("token")
        if j.get("base_url"):
            host = re.sub(r"^https?://", "", j["base_url"]).split("/")[0]
            self.download_base = f"https://{host}/api/v1"
        self.remaining = (j.get("user") or {}).get("remaining_downloads")

    def search(self, **params) -> list[dict]:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params.setdefault("languages", self.lang)
        params = dict(sorted((k, str(v).lower() if k != "query" else v)
                             for k, v in params.items()))
        r = self._req("GET", "/subtitles", params=params)
        if r.status_code in (401, 403):
            raise OpenSubtitlesAuthError(
                f"OpenSubtitles rejected the API key ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"OpenSubtitles search failed ({r.status_code}): {r.text[:200]}")
        return r.json().get("data") or []

    def download(self, file_id: int) -> str:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        body = {"file_id": file_id, "sub_format": "srt"}
        r = self._req("POST", "/download", base=self.download_base, json=body, headers=headers)
        if _invalid_domain(r) and self.download_base != OS_BASE:
            self.download_base = OS_BASE  # VIP server refused; use the main API from now on
            r = self._req("POST", "/download", base=OS_BASE, json=body, headers=headers)
        if r.status_code in (406, 429):
            try:
                msg = r.json().get("message")
            except ValueError:
                msg = None
            raise QuotaExhausted(msg or r.text[:200])
        if r.status_code in (401, 403):
            raise OpenSubtitlesAuthError(
                f"OpenSubtitles refused the download ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"OpenSubtitles download failed ({r.status_code}): {r.text[:200]}")
        j = r.json()
        self.remaining = j.get("remaining", self.remaining)
        link = j.get("link")
        if not link:
            raise QuotaExhausted(j.get("message") or "No download link returned")
        raw = httpx.get(link, timeout=60, follow_redirects=True, headers={"User-Agent": UA})
        raw.raise_for_status()
        try:
            return raw.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.content.decode("latin-1")


def _pick(results: list[dict], want_title: str | None, season: int, episode: int,
          check_numbers: bool) -> dict | None:
    good = []
    for r in results:
        a = r.get("attributes") or {}
        fd = a.get("feature_details") or {}
        if a.get("machine_translated") or a.get("ai_translated"):
            continue
        if not a.get("files"):
            continue
        if check_numbers and (fd.get("season_number") != season or
                              fd.get("episode_number") != episode):
            continue
        title_ok = want_title is None or titles_match(fd.get("title") or "", want_title)
        good.append((title_ok, not a.get("hearing_impaired"), a.get("download_count") or 0, r))
    if not good:
        return None
    good.sort(key=lambda g: (g[0], g[1], g[2]), reverse=True)
    return good[0]


def fetch_opensubs(os_: OpenSubtitles, series: dict, ep: dict, ctx) -> tuple[dict | None, str]:
    """Returns (ref, note). Raises QuotaExhausted.

    Subtitle *text* on OpenSubtitles comes from release files, which are numbered the
    way Sonarr/TVDB number them; the *metadata* (title, IMDb id) comes from IMDb. For
    shows where IMDb orders episodes differently (Gumball's paired episodes, for
    one), looking a subtitle up by IMDb id or by title returns the neighbouring
    episode's dialogue. So: search by TVDB season/episode first, and flag any
    reference where IMDb's numbering disagrees so the planner treats it with care."""
    code = ep_code(ep["season"], ep["episode"])
    title = ep["title"]
    pick, how = None, ""
    if series.get("imdb_id"):
        res = os_.search(parent_imdb_id=series["imdb_id"].lstrip("t"),
                         season_number=ep["season"], episode_number=ep["episode"])
        p = _pick(res, title, ep["season"], ep["episode"], check_numbers=True)
        if p:
            pick, how = p, "series + episode number"
    if pick is None:
        imdb_ep = ep.get("imdb_id")
        if imdb_ep is None and ep.get("tvdb_episode_id"):
            try:
                imdb_ep = tvdb.episode_imdb_id(ep["tvdb_episode_id"]) or ""
            except Exception:  # noqa: BLE001
                imdb_ep = ""
            ep["imdb_id"] = imdb_ep
        if imdb_ep:
            res = os_.search(imdb_id=imdb_ep.lstrip("t"))
            p = _pick(res, title, ep["season"], ep["episode"], check_numbers=False)
            if p:
                pick, how = p, "episode IMDb id"
    if pick is None:
        return None, "no subtitle found"
    title_ok, _, _, r = pick
    a = r["attributes"]
    fd = a.get("feature_details") or {}
    os_number = f"S{fd.get('season_number') or 0:02d}E{fd.get('episode_number') or 0:02d}"
    conflict = bool(fd.get("episode_number")) and os_number != code or not title_ok
    os_title_code = title_code(series.get("episodes") or [], fd.get("title") or "")
    raw = os_.download(a["files"][0]["file_id"])
    cues = parse_subtitle_text(raw, ".srt")
    text = " ".join(c.text for c in cues)
    if len(text) < 200:
        return None, "downloaded subtitle was empty"
    return {
        "source": "opensubtitles",
        "text": text,
        "via": how,
        "os_title": fd.get("title"),
        "os_number": os_number,
        "title_verified": bool(title_ok),
        "numbering_conflict": conflict,
        "os_title_code": os_title_code,
        "subtitle_id": r.get("id"),
    }, how


# ------------------------------------------------------------------ Fandom

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h)
    h = re.sub(r"(?is)<(table|div|nav)\b[^>]*class=\"[^\"]*navbox.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</dd>|</li>", "\n", h)
    return re.sub(r"[ \t]+", " ", html.unescape(_HTML_TAG.sub(" ", h))).strip()


def normalize_wiki(value: str) -> str:
    """'pawpatrol', 'pawpatrol.fandom.com' or any URL on the wiki → 'pawpatrol' /
    'host.example.org' (non-Fandom MediaWiki hosts are kept as hosts)."""
    v = (value or "").strip()
    v = re.sub(r"^https?://", "", v, flags=re.I).split("/")[0].split("?")[0].lower()
    m = re.fullmatch(r"([a-z0-9-]+)\.fandom\.com", v)
    return m.group(1) if m else v


def normalize_page(value: str) -> str:
    """A page title as typed or pasted: full URL, /wiki/ path, underscores and
    %-escapes all become the plain title MediaWiki expects."""
    from urllib.parse import unquote
    v = (value or "").strip()
    if "/wiki/" in v:
        v = v.split("/wiki/", 1)[1]
    elif "title=" in v:
        v = v.split("title=", 1)[1].split("&")[0]
    v = v.split("#")[0].split("?")[0]
    return unquote(v).replace("_", " ").strip()


def fandom_page(wiki: str, page: str) -> tuple[str | None, str]:
    """Fetch a wiki page. Returns (text, status) where status is
    "ok", "missing" (the wiki says the page doesn't exist) or "error" (blocked,
    rate-limited or unreachable — worth retrying later, NOT evidence it's missing)."""
    wiki = normalize_wiki(wiki)
    page = normalize_page(page)
    host = wiki if "." in wiki else f"{wiki}.fandom.com"
    api = f"https://{host}/api.php"
    missing = False
    with httpx.Client(timeout=30, headers={"User-Agent": BROWSER_UA},
                      follow_redirects=True) as c:
        def get(url, params=None):
            for attempt in range(3):
                try:
                    r = c.get(url, params=params)
                except httpx.HTTPError:
                    r = None
                if r is not None and r.status_code not in (402, 403, 429) and r.status_code < 500:
                    return r
                time.sleep(2 + attempt * 4)  # throttled or flaky: back off and retry
            return r

        r = get(api, {"action": "parse", "page": page, "prop": "text", "format": "json",
                      "redirects": 1})
        if r is not None and r.status_code == 200:
            try:
                j = r.json()
            except ValueError:
                j = {}
            if "parse" in j:
                text = _strip_html(j["parse"]["text"]["*"])
                if len(text) > 500:
                    return text, "ok"
            elif (j.get("error") or {}).get("code") in ("missingtitle", "invalidtitle"):
                missing = True
        r = get(f"https://{host}/index.php", {"title": page, "action": "raw"})
        if r is not None and r.status_code == 200 and len(r.text) > 500:
            t = re.sub(r"\{\{[^}]*\}\}|\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", r.text)
            return re.sub(r"'{2,}|<[^>]+>", "", t), "ok"
        if r is not None and r.status_code == 404:
            missing = True
        r = get(f"https://{host}/wiki/{page.replace(' ', '_')}")
        if r is not None and r.status_code == 200:
            m = re.search(r'(?s)<div class="mw-parser-output">(.*?)<div class="printfooter', r.text)
            if m:
                text = _strip_html(m.group(1))
                if len(text) > 500:
                    return text, "ok"
        elif r is not None and r.status_code == 404:
            missing = True
    return None, "missing" if missing else "error"


def fetch_fandom(wiki: str, page: str) -> str | None:
    return fandom_page(wiki, page)[0]


def detect_fandom_wiki(series_name: str, episodes: list[dict], ctx=None) -> str | None:
    """Guess <slug>.fandom.com from the series name and confirm it really hosts
    "{title}/Transcript" pages by fetching one for an early episode and checking
    it reads like a speaker-labelled transcript (so a generic "wiki not found"
    page can't be mistaken for one)."""
    name = re.sub(r"\(\d{4}\)|\[[^\]]*\]|\{[^}]*\}", " ", series_name or "").lower()
    base = re.sub(r"[^a-z0-9]+", "", name)
    slugs = list(dict.fromkeys(s for s in (base, re.sub(r"^the", "", base)) if len(s) >= 3))
    probes = [e for e in episodes if e["season"] >= 1 and e.get("title")][:3]
    for slug in slugs:
        for ep in probes:
            try:
                text = fetch_fandom(slug, f"{ep['title']}/Transcript")
            except Exception:  # noqa: BLE001
                text = None
            if text and transcript_stats(text)[0] >= 15:
                return slug
    return None


# ------------------------------------------------------------------ driver

def _read_cached(tvdb_id: int, code: str) -> dict | None:
    try:
        return json.loads((refs_dir(tvdb_id) / f"{code}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def title_code(episodes: list[dict], title: str) -> str | None:
    """TVDB episode whose title is (nearly) exactly this title, if exactly one is."""
    want = norm_title(title)
    if len(want) < 3:
        return None
    hits = [ep_code(e["season"], e["episode"]) for e in episodes
            if norm_title(e.get("title") or "") == want]
    if not hits:
        hits = [ep_code(e["season"], e["episode"]) for e in episodes
                if difflib.SequenceMatcher(None, norm_title(e.get("title") or ""), want).ratio() >= 0.92]
    return hits[0] if len(hits) == 1 else None


def is_low_trust(r: dict) -> bool:
    return r.get("source") == "opensubtitles" and bool(
        r.get("numbering_conflict", not r.get("title_verified", True)))


def describe_conflict(r: dict, code: str, tvdb_title: str | None = None) -> str:
    """Plain-language reason an OpenSubtitles reference isn't trusted yet."""
    os_t, os_n, other = r.get("os_title") or "?", r.get("os_number"), r.get("os_title_code")
    if os_n and os_n != code:
        msg = f"OpenSubtitles files this subtitle under {os_n} '{os_t}'"
    else:
        msg = (f"OpenSubtitles (IMDb) calls {code} '{os_t}'"
               + (f" but TVDB calls it '{tvdb_title}'" if tvdb_title else ""))
    if other and other != code:
        msg += f"; '{os_t}' is {other} on TVDB"
    return msg + " — the subtitle may hold another episode's dialogue"


def verify_flagged(series: dict, ctx) -> dict:
    """Settle flagged OpenSubtitles references by comparing their dialogue with
    trustworthy references (wiki transcripts, manual uploads, unflagged subtitles):
      * it matches another episode's reference  → it's that episode's dialogue: removed
      * the episode its IMDb title points at has a reference, and this dialogue
        clearly isn't it                         → the label was IMDb's ordering: trusted
      * nothing to compare with yet              → stays flagged, re-checked next fetch"""
    tvdb_id = series["tvdb_id"]
    refs = load_refs(tvdb_id)
    flagged = {c: r for c, r in refs.items() if is_low_trust(r)}
    trusted = {c: r["text"] for c, r in refs.items() if not is_low_trust(r)}
    result = {"verified": 0, "removed": 0, "pending": len(flagged)}
    if not flagged or len(trusted) < 3:
        return result
    from sklearn.feature_extraction.text import TfidfVectorizer
    codes = sorted(trusted)
    vec = TfidfVectorizer(lowercase=True, strip_accents="unicode", sublinear_tf=True,
                          stop_words="english", max_df=0.6 if len(codes) >= 10 else 1.0,
                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z']+\b")
    T = vec.fit_transform([trusted[c] for c in codes])
    misses = _load_misses(tvdb_id)
    for code, r in sorted(flagged.items()):
        sims = (vec.transform([r["text"]]) @ T.T).toarray()[0]
        order = sims.argsort()[::-1]
        best_i = next((i for i in order if codes[i] != code), None)
        best_code, best = (codes[best_i], float(sims[best_i])) if best_i is not None else (None, 0.0)
        target = r.get("os_title_code")
        if best_code and best >= 0.35:
            (refs_dir(tvdb_id) / f"{code}.json").unlink(missing_ok=True)
            misses[code] = f"OpenSubtitles' subtitle for {code} was {best_code}'s dialogue"
            result["removed"] += 1
            result["pending"] -= 1
            ctx.log(f"{code} ✗ the OpenSubtitles subtitle is {best_code}'s dialogue "
                    f"(similarity {best:.2f} with its reference) — discarded")
        elif target and target != code and target in trusted and \
                float(sims[codes.index(target)]) < 0.15 and best < 0.25:
            raw = _read_cached(tvdb_id, code)
            if raw:
                raw.update(numbering_conflict=False,
                           verified=f"checked against {target}'s reference: different dialogue")
                _save(tvdb_id, code, raw)
            result["verified"] += 1
            result["pending"] -= 1
            ctx.log(f"{code} ✓ verified: its dialogue isn't {target} ('{r.get('os_title')}'), so "
                    "the OpenSubtitles label is just IMDb's ordering")
    _save_misses(tvdb_id, misses)
    if result["pending"]:
        ctx.log(f"{result['pending']} OpenSubtitles references still unverified (no reference "
                "to compare with yet); renames relying on them won't be pre-ticked.")
    return result


def ref_meta(tvdb_id: int) -> dict[str, dict]:
    """code -> where its reference came from and how far it can be trusted."""
    out = {}
    for code, r in load_refs(tvdb_id).items():
        out[code] = {"source": r.get("source", "").split(":")[0], "low_trust": is_low_trust(r),
                     "os_number": r.get("os_number"), "os_title": r.get("os_title"),
                     "os_title_code": r.get("os_title_code")}
    return out


def refresh_references(series: dict, ctx, force_codes: list[str] | None = None,
                       retry_misses: bool = False, retry_codes: list[str] | None = None) -> dict:
    tvdb_id = series["tvdb_id"]
    episodes = series["episodes"]
    opts = series.get("options") or {}
    have = load_refs(tvdb_id)
    misses = {} if retry_misses else _load_misses(tvdb_id)
    for code in retry_codes or []:
        misses.pop(code, None)
    for code in force_codes or []:
        clear_reference(tvdb_id, code)
        have.pop(code, None)
        misses.pop(code, None)

    wiki = (opts.get("fandom_wiki") or "").strip()
    pattern = opts.get("fandom_page_pattern") or "{title}/Transcript"
    overrides = opts.get("fandom_title_overrides") or {}
    page_map = opts.get("fandom_page_map") or {}

    wanted = [e for e in episodes if not (e["season"] == 0 and not opts.get("include_specials"))]
    todo = [e for e in wanted if ep_code(e["season"], e["episode"]) not in have
            and ep_code(e["season"], e["episode"]) not in misses]
    # With a wiki configured, replace OpenSubtitles references with transcripts:
    # those are looked up by title, so they can't be numbered differently.
    upgrades = [e for e in wanted if wiki
                and (have.get(ep_code(e["season"], e["episode"])) or {}).get("source") == "opensubtitles"
                and not have[ep_code(e["season"], e["episode"])].get("fandom_tried")]
    ctx.log(f"{len(have)} episodes already have references; {len(todo)} to fetch"
            + (f"; {len(upgrades)} OpenSubtitles references to replace with wiki transcripts"
               if upgrades else "") + ".")
    if not todo and not upgrades:
        return {"fetched": 0, "missing": len(misses)}

    os_ = OpenSubtitles()
    fetched = upgraded = deferred = 0
    quota_hit = None
    work = [(e, False) for e in todo] + [(e, True) for e in upgrades]
    try:
        if os_.configured and todo:
            try:
                os_.login()
                if os_.remaining is not None:
                    ctx.log(f"OpenSubtitles: {os_.remaining} downloads remaining today.")
            except OpenSubtitlesAuthError as e:
                quota_hit = str(e)
                ctx.log(f"{e} — skipping OpenSubtitles for this fetch. Check Settings → OpenSubtitles.")
        elif not os_.configured and not wiki:
            ctx.log("No OpenSubtitles API key and no Fandom wiki set — nothing to fetch from.")
            return {"fetched": 0, "missing": len(todo)}

        for i, (ep, is_upgrade) in enumerate(work):
            ctx.check_cancel()
            code = ep_code(ep["season"], ep["episode"])
            ctx.progress(i / len(work), f"References: {code} {ep['title']}")
            ref, note, fandom_status, stub_note = None, "", None, ""
            # Fandom first when configured: it costs no download quota.
            if wiki:
                page = overrides.get(code) or page_map.get(code) or pattern.format(
                    title=ep["title"], season=ep["season"], episode=ep["episode"])
                text, fandom_status = fandom_page(wiki, page)
                if text:
                    good, why = transcript_quality(text)
                    if good:
                        ref, note = {"source": "fandom", "text": text, "page": page}, f"fandom {page}"
                    else:
                        fandom_status, stub_note = "missing", why
                        note = why
                        ctx.log(f"{code} · {page}: {why} — trying other sources")
                        cached = _read_cached(tvdb_id, code)
                        if not is_upgrade and cached and cached.get("source") == "fandom":
                            (refs_dir(tvdb_id) / f"{code}.json").unlink(missing_ok=True)
                elif fandom_status == "error":
                    note = f"wiki unreachable for {page} — will retry next fetch"

            if is_upgrade:
                if ref:
                    _save(tvdb_id, code, ref)
                    upgraded += 1
                    ctx.log(f"{code} ↑ replaced OpenSubtitles reference with {note}")
                elif fandom_status == "error":
                    deferred += 1
                    ctx.log(f"{code} … {note}; keeping the OpenSubtitles reference for now")
                elif fandom_status == "missing":
                    cur = _read_cached(tvdb_id, code)
                    if cur:
                        cur["fandom_tried"] = True
                        _save(tvdb_id, code, cur)
                continue

            if ref is None and fandom_status == "error":
                # Don't fall back to OpenSubtitles just because the wiki hiccuped.
                deferred += 1
                ctx.log(f"{code} … {note}")
                continue
            os_skipped = os_.configured and quota_hit is not None
            if ref is None and os_.configured and quota_hit is None:
                try:
                    ref, note = fetch_opensubs(os_, series, ep, ctx)
                except OpenSubtitlesAuthError as e:
                    quota_hit = str(e)
                    os_skipped = True
                    note = ""
                    ctx.log(f"{e} — skipping OpenSubtitles for the rest of this fetch. "
                            "Check the API key under Settings → OpenSubtitles.")
                except QuotaExhausted as e:
                    quota_hit = str(e)
                    os_skipped = True
                    ctx.log(f"OpenSubtitles download quota reached: {e}")
                    ctx.log("Run 'Fetch references' again after the quota resets; "
                            "already-downloaded episodes are cached.")
                except Exception as e:  # noqa: BLE001
                    note = f"error: {e}"
            if ref is None and stub_note and not note.startswith("error"):
                note = f"{stub_note}; {note or 'no other source'}" if note != stub_note else stub_note
            if ref:
                if ref.get("source") == "opensubtitles" and fandom_status == "missing":
                    ref["fandom_tried"] = True
                _save(tvdb_id, code, ref)
                fetched += 1
                warn = ""
                if ref.get("numbering_conflict"):
                    warn = (f"  ⚠ {describe_conflict(ref, code, ep['title'])}; checked against "
                            "other references at the end of this fetch — until then "
                            "renames relying on it won't be pre-ticked")
                ctx.log(f"{code} ✓ {note}{warn}")
            elif not os_skipped and not note.startswith("error"):
                # Remember real misses so re-runs don't burn quota on them.
                misses[code] = note or "not found"
                ctx.log(f"{code} ✗ {note or 'not found'}")
            elif note:
                ctx.log(f"{code} ✗ {note}")
        _save_misses(tvdb_id, misses)
    finally:
        os_.close()
    if deferred:
        ctx.log(f"{deferred} episodes skipped because the wiki didn't respond; "
                "run Fetch references again later.")
    return {"fetched": fetched, "upgraded": upgraded, "deferred": deferred,
            "missing": len(misses), "quota_hit": quota_hit,
            "remaining_downloads": os_.remaining}
