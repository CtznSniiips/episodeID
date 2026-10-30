"""Re-download damaged files through Sonarr — choosing releases by episode title.

Sonarr's own search asks indexers by season and episode number, and for shows whose
releases are numbered differently from TVDB that brings back the wrong episodes. So
EpisodeID runs Sonarr's interactive search for the season (every release, nothing
grabbed), picks the releases whose names carry the damaged file's episode titles,
and has Sonarr grab those — telling Sonarr (v4) which episodes they hold, so it
imports and names them correctly whatever numbers the release uses.

The damaged file is moved to the backup folder (`damaged/`) once its replacement is
grabbed, so Sonarr sees the episode as missing and imports the new copy instead of
refusing it as "not an upgrade". SABnzbd/Sonarr do the downloading, importing and
naming as usual; EpisodeID never touches the download folder. When the new file is
imported, the import webhook scans it (and checks it decodes cleanly).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import logging

from . import db, sonarr
from .config import CONFIG_DIR
from .jobs import handler
from .references import _CODE_IN_NAME, _name_words

log_ = logging.getLogger("episodeid.redownload")
SOFT_REJECTIONS = re.compile(r"(?i)cutoff|upgrade|existing file|already|in queue|not wanted in")
MAX_CANDIDATES = 8


def _code(s: int, e: int) -> str:
    return f"S{int(s):02d}E{int(e):02d}"


def titles_in_release(name: str, episodes: list[dict], series_name: str = "") -> list[str]:
    """Episodes whose TVDB titles a release name carries, in the order they appear
    ("Show.S02E04E05.The.Fridge.The.Flower.1080p…" → S02E04, S02E05 if those are the
    titles), whatever numbers the name uses. A title only inside a longer matched title
    doesn't count ("The Pest" in "The Pest Control")."""
    text = re.sub(r"\.(mkv|mp4|avi|nzb)$", "", name or "", flags=re.I)
    m = _CODE_IN_NAME.search(text)
    after = text[m.end():] if m else text
    w = _name_words(after)
    series = _name_words(series_name).strip()
    if series and not m:
        w = w.replace(" " + series + " ", " ")
    found = []
    for e in episodes:
        if e["season"] == 0:
            continue
        t = _name_words(re.sub(r"\(\d+\)\s*$", "", e.get("title") or "")).strip()
        if len(t) < 4:
            continue
        i = w.find(" " + t + " ")
        if i >= 0:
            found.append((i, t, _code(e["season"], e["episode"])))
    keep = [x for x in found if not any(x[1] != y[1] and (" " + x[1] + " ") in (" " + y[1] + " ")
                                        and w.count(" " + x[1] + " ") == 1 for y in found)]
    return [c for _i, _t, c in sorted(keep)]


def _rejections(r: dict) -> list[str]:
    out = []
    for x in r.get("rejections") or []:
        out.append(x.get("reason") or x.get("message") or str(x) if isinstance(x, dict) else str(x))
    return out


def _usual_quality(series: dict) -> str | None:
    """The quality most of the series' files have (for missing episodes: match the rest)."""
    from collections import Counter
    scan = db.latest_scan(series["id"])
    qs = Counter(q for f in (scan or {}).get("results", {}).get("files") or [] if (q := _quality_of(f["rel"])))
    return qs.most_common(1)[0][0] if qs else None


def _quality_of(rel: str) -> str | None:
    m = re.search(r"(?i)\b((?:WEB(?:DL|Rip)|HDTV|Bluray|DVD)-\d{3,4}p|SDTV|DVD)\b", Path(rel).name)
    return m.group(1).lower() if m else None


