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

from . import db, sonarr
from .config import CONFIG_DIR
from .jobs import handler
from .references import _CODE_IN_NAME, _name_words

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


def _quality_of(rel: str) -> str | None:
    m = re.search(r"(?i)\b((?:WEB(?:DL|Rip)|HDTV|Bluray|DVD)-\d{3,4}p|SDTV|DVD)\b", Path(rel).name)
    return m.group(1).lower() if m else None


def candidates(want: list[str], releases: list[dict], episodes: list[dict], series_name: str,
               current_quality: str | None) -> list[dict]:
    """Releases that could replace a file holding the episodes `want`, best first."""
    want_set = set(want)
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
            kind, extra = "title", [c for c in named if c not in want_set]
        elif r.get("fullSeason"):
            kind, covers, extra = "pack", sorted(want_set), []
        elif mapped and set(mapped) <= want_set:
            kind, covers, extra = "numbers", [c for c in want if c in mapped], []
        else:
            continue
        rej = _rejections(r)
        hard = [x for x in rej if not SOFT_REJECTIONS.search(x)]
        q = ((r.get("quality") or {}).get("quality") or {}).get("name") or ""
        out.append({
            "guid": r.get("guid"), "indexerId": r.get("indexerId"), "indexer": r.get("indexer"),
            "title": title, "size": r.get("size") or 0, "quality": q, "protocol": r.get("protocol"),
            "age": r.get("age"), "kind": kind, "covers": covers, "extra": extra, "rejections": rej,
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
    if not rels:
        raise RuntimeError("No files chosen.")
    ss = sonarr.find_series(series["tvdb_id"])
    if not ss:
        raise RuntimeError("Sonarr doesn't have this series (matched by TVDB id).")
    eps = series["episodes"]
    titles = {_code(e["season"], e["episode"]): e.get("title") for e in eps}
    want = {rel: _contents(series, rel) for rel in rels}
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
        cands = candidates(codes, pool, eps, series["name"], _quality_of(rel))
        pick = preselect(codes, cands)
        titled = sum(1 for c in cands if c["kind"] == "title")
        files[rel] = {"want": [{"code": c, "title": titles.get(c)} for c in codes],
                      "candidates": cands[:MAX_CANDIDATES], "found": len(cands), "titled": titled,
                      "preselect": pick}
        ctx.log(f"  {rel}: {titled} release(s) naming "
                + (" / ".join(f"'{titles.get(c)}'" for c in codes) or "its episodes")
                + (f"; best: {cands[0]['title']}" if cands else " — none found"))
    st = load_state(series["id"])
    st.update(searched_at=db.now(), sonarr_series_id=ss["id"], files=files)
    _save_state(series["id"], st)
    ctx.log("Done. Open Re-download… (Plan tab, Damaged) to choose and grab.")
    return {"files": len(files)}


def grab(series: dict, picks: list[dict], ctx_log=None) -> dict:
    """picks: [{rel, guid}]. Grab each release in Sonarr (telling it which episodes it
    holds), then move each replaced file to the backup folder and have Sonarr rescan."""
    from .executor import backup_dir, mark_backup_folder
    log = ctx_log or (lambda m: None)
    st = load_state(series["id"])
    files = st.get("files") or {}
    sid = st.get("sonarr_series_id")
    sonarr_eps = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in sonarr.episodes(sid)}
    root = Path(series["path"]).resolve()
    done, failed, moved = [], [], []
    grabbed_rels = set()
    for p in picks:
        f = files.get(p["rel"]) or {}
        c = next((x for x in f.get("candidates") or [] if x["guid"] == p["guid"]), None)
        if not c:
            failed.append({**p, "error": "not in the last search — search again"})
            continue
        meant = c["covers"] + c["extra"]
        ids = [sonarr_eps[x] for x in meant if x in sonarr_eps] if c["kind"] == "title" else []
        # Grabbed as it numbers itself, Sonarr would import it as c["mapped"]: that's only
        # safe when those are the episodes meant (numbers-only and pack picks are chosen as such).
        plain_ok = c["kind"] != "title" or sorted(c.get("mapped") or []) == sorted(meant)
        try:
            r = sonarr.grab(c["release"], sid, ids or None, plain_ok=plain_ok)
        except sonarr.SonarrError as e:
            failed.append({**p, "title": c["title"], "error": str(e)})
            log(f"✗ {c['title']}: {e}")
            continue
        done.append({**p, "title": c["title"], "override": r["override"], "episodes": c["covers"] + c["extra"]})
        grabbed_rels.add(p["rel"])
        log(f"✓ grabbed {c['title']}" + (f" as {', '.join(c['covers'] + c['extra'])}" if r["override"] else
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
    for d in done:
        req.setdefault(d["rel"], []).append({"title": d["title"], "episodes": d["episodes"], "at": db.now()})
    for rel in moved:
        files.pop(rel, None)
    _save_state(series["id"], st)
    return {"grabbed": done, "failed": failed, "moved": moved}


def summary(series: dict) -> dict:
    st = load_state(series["id"])
    return {"searched_at": st.get("searched_at"), "files": st.get("files") or {},
            "requested": st.get("requested") or {}}

