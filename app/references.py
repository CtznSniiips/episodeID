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


def load_refs(tvdb_id: int) -> dict[str, dict]:
    """code -> {"source", "text", ...}. Manual files override cached downloads."""
    out: dict[str, dict] = {}
    d = refs_dir(tvdb_id)
    if d.exists():
        for f in d.glob("S*E*.json"):
            try:
                data = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            if data.get("text"):
                out[f.stem] = data
    md = manual_dir(tvdb_id)
    if md.exists():
        for f in md.iterdir():
            m = re.match(r"(?i)s(\d+)e(\d+)", f.name)
            if not m or f.suffix.lower() not in SUB_EXTS + (".txt",):
                continue
            raw = read_text_file(f)
            if f.suffix.lower() == ".txt":
                text = re.sub(r"\s+", " ", raw)
            else:
                text = " ".join(c.text for c in parse_subtitle_text(raw, f.suffix.lower()))
            if text.strip():
                out[ep_code(int(m[1]), int(m[2]))] = {"source": f"manual:{f.name}", "text": text}
    return out


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


class OpenSubtitles:
    def __init__(self):
        s = get_settings()
        self.api_key = s["opensubtitles_api_key"]
        self.username = s["opensubtitles_username"]
        self.password = s["opensubtitles_password"]
        self.lang = s["subtitle_language"] or "en"
        self.token = None
        self.base = OS_BASE
        self.remaining = None
        self.client = httpx.Client(timeout=30, follow_redirects=True, headers={
            "Api-Key": self.api_key, "User-Agent": UA, "Accept": "application/json"})

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def close(self):
        self.client.close()

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(4):
            r = self.client.request(method, f"{self.base}{path}", **kw)
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
        if r.status_code != 200:
            raise RuntimeError(f"OpenSubtitles login failed ({r.status_code}): {r.text[:200]}")
        j = r.json()
        self.token = j.get("token")
        if j.get("base_url"):
            self.base = f"https://{j['base_url']}/api/v1"
        self.remaining = (j.get("user") or {}).get("remaining_downloads")

    def search(self, **params) -> list[dict]:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params.setdefault("languages", self.lang)
        params = dict(sorted((k, str(v).lower() if k != "query" else v)
                             for k, v in params.items()))
        r = self._req("GET", "/subtitles", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"OpenSubtitles search failed ({r.status_code}): {r.text[:200]}")
        return r.json().get("data") or []

    def download(self, file_id: int) -> str:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = self._req("POST", "/download", json={"file_id": file_id, "sub_format": "srt"},
                      headers=headers)
        if r.status_code in (406, 429):
            try:
                msg = r.json().get("message")
            except ValueError:
                msg = None
            raise QuotaExhausted(msg or r.text[:200])
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
    """Returns (ref, note). Raises QuotaExhausted."""
    title = ep["title"]
    imdb_ep = ep.get("imdb_id")
    if imdb_ep is None and ep.get("tvdb_episode_id"):
        try:
            imdb_ep = tvdb.episode_imdb_id(ep["tvdb_episode_id"]) or ""
        except Exception:  # noqa: BLE001
            imdb_ep = ""
        ep["imdb_id"] = imdb_ep
    pick = None
    how = ""
    if imdb_ep:
        res = os_.search(imdb_id=imdb_ep.lstrip("t"))
        p = _pick(res, title, ep["season"], ep["episode"], check_numbers=False)
        if p:
            pick, how = p, "episode imdb"
    if pick is None and series.get("imdb_id"):
        res = os_.search(parent_imdb_id=series["imdb_id"].lstrip("t"),
                         season_number=ep["season"], episode_number=ep["episode"])
        p = _pick(res, title, ep["season"], ep["episode"], check_numbers=True)
        if p and p[0]:
            pick, how = p, "series imdb + number"
        else:
            # Numbering may differ from TVDB; look the episode up by title instead.
            res = os_.search(parent_imdb_id=series["imdb_id"].lstrip("t"), query=title)
            p = _pick(res, title, ep["season"], ep["episode"], check_numbers=False)
            if p and p[0]:
                pick, how = p, "series imdb + title"
    if pick is None:
        return None, "no subtitle found"
    title_ok, _, _, r = pick
    a = r["attributes"]
    fd = a.get("feature_details") or {}
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
        "os_number": f"S{fd.get('season_number') or 0:02d}E{fd.get('episode_number') or 0:02d}",
        "title_verified": bool(title_ok),
        "subtitle_id": r.get("id"),
    }, how