def candidates(want: list[str], releases: list[dict], episodes: list[dict], series_name: str,
               current_quality: str | None, fill: set | None = None) -> list[dict]:
    """Releases that could replace a file holding the episodes `want` (or fetch missing
    episodes `want`), best first. fill: other missing episodes — a release that also holds
    one of those fills that gap too ("fills"), rather than holding an unwanted extra."""
    want_set = set(want)
    fill = set(fill or ()) - want_set
    out = []
    for r in releases:
        title = r.get("title") or ""
        named = titles_in_release(title, episodes, series_name)
        mapped = [_code(x.get("seasonNumber", 0), x.get("episodeNumber", 0))
                  for x in r.get("mappedEpisodeInfo") or [] if x.get("episodeNumber")]
        if named:
            covers = [c for c in named if c in want_set]
            if not covers:
                continue
            kind, extra = "title", [c for c in named if c not in want_set and c not in fill]
            fills = [c for c in named if c in fill]
        elif r.get("fullSeason"):
            kind, covers, extra, fills = "pack", sorted(want_set), [], []
        elif mapped and set(mapped) <= want_set:
            kind, covers, extra, fills = "numbers", [c for c in want if c in mapped], [], []
        else:
            continue
        rej = _rejections(r)
        hard = [x for x in rej if not SOFT_REJECTIONS.search(x)]
        q = ((r.get("quality") or {}).get("quality") or {}).get("name") or ""
        out.append({
            "guid": r.get("guid"), "indexerId": r.get("indexerId"), "indexer": r.get("indexer"),
            "title": title, "size": r.get("size") or 0, "quality": q, "protocol": r.get("protocol"),
            "age": r.get("age"), "kind": kind, "covers": covers, "extra": extra, "fills": fills, "rejections": rej,
            "mapped": mapped,  # the episodes Sonarr would take it for, going by its numbers
            "hard_rejections": hard,
            "release": {k: r.get(k) for k in ("guid", "indexerId", "quality", "languages")},
            "rank": ({"title": 2, "numbers": 1, "pack": 0}[kind], len(covers), -len(extra), not hard, q.lower() == (current_quality or ""),
                     r.get("protocol") == "usenet", -(r.get("age") or 0)),
        })
    out.sort(key=lambda c: c["rank"], reverse=True)
    for c in out:
        c.pop("rank")
    return out


def preselect(want: list[str], cands: list[dict]) -> list[str]:
    """guids to tick: the best titled release(s) covering every wanted episode."""
    left, picked = set(want), []
    for c in cands:
        if c["kind"] != "title" or c["hard_rejections"]:
            continue
        if left & set(c["covers"]):
            picked.append(c["guid"])
            left -= set(c["covers"])
        if not left:
            break
    return picked


# ------------------------------------------------------------------ state

def _state_file(sid: int) -> Path:
    return CONFIG_DIR / "redownload" / f"{sid}.json"


def load_state(sid: int) -> dict:
    try:
        return json.loads(_state_file(sid).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(sid: int, st: dict) -> None:
    f = _state_file(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st))


MISSING = "missing:"   # target key prefix for an episode with no file at all


def missing_episodes(series: dict) -> list[dict]:
    """Aired episodes no file has, going by the last scan (names and what was found inside)."""
    from .pipeline import _aired
    scan = db.latest_scan(series["id"])
    if not scan:
        return []
    have = set()
    for f in scan["results"].get("files") or []:
        have.update(f.get("expected") or [])
        have.update(sg["code"] for sg in f.get("segments") or []
                    if sg.get("code") and sg.get("confidence") in ("high", "manual"))
    specials = bool((series.get("options") or {}).get("include_specials"))
    return [{"code": _code(e["season"], e["episode"]), "title": e.get("title")}
            for e in series["episodes"] if (e["season"] > 0 or specials) and _aired(e)
            and _code(e["season"], e["episode"]) not in have]


def _contents(series: dict, rel: str) -> list[str]:
    """The episodes a file holds: what the scan found (confidently), else its name."""
    scan = db.latest_scan(series["id"])
    for f in (scan or {}).get("results", {}).get("files") or []:
        if f["rel"] == rel:
            sure = [sg["code"] for sg in f.get("segments") or []
                    if sg.get("confidence") in ("high", "manual") and sg.get("code")]
            return list(dict.fromkeys(sure)) or list(f.get("expected") or [])
    from .matcher import parse_filename_episodes
    return parse_filename_episodes(Path(rel).name)


