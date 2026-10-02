"""EpisodeID web server."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, jobs, llm, titlecard, tvdb
from . import pipeline  # noqa: F401  (registers job handlers)
from .config import (CACHE_DIR, MEDIA_ROOT, env_locked_keys, get_settings, public_settings,
                     safe_media_path, save_settings)
from .references import (OpenSubtitles, _load_misses, clear_reference, ep_code, load_refs,
                         manual_dir)
from .media_text import VIDEO_EXTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="EpisodeID", version=__version__)


@app.on_event("startup")
def _startup():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db.init()
    jobs.start()


def _404(what="Not found"):
    raise HTTPException(404, what)


# ------------------------------------------------------------------ settings

@app.get("/api/status")
def status():
    s = get_settings()
    return {"version": __version__, "media_root": str(MEDIA_ROOT),
            "tvdb": bool(s["tvdb_api_key"]), "opensubtitles": bool(s["opensubtitles_api_key"]),
            "llm": llm.available(), "whisper": s["whisper_enabled"],
            "titlecards": bool(s["titlecard_enabled"] and titlecard.available()),
            "sonarr": bool(s["sonarr_url"] and s["sonarr_api_key"])}


@app.get("/api/settings")
def read_settings():
    return {"settings": public_settings(), "locked": env_locked_keys()}


@app.put("/api/settings")
def write_settings(body: dict):
    save_settings(body)
    return read_settings()


@app.post("/api/test/{service}")
def test_service(service: str):
    s = get_settings()
    try:
        if service == "tvdb":
            tvdb._token["key"] = None
            r = tvdb.search_series("The Simpsons")
            return {"ok": True, "message": f"Connected — search returned {len(r)} results."}
        if service == "opensubtitles":
            os_ = OpenSubtitles()
            if not os_.configured:
                return {"ok": False, "message": "No API key set."}
            try:
                os_.login()
                if os_.token:
                    return {"ok": True, "message": f"Logged in — {os_.remaining} downloads "
                                                   "remaining today."}
                os_.search(query="test")
                return {"ok": True, "message": "API key works. Add username/password to "
                                               "download subtitles."}
            finally:
                os_.close()
        if service == "llm":
            if not (s["llm_base_url"] and s["llm_model"]):
                return {"ok": False, "message": "Set a base URL and model first."}
            return {"ok": True, "message": f"Model replied: {llm.test_connection()!r}"}
        if service == "sonarr":
            r = httpx.get(f"{s['sonarr_url'].rstrip('/')}/api/v3/system/status",
                          headers={"X-Api-Key": s["sonarr_api_key"]}, timeout=15)
            r.raise_for_status()
            return {"ok": True, "message": f"Sonarr {r.json().get('version')}"}
        if service == "whisper":
            import importlib.util
            ok = importlib.util.find_spec("faster_whisper") is not None
            return {"ok": ok, "message": "faster-whisper installed" if ok else
                    "faster-whisper is not installed in this image"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300]}
    _404("Unknown service")


# -------------------------------------------------------------------- browse

@app.get("/api/browse")
def browse(path: str = ""):
    try:
        p = safe_media_path(path or MEDIA_ROOT)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p.is_dir():
        _404("Not a folder")
    dirs, videos = [], 0
    for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        if c.name.startswith("."):
            continue
        if c.is_dir():
            dirs.append({"name": c.name, "path": str(c.relative_to(MEDIA_ROOT))})
        elif c.suffix.lower() in VIDEO_EXTS:
            videos += 1
    rel = "" if p == MEDIA_ROOT else str(p.relative_to(MEDIA_ROOT))
    parent = None if p == MEDIA_ROOT else str(p.parent.relative_to(MEDIA_ROOT)) \
        if p.parent != MEDIA_ROOT else ""
    return {"path": rel, "parent": parent, "dirs": dirs, "videos": videos}


def guess_query(folder: str) -> tuple[str, int | None]:
    m = re.search(r"[\[{(]tvdb(?:id)?[-= ](\d+)[\]})]", folder, re.I)
    tvdb_id = int(m[1]) if m else None
    q = re.sub(r"[\[{(][^\]})]*[\]})]", " ", folder)
    q = re.sub(r"\s+", " ", q.replace(".", " ").replace("_", " ")).strip()
    return q, tvdb_id


@app.get("/api/tvdb/search")
def tvdb_search(q: str):
    try:
        results = tvdb.search_series(q)
    except tvdb.TVDBError as e:
        raise HTTPException(400, str(e))
    added = {}
    for s in db.list_series():
        added.setdefault(s["tvdb_id"], s["id"])
    for r in results:
        r["added_id"] = added.get(r["tvdb_id"])
    return results


_SEASON_DIR = re.compile(r"(?i)^(season|series|s)\s*\d+$|^specials$")


def _video_count(p: Path, limit: int = 2000) -> int:
    n = 0
    for _root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_episodeid"))]
        n += sum(1 for f in files if Path(f).suffix.lower() in VIDEO_EXTS)
        if n >= limit:
            break
    return n


_folder_cache: dict = {"at": 0.0, "items": []}


def _show_folders() -> list[dict]:
    """Every folder in the library that looks like a show: it holds season folders or
    video files directly. Parent folders (e.g. /media/tv) are searched up to 3 levels.
    Cached for a minute so typing in the search box stays instant."""
    import time
    if time.time() - _folder_cache["at"] < 60:
        return _folder_cache["items"]
    out = []
    stack = [(MEDIA_ROOT, 0)]
    while stack and len(out) < 20000:
        d, depth = stack.pop()
        try:
            entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=True)
                       and not e.name.startswith((".", "_episodeid", "@", "#"))]
        except OSError:
            continue
        for e in entries:
            try:
                kids = list(os.scandir(e.path))
            except OSError:
                continue
            is_show = any(k.is_dir() and _SEASON_DIR.match(k.name.strip()) for k in kids) or \
                any(k.is_file() and Path(k.name).suffix.lower() in VIDEO_EXTS for k in kids)
            if is_show:
                name, tvdb_id = guess_query(e.name)
                year = re.search(r"\((19|20)\d{2}\)", e.name)
                out.append({"path": str(Path(e.path).relative_to(MEDIA_ROOT)), "folder": e.name,
                            "name": re.sub(r"\s*\b(19|20)\d{2}\b\s*$", "", name).strip() or name,
                            "year": year.group(0)[1:-1] if year else "", "tvdb_id": tvdb_id})
            elif depth < 3:
                stack.append((Path(e.path), depth + 1))
    out.sort(key=lambda f: f["folder"].lower())
    _folder_cache.update(at=time.time(), items=out)
    return out


@app.get("/api/folders/search")
def folders_search(q: str = "", limit: int = 40):
    """Library folders matching the typed text (all words, any order), best first."""
    from .references import norm_title
    used = {s["path"]: s["id"] for s in db.list_series()}
    words = norm_title(q).split()
    hits = []
    for f in _show_folders():
        hay = norm_title(f["folder"]) + " " + f["path"].lower()
        if words and not all(w in hay for w in words):
            continue
        n = norm_title(f["name"])
        rank = 0 if n == " ".join(words) else 1 if n.startswith(" ".join(words)) else 2
        hits.append((rank, f["folder"].lower(), f))
    hits.sort(key=lambda h: h[:2])
    out = []
    for _, _, f in hits[:limit]:
        added = used.get(str((MEDIA_ROOT / f["path"]).resolve()))
        out.append({**f, "added_id": added})
    return {"total": len(hits), "results": out}


@app.get("/api/folders/resolve")
def folders_resolve(path: str):
    """Work out which TVDB show a folder is. Uses a {tvdb-123} tag when present,
    otherwise searches TVDB by the folder's name, preferring the folder's year."""
    p = safe_media_path(path)
    if not p.is_dir():
        _404("Folder not found")
    name, tvdb_id = guess_query(p.name)
    year_m = re.search(r"\((19|20)\d{2}\)", p.name)
    year = year_m.group(0)[1:-1] if year_m else ""
    name = re.sub(r"\s*\b(19|20)\d{2}\b\s*$", "", name).strip() or name
    from .references import norm_title
    try:
        results = tvdb.search_series(name)
        if tvdb_id and not any(r["tvdb_id"] == tvdb_id for r in results):
            info = tvdb.series_info(tvdb_id)
            results.insert(0, {**info, "overview": "", "image": None, "network": ""})
    except tvdb.TVDBError as e:
        raise HTTPException(400, str(e))

    def score(r):
        sc = 0
        if tvdb_id and r["tvdb_id"] == tvdb_id:
            sc += 100
        if norm_title(r["name"]) == norm_title(name):
            sc += 20
        if year and str(r.get("year")) == year:
            sc += 10
        return sc

    results.sort(key=score, reverse=True)
    best = results[0] if results else None
    confident = bool(best) and (score(best) >= 100 or (score(best) >= 20 and (
        len(results) == 1 or score(results[1]) < score(best))))
    return {"folder": p.name, "path": str(p.relative_to(MEDIA_ROOT)), "query": name,
            "year": year, "tvdb_id_tag": tvdb_id, "confident": confident,
            "videos": _video_count(p), "results": results[:8]}


@app.get("/api/guess")
def guess(path: str):
    p = safe_media_path(path)
    q, tid = guess_query(p.name)
    return {"query": q, "tvdb_id": tid}


# -------------------------------------------------------------------- series

class NewSeries(BaseModel):
    path: str
    tvdb_id: int


def _series_out(s: dict, full: bool = False) -> dict:
    refs = load_refs(s["tvdb_id"])
    regular = [e for e in s["episodes"] if e["season"] > 0]
    out = {k: v for k, v in s.items() if k != "episodes"}
    out["episode_count"] = len(regular)
    out["reference_count"] = sum(1 for e in regular
                                 if ep_code(e["season"], e["episode"]) in refs)
    out["active_job"] = db.active_job(s["id"])
    scan = db.latest_scan(s["id"])
    out["last_scan"] = {"created": scan["created"], "counts": scan["results"]["counts"]} \
        if scan else None
    if full:
        misses = _load_misses(s["tvdb_id"])
        out["episodes"] = [{
            **e, "code": ep_code(e["season"], e["episode"]),
            "ref": (refs.get(ep_code(e["season"], e["episode"])) or {}).get("source"),
            "ref_note": _ref_note(refs.get(ep_code(e["season"], e["episode"]))),
            "miss": misses.get(ep_code(e["season"], e["episode"])),
        } for e in s["episodes"]]
    return out


def _ref_note(ref: dict | None) -> str:
    if not ref:
        return ""
    if ref.get("source") == "opensubtitles":
        n = f"via {ref.get('via')}"
        if ref.get("numbering_conflict", not ref.get("title_verified", True)):
            n += (f" — ⚠ OpenSubtitles has this as {ref.get('os_number')} "
                  f"'{ref.get('os_title')}'; may be another episode's dialogue")
        return n
    if ref.get("source") == "fandom":
        return ref.get("page", "")
    return ""


@app.get("/api/series")
def series_list():
    return [_series_out(s) for s in db.list_series()]


@app.post("/api/series")
def series_add(body: NewSeries):
    p = safe_media_path(body.path)
    if not p.is_dir():
        raise HTTPException(400, "Folder not found")
    try:
        info = tvdb.series_info(body.tvdb_id)
    except tvdb.TVDBError as e:
        raise HTTPException(400, str(e))
    try:
        sid = db.add_series(body.tvdb_id, info["name"], info["year"], str(p), info["imdb_id"])
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "That folder has already been added")
    # Loads the TVDB episode list, then fetches references straight away.
    jobs.submit("fetch_refs", sid)
    return _series_out(db.get_series(sid))


@app.get("/api/series/{sid}")
def series_get(sid: int):
    s = db.get_series(sid) or _404()
    return _series_out(s, full=True)


@app.patch("/api/series/{sid}/options")
def series_options(sid: int, body: dict):
    s = db.get_series(sid) or _404()
    opts = dict(s["options"] or {})
    for k in ("fandom_wiki", "fandom_page_pattern", "fandom_title_overrides",
              "include_specials", "name_in_files", "title_cards", "title_cards_scope"):
        if k in body:
            opts[k] = body[k]
    if "fandom_wiki" in body:
        from .references import normalize_page, normalize_wiki
        opts["fandom_wiki"] = normalize_wiki(body["fandom_wiki"] or "")
        body["fandom_wiki"] = opts["fandom_wiki"]
        if isinstance(opts.get("fandom_title_overrides"), dict):
            opts["fandom_title_overrides"] = {k.strip().upper(): normalize_page(v)
                                              for k, v in opts["fandom_title_overrides"].items() if v}
        opts["fandom_checked"] = True  # the user decided; don't auto-detect over it
        if (body["fandom_wiki"] or "").strip() != ((s["options"] or {}).get("fandom_wiki") or ""):
            opts.pop("fandom_page_map", None)      # new wiki: its pages get mapped on next fetch
            opts.pop("fandom_discovery", None)
    if body.get("title_cards") == "auto" and (s["options"] or {}).get("title_cards") not in (None, "auto"):
        opts.pop("title_cards_status", None)  # switching back to auto: test again
    if "imdb_id" in body:
        db.update_series(sid, imdb_id=(body["imdb_id"] or None))
    db.update_series(sid, options=opts)
    return _series_out(db.get_series(sid), full=True)


@app.delete("/api/series/{sid}")
def series_delete(sid: int):
    db.get_series(sid) or _404()
    db.delete_series(sid)
    return {"ok": True}


# ---------------------------------------------------------------- references

@app.post("/api/series/{sid}/references/{code}")
async def upload_reference(sid: int, code: str, file: UploadFile = File(...)):
    s = db.get_series(sid) or _404()
    if not re.fullmatch(r"S\d{2,3}E\d{2,4}", code):
        raise HTTPException(400, "Bad episode code")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".srt", ".ass", ".ssa", ".vtt", ".txt"):
        raise HTTPException(400, "Upload a .srt, .ass, .vtt or .txt file")
    d = manual_dir(s["tvdb_id"])
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{code}*"):
        old.unlink()
    (d / f"{code}{ext}").write_bytes(await file.read())
    return {"ok": True}


@app.delete("/api/series/{sid}/references/{code}")
def delete_reference(sid: int, code: str):
    s = db.get_series(sid) or _404()
    clear_reference(s["tvdb_id"], code)
    d = manual_dir(s["tvdb_id"])
    if d.exists():
        for old in d.glob(f"{code}*"):
            old.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------- jobs

class JobReq(BaseModel):
    kind: str
    params: dict = {}


@app.post("/api/series/{sid}/jobs")
def start_job(sid: int, body: JobReq):
    db.get_series(sid) or _404()
    if body.kind not in ("refresh_episodes", "fetch_refs", "scan", "replan"):
        raise HTTPException(400, "Unknown job")
    if db.active_job(sid):
        raise HTTPException(409, "A job is already running for this series")
    return {"job_id": jobs.submit(body.kind, sid, body.params)}


@app.get("/api/jobs")
def jobs_list(series_id: int | None = None):
    return db.list_jobs(series_id)


@app.get("/api/jobs/{jid}")
def job_get(jid: int):
    return db.get_job(jid) or _404()


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: int):
    jobs.cancel(jid)
    return {"ok": True}


# --------------------------------------------------------------- scans/plans

@app.get("/api/series/{sid}/plan")
def plan_get(sid: int):
    db.get_series(sid) or _404()
    plan = db.latest_plan(sid)
    return plan or {"items": [], "id": None}


class Selection(BaseModel):
    selected: dict[int, bool]


@app.patch("/api/plans/{pid}/selection")
def plan_select(pid: int, body: Selection):
    plan = db.get_plan(pid) or _404()
    if plan["status"] != "draft":
        raise HTTPException(400, "Plan already applied")
    for it in plan["items"]:
        if it["id"] in body.selected and it["kind"] in ("rename", "split", "aside"):
            it["selected"] = bool(body.selected[it["id"]])
    db.update_plan(pid, items=plan["items"])
    return {"ok": True}


class Override(BaseModel):
    source: str
    codes: list[str] | None


@app.post("/api/series/{sid}/overrides")
def set_override(sid: int, body: Override):
    s = db.get_series(sid) or _404()
    opts = dict(s["options"] or {})
    ov = dict(opts.get("overrides") or {})
    if body.codes:
        codes = [c.strip().upper() for c in body.codes if c.strip()]
        for c in codes:
            if not re.fullmatch(r"S\d{2,3}E\d{2,4}", c):
                raise HTTPException(400, f"'{c}' is not an episode code like S01E02")
        ov[body.source] = codes
    else:
        ov.pop(body.source, None)
    opts["overrides"] = ov
    db.update_series(sid, options=opts)
    scan = db.latest_scan(sid)
    if scan:
        from .planner import build_plan
        items = build_plan(db.get_series(sid), scan["results"])
        db.add_plan(sid, scan["id"], items)
    return plan_get(sid)


@app.post("/api/plans/{pid}/apply")
def plan_apply(pid: int):
    plan = db.get_plan(pid) or _404()
    if db.active_job(plan["series_id"]):
        raise HTTPException(409, "A job is already running for this series")
    if plan["id"] != (db.latest_plan(plan["series_id"]) or {}).get("id"):
        raise HTTPException(400, "This is not the latest plan — reload.")
    return {"job_id": jobs.submit("apply", plan["series_id"], {"plan_id": pid})}


@app.get("/api/series/{sid}/applies")
def applies(sid: int):
    return db.list_applies(sid)


@app.post("/api/applies/{aid}/undo")
def undo(aid: int):
    ap = db.get_apply(aid) or _404()
    newer = [a for a in db.list_applies(ap["series_id"]) if a["id"] > aid and not a["undone_at"]]
    if newer:
        raise HTTPException(400, "Undo the newer changes first (most recent first).")
    if db.active_job(ap["series_id"]):
        raise HTTPException(409, "A job is already running for this series")
    return {"job_id": jobs.submit("undo", ap["series_id"], {"apply_id": aid})}


@app.get("/api/applies/{aid}")
def apply_get(aid: int):
    return db.get_apply(aid) or _404()


# -------------------------------------------------------------------- static

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