# ------------------------------------------------------------------ Fandom

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|table class=\"navbox).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</dd>|</li>", "\n", h)
    return re.sub(r"[ \t]+", " ", html.unescape(_HTML_TAG.sub(" ", h))).strip()


def fetch_fandom(wiki: str, page: str) -> str | None:
    wiki = wiki.strip()
    host = wiki if "." in wiki else f"{wiki}.fandom.com"
    host = re.sub(r"^https?://", "", host).rstrip("/")
    api = f"https://{host}/api.php"
    headers = {"User-Agent": BROWSER_UA}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as c:
        try:
            r = c.get(api, params={"action": "parse", "page": page, "prop": "text",
                                   "format": "json", "redirects": 1})
            if r.status_code == 200 and "parse" in r.json():
                text = _strip_html(r.json()["parse"]["text"]["*"])
                if len(text) > 500:
                    return text
        except (httpx.HTTPError, ValueError):
            pass
        try:
            r = c.get(f"https://{host}/index.php", params={"title": page, "action": "raw"})
            if r.status_code == 200 and len(r.text) > 500:
                t = re.sub(r"\{\{[^}]*\}\}|\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", r.text)
                return re.sub(r"'{2,}|<[^>]+>", "", t)
        except httpx.HTTPError:
            pass
        try:
            r = c.get(f"https://{host}/wiki/{page.replace(' ', '_')}")
            if r.status_code == 200:
                m = re.search(r'(?s)<div class="mw-parser-output">(.*?)<div class="printfooter',
                              r.text)
                if m:
                    text = _strip_html(m.group(1))
                    if len(text) > 500:
                        return text
        except httpx.HTTPError:
            pass
    return None


# ------------------------------------------------------------------ driver

def refresh_references(series: dict, ctx, force_codes: list[str] | None = None,
                       retry_misses: bool = False) -> dict:
    tvdb_id = series["tvdb_id"]
    episodes = series["episodes"]
    opts = series.get("options") or {}
    have = load_refs(tvdb_id)
    misses = {} if retry_misses else _load_misses(tvdb_id)
    for code in force_codes or []:
        clear_reference(tvdb_id, code)
        have.pop(code, None)
        misses.pop(code, None)

    todo = [e for e in episodes if ep_code(e["season"], e["episode"]) not in have
            and ep_code(e["season"], e["episode"]) not in misses
            and not (e["season"] == 0 and not opts.get("include_specials"))]
    ctx.log(f"{len(have)} episodes already have references; {len(todo)} to fetch.")
    if not todo:
        return {"fetched": 0, "missing": len(misses)}

    wiki = (opts.get("fandom_wiki") or "").strip()
    pattern = opts.get("fandom_page_pattern") or "{title}/Transcript"
    overrides = opts.get("fandom_title_overrides") or {}
    os_ = OpenSubtitles()
    fetched = 0
    quota_hit = None
    try:
        if os_.configured:
            os_.login()
            if os_.remaining is not None:
                ctx.log(f"OpenSubtitles: {os_.remaining} downloads remaining today.")
        elif not wiki:
            ctx.log("No OpenSubtitles API key and no Fandom wiki set — nothing to fetch from.")
            return {"fetched": 0, "missing": len(todo)}

        for i, ep in enumerate(todo):
            ctx.check_cancel()
            code = ep_code(ep["season"], ep["episode"])
            ctx.progress(i / len(todo), f"References: {code} {ep['title']}")
            ref, note = None, ""
            os_skipped = os_.configured and quota_hit is not None
            # Fandom first when configured: it costs no download quota.
            if wiki:
                page = overrides.get(code) or pattern.format(title=ep["title"],
                                                             season=ep["season"],
                                                             episode=ep["episode"])
                text = fetch_fandom(wiki, page)
                if text:
                    ref, note = {"source": "fandom", "text": text, "page": page}, f"fandom {page}"
            if ref is None and os_.configured and quota_hit is None:
                try:
                    ref, note = fetch_opensubs(os_, series, ep, ctx)
                except QuotaExhausted as e:
                    quota_hit = str(e)
                    os_skipped = True
                    ctx.log(f"OpenSubtitles download quota reached: {e}")
                    ctx.log("Run 'Fetch references' again after the quota resets; "
                            "already-downloaded episodes are cached.")
                except Exception as e:  # noqa: BLE001
                    note = f"error: {e}"
            if ref:
                _save(tvdb_id, code, ref)
                fetched += 1
                warn = "" if ref.get("title_verified", True) else \
                    f"  ⚠ title not verified (OpenSubtitles says '{ref.get('os_title')}')"
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
    return {"fetched": fetched, "missing": len(misses), "quota_hit": quota_hit,
            "remaining_downloads": os_.remaining}