@handler("find_releases")
def job_find_releases(ctx, params):
    series = db.get_series(ctx.series_id)
    rels = list(params.get("files") or [])
    missing = [c for c in params.get("missing") or [] if re.fullmatch(r"S\d{2}E\d{2,3}", c)]
    if not rels and not missing:
        raise RuntimeError("Nothing chosen.")
    ss = sonarr.find_series(series["tvdb_id"])
    if not ss:
        raise RuntimeError("Sonarr doesn't have this series (matched by TVDB id).")
    eps = series["episodes"]
    titles = {_code(e["season"], e["episode"]): e.get("title") for e in eps}
    want = {rel: _contents(series, rel) for rel in rels}
    want.update({MISSING + c: [c] for c in missing})
    seasons = sorted({int(c[1:3]) for cs in want.values() for c in cs})
    releases: dict[int, list] = {}
    for i, sn in enumerate(seasons):
        ctx.check_cancel()
        ctx.progress(i / max(1, len(seasons)), f"Searching for season {sn} releases (Sonarr)")
        ctx.log(f"Season {sn}: asking Sonarr to search the indexers (interactive search — nothing is grabbed)…")
        try:
            releases[sn] = sonarr.season_releases(ss["id"], sn)
        except sonarr.SonarrError as e:
            ctx.log(f"Season {sn}: {e}")
            releases[sn] = []
        ctx.log(f"Season {sn}: {len(releases[sn])} releases.")
    files = {}
    for rel, codes in want.items():
        pool = [r for sn in {int(c[1:3]) for c in codes} for r in releases.get(sn, [])]
        cands = candidates(codes, pool, eps, series["name"], _quality_of(rel) if not rel.startswith(MISSING) else
                           _usual_quality(series), fill=set(missing))
        pick = preselect(codes, cands)
        titled = sum(1 for c in cands if c["kind"] == "title")
        files[rel] = {"want": [{"code": c, "title": titles.get(c)} for c in codes], "missing": rel.startswith(MISSING),
                      "candidates": cands[:MAX_CANDIDATES], "found": len(cands), "titled": titled,
                      "preselect": pick}
        ctx.log(f"  {rel}: {titled} release(s) naming "
                + (" / ".join(f"'{titles.get(c)}'" for c in codes) or "its episodes")
                + (f"; best: {cands[0]['title']}" if cands else " — none found"))
    st = load_state(series["id"])
    st.update(searched_at=db.now(), sonarr_series_id=ss["id"], files=files)
    _save_state(series["id"], st)
    ctx.log("Done. Open Re-download… (Plan tab) to choose and grab.")
    return {"files": len(files)}


MAX_ATTEMPTS = 5   # releases tried per file when retrying automatically


def _grab_one(c: dict, sid: int, sonarr_eps: dict) -> dict:
    """Grab one candidate in Sonarr, telling it which episodes it holds. Raises SonarrError."""
    meant = c["covers"] + c["extra"] + (c.get("fills") or [])
    ids = [sonarr_eps[x] for x in meant if x in sonarr_eps] if c["kind"] == "title" else []
    # Grabbed as it numbers itself, Sonarr would import it as c["mapped"]: that's only
    # safe when those are the episodes meant (numbers-only and pack picks are chosen as such).
    plain_ok = c["kind"] != "title" or sorted(c.get("mapped") or []) == sorted(meant)
    return sonarr.grab(c["release"], sid, ids or None, plain_ok=plain_ok)


def _keep(c: dict) -> dict:
    return {k: c.get(k) for k in ("guid", "title", "kind", "covers", "extra", "fills", "mapped", "release",
                                  "hard_rejections", "quality", "size")}


def grab(series: dict, picks: list[dict], ctx_log=None, auto_retry: bool = True) -> dict:
    """picks: [{rel, guid}]. Grab each release in Sonarr (telling it which episodes it
    holds), then move each replaced file to the backup folder and have Sonarr rescan.
    auto_retry: if a download fails, try the file's next release whose name carries the
    title (never numbers-only ones), up to MAX_ATTEMPTS releases in all."""
    from .executor import backup_dir, mark_backup_folder
    log = ctx_log or (lambda m: None)
    st = load_state(series["id"])
    files = st.get("files") or {}
    sid = st.get("sonarr_series_id")
    sonarr_eps = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in sonarr.episodes(sid)}
    root = Path(series["path"]).resolve()
    done, failed, moved = [], [], []
    grabbed_rels = set()
    sent: dict[str, dict] = {}   # guid → result: a release picked for two missing episodes is grabbed once
    for p in picks:
        f = files.get(p["rel"]) or {}
        c = next((x for x in f.get("candidates") or [] if x["guid"] == p["guid"]), None)
        if not c:
            failed.append({**p, "error": "not in the last search — search again"})
            continue
        if p["guid"] in sent:
            r = sent[p["guid"]]
            done.append({**p, "title": c["title"], "override": r["override"],
                         "episodes": c["covers"] + c["extra"] + (c.get("fills") or [])})
            continue
        try:
            r = _grab_one(c, sid, sonarr_eps)
            sent[p["guid"]] = r
        except sonarr.SonarrError as e:
            failed.append({**p, "title": c["title"], "error": str(e)})
            log(f"✗ {c['title']}: {e}")
            continue
        meant = c["covers"] + c["extra"] + (c.get("fills") or [])
        done.append({**p, "title": c["title"], "override": r["override"], "episodes": meant})
        if not p["rel"].startswith(MISSING):
            grabbed_rels.add(p["rel"])
        log(f"✓ grabbed {c['title']}" + (f" as {', '.join(meant)}" if r["override"] else
                                          " (as Sonarr names it)"))
    for rel in sorted(grabbed_rels):
        src = (root / rel).resolve()
        if root not in src.parents or not src.is_file():
            continue
        dst = backup_dir(series) / "damaged" / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(rel)
            log(f"moved {rel} → backup folder (damaged/)")
        except OSError as e:
            failed.append({"rel": rel, "error": f"grabbed, but couldn't move the damaged file: {e}"})
    if moved:
        try:
            mark_backup_folder(series["path"])
        except Exception:  # noqa: BLE001
            pass
        try:
            sonarr.rescan(sid)
        except sonarr.SonarrError as e:
            log(f"Sonarr rescan: {e}")
    req = st.setdefault("requested", {})
    now = db.now()
    for d in done:
        r = req.get(d["rel"])
        if not r or r.get("status") != "waiting":
            f = files.get(d["rel"]) or {}
            r = req[d["rel"]] = {"status": "waiting", "grabs": [], "moved": False, "auto_retry": auto_retry,
                                 "want": [w["code"] for w in f.get("want") or []],
                                 # the titled releases found, kept for retrying
                                 "candidates": [_keep(c) for c in f.get("candidates") or []
                                                if c["kind"] == "title" and not c["hard_rejections"]]}
        r["auto_retry"] = auto_retry  # the latest choice for this file
        ids = [sonarr_eps[x] for x in d["episodes"] if x in sonarr_eps]
        r["grabs"].append({"title": d["title"], "guid": d["guid"], "episodes": d["episodes"], "episode_ids": ids,
                           "at": now, "status": "waiting"})
    for d in done:
        if d["rel"].startswith(MISSING):
            files.pop(d["rel"], None)
    for rel in moved:
        files.pop(rel, None)
        if rel in req:
            req[rel]["moved"] = True
            req[rel]["backup"] = str(Path("damaged") / rel)
    _save_state(series["id"], st)
    return {"grabbed": done, "failed": failed, "moved": moved}


# --------------------------------------------------------- following the grabs

GRAB_SEEN_WITHIN = 30 * 60      # no "grabbed" in Sonarr's history after this long: it never happened
STUCK_AFTER = 48 * 3600         # downloading/importing for longer than this: needs a look
_IMPORTED = ("downloadFolderImported", "seriesFolderImported")
_FAILED = ("downloadFailed", "downloadIgnored")


def _age(iso: str) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def follow_up(series: dict, log=None) -> int:
    """Check Sonarr's history for the downloads EpisodeID started: mark each imported or
    failed. When every grab for a file failed (or never happened), put the damaged file
    back where it was — a failed attempt never leaves the episode missing. Returns how
    many grabs changed state."""
    log = log or (lambda m: None)
    st = load_state(series["id"])
    req = st.get("requested") or {}
    waiting = [(rel, g) for rel, r in req.items() if r.get("status") == "waiting"
               for g in r.get("grabs") or [] if g.get("status") == "waiting"]
    if not waiting:
        return 0
    since = min(g["at"] for _rel, g in waiting)
    try:
        hist = sonarr.history_since(since)
    except sonarr.SonarrError as e:
        log(f"Couldn't read Sonarr's history: {e}")
        return 0
    changed = 0
    for rel, g in waiting:
        ids = set(g.get("episode_ids") or [])
        if not g.get("download_id"):
            hit = next((h for h in hist if h.get("eventType") == "grabbed" and h.get("episodeId") in ids
                        and _norm(h.get("sourceTitle")) == _norm(g["title"])), None)
            if hit:
                g["download_id"] = hit.get("downloadId")
            elif _age(g["at"]) > GRAB_SEEN_WITHIN:
                g.update(status="failed", reason="Sonarr has no record of the grab")
                changed += 1
                continue
        did = g.get("download_id")
        if did:
            evs = [h for h in hist if h.get("downloadId") == did and h.get("eventType") in _IMPORTED + _FAILED]
            if any(h["eventType"] in _IMPORTED for h in evs):
                g.update(status="imported", done_at=db.now())
                changed += 1
                continue
            bad = next((h for h in evs if h["eventType"] in _FAILED), None)
            if bad:
                msg = (bad.get("data") or {}).get("message") or bad["eventType"]
                g.update(status="failed", reason=f"{'download failed' if bad['eventType'] == 'downloadFailed' else 'ignored in Sonarr'}: {msg}"[:300])
                changed += 1
                continue
        if _age(g["at"]) > STUCK_AFTER and g.get("status") == "waiting":
            g.update(status="stuck", reason="not imported after 2 days — check Sonarr's Activity queue")
            changed += 1
    for rel, r in req.items():
        if r.get("status") != "waiting":
            continue
        states = [g.get("status") for g in r.get("grabs") or []]
        if "waiting" in states:
            continue
        if "imported" in states:
            r["status"] = "imported" if all(s == "imported" for s in states) else "partly imported"
            log(f"{rel}: replacement imported" if r["status"] == "imported" else
                f"{rel}: only some replacements imported — the damaged file stays in the backup folder")
        elif "stuck" in states:
            r["status"] = "stuck"
            log(f"{rel}: replacement not imported after 2 days — check Sonarr's Activity queue")
        else:
            nxt = _retry(r, st.get("sonarr_series_id"), log, rel) if r.get("auto_retry") else None
            if nxt:
                continue  # trying the next release: the damaged file stays in the backup folder
            r["status"] = "failed"
            reasons = "; ".join(g.get("reason") or "failed" for g in r.get("grabs") or [])
            log(f"{rel}: re-download failed ({reasons})")
            if r.get("moved"):
                ok, why = _put_back(series, rel, r, st.get("sonarr_series_id"))
                r["restored"] = ok
                r["restore_note"] = why
                log(f"{rel}: {why}")
    _save_state(series["id"], st)
    return changed


def _retry(r: dict, sid: int | None, log, rel: str) -> dict | None:
    """Every grab for this file failed: grab its next release whose name carries the
    episode title(s) — covering what the failed ones were for, holding no other episode —
    unless MAX_ATTEMPTS have been tried. Returns the new grab, or None."""
    grabs = r.get("grabs") or []
    if len(grabs) >= MAX_ATTEMPTS:
        log(f"{rel}: {len(grabs)} releases tried — no more automatic retries")
        return None
    need = {c for g in grabs for c in g.get("episodes") or []} or set(r.get("want") or [])
    tried = {g.get("guid") for g in grabs}
    try:
        sonarr_eps = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in sonarr.episodes(sid)}
    except sonarr.SonarrError as e:
        log(f"{rel}: couldn't retry — {e}")
        return None
    for c in r.get("candidates") or []:
        if c["guid"] in tried or c["kind"] != "title" or c.get("extra") or not need <= set(c["covers"]):
            continue
        tried.add(c["guid"])
        try:
            _grab_one(c, sid, sonarr_eps)
        except sonarr.SonarrError as e:
            grabs.append({"title": c["title"], "guid": c["guid"], "episodes": c["covers"], "episode_ids": [],
                          "at": db.now(), "status": "failed", "reason": f"not grabbed: {e}"[:300], "retry": True})
            if len(grabs) >= MAX_ATTEMPTS:
                return None
            continue
        g = {"title": c["title"], "guid": c["guid"], "episodes": c["covers"],
             "episode_ids": [sonarr_eps[x] for x in c["covers"] if x in sonarr_eps],
             "at": db.now(), "status": "waiting", "retry": True}
        grabs.append(g)
        log(f"{rel}: download failed — trying the next release with the title: {c['title']}")
        return g
    log(f"{rel}: no other release with the title left to try")
    return None


def _put_back(series: dict, rel: str, r: dict, sonarr_series_id: int | None) -> tuple[bool, str]:
    """Move the damaged file back from the backup folder — unless something has taken its
    place (a file at that path, or Sonarr having a file for its episodes again)."""
    from .executor import backup_dir
    root = Path(series["path"]).resolve()
    src = backup_dir(series) / (r.get("backup") or str(Path("damaged") / rel))
    dst = (root / rel).resolve()
    if root not in dst.parents:
        return False, "not put back: path outside the series"
    if not src.is_file():
        return False, "not put back: it's no longer in the backup folder"
    if dst.exists():
        return False, "not put back: another file is at that path now"
    ids = {i for g in r.get("grabs") or [] for i in g.get("episode_ids") or []}
    try:
        if ids and any(e.get("hasFile") for e in sonarr.episodes(sonarr_series_id) if e.get("id") in ids):
            return False, "not put back: Sonarr has a file for that episode again (left in the backup folder)"
    except sonarr.SonarrError:
        pass
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as e:
        return False, f"couldn't put it back: {e}"
    try:
        sonarr.rescan(sonarr_series_id)
    except sonarr.SonarrError:
        pass
    return True, "the damaged file was put back, so the episode isn't left missing"


def put_back(series: dict, rel: str) -> dict:
    """Put a damaged file back by hand (a re-download that's stuck, or not wanted)."""
    st = load_state(series["id"])
    r = (st.get("requested") or {}).get(rel)
    if not r or not r.get("moved"):
        return {"ok": False, "note": "nothing to put back"}
    ok, why = _put_back(series, rel, r, st.get("sonarr_series_id"))
    if ok:
        r.update(status="cancelled", restored=True, restore_note=why)
        _save_state(series["id"], st)
    return {"ok": ok, "note": why}


def follow_up_all() -> None:
    for s in db.list_series():
        st = load_state(s["id"])
        if any(r.get("status") == "waiting" for r in (st.get("requested") or {}).values()):
            try:
                follow_up(db.get_series(s["id"]), lambda m, name=s["name"]: log_.info("%s: %s", name, m))
            except Exception:  # noqa: BLE001
                log_.exception("following re-downloads for %s", s["name"])


def start_follower(interval: float = 300) -> None:
    """Every few minutes, check on re-downloads in progress (only when there are any)."""
    import threading
    import time

    def loop():
        while True:
            time.sleep(interval)
            if sonarr.configured():
                follow_up_all()

    threading.Thread(target=loop, name="episodeid-redownloads", daemon=True).start()


def sonarr_redownload_settings() -> dict | None:
    """Sonarr's Failed Download Handling: does it search again by itself when a download
    fails? (It searches by number — the wrong episodes, for shows numbered differently.)"""
    try:
        c = sonarr._get("config/downloadclient")
    except sonarr.SonarrError:
        return None
    return {"failed": bool(c.get("autoRedownloadFailed")),
            "interactive": bool(c.get("autoRedownloadFailedFromInteractiveSearch"))}


def summary(series: dict) -> dict:
    if sonarr.configured():
        try:
            follow_up(series)
        except Exception:  # noqa: BLE001
            log_.exception("following re-downloads")
    st = load_state(series["id"])
    return {"searched_at": st.get("searched_at"), "files": st.get("files") or {},
            "requested": st.get("requested") or {}, "missing": missing_episodes(series),
            "sonarr_redownload": sonarr_redownload_settings() if sonarr.configured() else None}
