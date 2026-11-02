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
from datetime import datetime, timezone
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
        st = json.loads(_state_file(sid).read_text())
    except (OSError, ValueError):
        return {}
    # The first version kept a plain list of grabs per file ([{title, episodes, at}]) and
    # didn't follow them: turn those into followed requests.
    req = st.get("requested")
    if isinstance(req, dict):
        for rel, r in list(req.items()):
            if isinstance(r, list):
                req[rel] = {"status": "waiting", "auto_retry": False, "moved": not rel.startswith(MISSING),
                            "backup": str(Path("damaged") / rel), "want": [],
                            "grabs": [{**g, "status": "waiting"} for g in r if isinstance(g, dict)]}
            elif not isinstance(r, dict):
                req.pop(rel)
                continue
            # Grabs an earlier version gave up on as "not in Sonarr's history" (it read the
            # history from a moment too late): look again, once, without starting new attempts.
            fix = [g for g in req[rel].get("grabs") or [] if not g.get("rechecked")
                   and re.match(r"Sonarr(?:'s history shows no grab| has no record)", g.get("reason") or "")]
            for g in fix:
                g.update(status="waiting", rechecked=True)
                g.pop("reason", None)
            if fix and req[rel].get("status") in ("failed", "partly imported"):
                req[rel]["status"] = "waiting"
                req[rel]["recheck_only"] = True
    return st


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
    try:
        return sonarr.grab(c["release"], sid, ids or None, plain_ok=plain_ok)
    except sonarr.SonarrError as e:
        if "404" not in str(e):
            raise
    # Sonarr only remembers search results for about half an hour: after that a grab
    # answers 404. Search again for the episode (that refreshes Sonarr's memory) and grab
    # the same release from the new results.
    fresh = _fresh_release(c, [sonarr_eps[x] for x in c["covers"] if x in sonarr_eps])
    if not fresh:
        raise sonarr.SonarrError("the indexers don't offer this release any more (searched again)")
    c["release"] = fresh
    return sonarr.grab(fresh, sid, ids or None, plain_ok=plain_ok)


def _fresh_release(c: dict, episode_ids: list[int]) -> dict | None:
    """The same release in a new search for its episode (same guid, or same name on the same indexer)."""
    for eid in episode_ids[:1]:
        for r in sonarr.episode_releases(eid):
            if r.get("guid") == c["guid"] or (
                    _norm(r.get("title")) == _norm(c["title"]) and r.get("indexerId") == (c.get("release") or {}).get("indexerId")):
                return {k: r.get(k) for k in ("guid", "indexerId", "quality", "languages")}
    return None


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

HISTORY_SLACK = 10 * 60        # read Sonarr's history from this long before a grab was noted
GRAB_SEEN_WITHIN = 30 * 60      # no "grabbed" in Sonarr's history after this long: it never happened
STUCK_AFTER = 48 * 3600         # downloading/importing for longer than this: needs a look
GONE_AFTER = 15 * 60            # grabbed, then out of Sonarr's queue this long with no import: over
_IMPORTED = ("downloadFolderImported", "seriesFolderImported")
_FAILED = ("downloadFailed", "downloadIgnored")


def _age(iso: str) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _ts(iso: str | None) -> float:
    """Seconds since the epoch for an ISO time ("…Z" or "+00:00"); a missing one counts as new."""
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return float("inf")


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
    # "stuck" ones are looked at again each time: a blocked import may now be fixable, or
    # the download may have left Sonarr's queue. Still sitting there, they're stuck again.
    for r in req.values():
        if r.get("status") == "stuck":
            r.update(status="waiting", was_stuck=True)
            for g in r.get("grabs") or []:
                if g.get("status") == "stuck":
                    g.update(status="waiting", was_stuck=True)
                    g.pop("reason", None)
    waiting = [(rel, g) for rel, r in req.items() if r.get("status") == "waiting"
               for g in r.get("grabs") or [] if g.get("status") == "waiting"]
    if not waiting:
        return 0
    if any(not g.get("episode_ids") for _rel, g in waiting):
        try:  # grabs recorded before episode ids were kept
            ids = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"]
                   for e in sonarr.episodes(st.get("sonarr_series_id"))}
            for _rel, g in waiting:
                if not g.get("episode_ids"):
                    g["episode_ids"] = [ids[c] for c in g.get("episodes") or [] if c in ids]
        except sonarr.SonarrError:
            pass
    # Sonarr records the grab while EpisodeID's request is still under way — a moment
    # *before* the time EpisodeID notes — so read from a little earlier (and allow for the
    # two clocks being a few minutes apart).
    since = datetime.fromtimestamp(min(_ts(g["at"]) for _rel, g in waiting) - HISTORY_SLACK,
                                   timezone.utc).isoformat(timespec="seconds")
    try:
        hist = sonarr.history_since(since)
    except sonarr.SonarrError as e:
        log(f"Couldn't read Sonarr's history: {e}")
        return 0
    changed = 0
    for rel, g in waiting:
        ids = set(g.get("episode_ids") or [])
        if not g.get("download_id"):
            grabs_here = [h for h in hist if h.get("eventType") == "grabbed" and h.get("episodeId") in ids
                          and _ts(h.get("date")) >= _ts(g["at"]) - HISTORY_SLACK]
            # Sonarr may record the name a little differently: by name first, else the
            # first grab of these episodes after ours
            hit = next((h for h in grabs_here if _norm(h.get("sourceTitle")) == _norm(g["title"])), None) or \
                next((h for h in sorted(grabs_here, key=lambda h: _ts(h.get("date")))
                      if not any(x.get("download_id") == h.get("downloadId") and x.get("guid") != g.get("guid")
                                 for _r, x in waiting if x is not g)), None)
            if hit:
                g["download_id"] = hit.get("downloadId")
            elif _age(g["at"]) > GRAB_SEEN_WITHIN:
                seen = sum(1 for h in hist if h.get("episodeId") in ids)
                g.update(status="failed", rechecked=True, reason="Sonarr's history shows no grab for "
                         + (", ".join(g.get("episodes") or []) or "it") + " since then"
                         + (f" ({seen} other event{'s' if seen != 1 else ''})" if seen else ""))
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
    # what has left Sonarr's queue is settled first, so "stuck" only means still sitting there
    changed += _reconcile_with_queue(series, st, [g for _rel, g in waiting if g.get("status") == "waiting"], log)
    for _rel, g in waiting:
        if _age(g["at"]) > STUCK_AFTER and g.get("status") == "waiting":
            g.update(status="stuck", reason="not imported after 2 days — check Sonarr's Activity queue")
            changed += 0 if g.get("was_stuck") else 1
    for rel, r in req.items():
        if r.get("status") != "waiting":
            continue
        states = [g.get("status") for g in r.get("grabs") or []]
        if "waiting" in states:
            continue
        recheck = r.pop("recheck_only", False)
        got = {c for g in r.get("grabs") or [] if g.get("status") == "imported" for c in g.get("episodes") or []}
        want = set(r.get("want") or []) or {c for g in r.get("grabs") or [] for c in g.get("episodes") or []}
        if "imported" in states:
            # done when what was wanted arrived — earlier failed attempts don't matter
            r["status"] = "imported" if want <= got else "partly imported"
            log(f"{rel}: replacement imported" if r["status"] == "imported" else
                f"{rel}: only some replacements imported — the damaged file stays in the backup folder")
        elif "stuck" in states:
            r["status"] = "stuck"
            if not r.get("was_stuck"):
                log(f"{rel}: replacement not imported after 2 days — check Sonarr's Activity queue")
        elif recheck:
            r["status"] = "failed"  # looked at again after an update: no new attempts started
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


def _reconcile_with_queue(series: dict, st: dict, waiting: list[dict], log) -> int:
    """Sonarr's history doesn't always say how a download ended: one removed from the queue
    (or from the download client) without blocklisting leaves no event, and a manual import
    may not carry the download id. So a grab Sonarr took that has left its queue is over:
    imported if Sonarr now has its episodes, otherwise failed once it has stayed gone for
    GONE_AFTER. Returns how many grabs changed state."""
    # not a just-made grab: Sonarr's queue and its rescan of the moved file need a moment
    seen = [g for g in waiting if g.get("download_id") and _age(g["at"]) >= 5 * 60]
    if not seen:
        return 0
    sid = st.get("sonarr_series_id")
    try:
        q = sonarr.queue(sid)
        have = {e["id"] for e in sonarr.episodes(sid) if e.get("hasFile")}
    except sonarr.SonarrError:
        return 0
    in_q_ids = {x.get("downloadId") for x in q}
    in_q_titles = {_norm(x.get("title")) for x in q}
    trouble = None  # asked only when needed
    changed = 0
    for g in seen:
        if g["download_id"] in in_q_ids or _norm(g.get("title")) in in_q_titles:
            g.pop("gone_since", None)
            item = next((x for x in q if x.get("downloadId") == g["download_id"]), None) or \
                next((x for x in q if _norm(x.get("title")) == _norm(g.get("title"))), None)
            if item and _import_blocked(item):
                _fix_import(series, sid, g, item, log)
            continue
        ids = set(g.get("episode_ids") or [])
        if ids and ids <= have:
            g.update(status="imported", done_at=db.now(), note="left Sonarr's queue and Sonarr has the episode "
                     "(no import record for this download — imported by hand, or by another grab)")
            g.pop("gone_since", None)
            changed += 1
            continue
        if trouble is None:
            trouble = sonarr.download_client_trouble()
        if trouble:
            continue  # the queue may just be missing what an unreachable client holds
        g.setdefault("gone_since", db.now())
        if _age(g["gone_since"]) >= GONE_AFTER:
            g.update(status="failed", reason="removed from Sonarr's queue without being imported")
            g.pop("gone_since", None)
            log(f"{g.get('title')}: no longer in Sonarr's queue and never imported")
            changed += 1
    return changed


AUTO_IMPORT_TRIES = 3
AUTO_IMPORT_EVERY = 10 * 60
_VIDEO = re.compile(r"(?i)\.(mkv|mp4|m4v|avi|ts|wmv|webm)$")


def _norm_path(p: str | None) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _import_blocked(item: dict) -> bool:
    """Downloaded, but Sonarr won't import it by itself (usually: the name's episode
    numbers aren't the episodes it was grabbed as) — it needs a Manual Import."""
    state = item.get("trackedDownloadState")
    return state == "importBlocked" or (state == "importPending" and item.get("trackedDownloadStatus") == "warning")


def _fix_import(series: dict, sid: int | None, g: dict, item: dict, log) -> bool:
    """Do the Manual Import Sonarr is waiting for: import the download's file(s) as the
    episodes EpisodeID grabbed it for (chosen by the titles in its name), whatever
    numbers the name carries. One file → the grab's episodes. Several → each file's
    episodes from the titles in its own name, which must cover the grab exactly.
    Notes the outcome on the grab; returns whether an import was sent."""
    ai = g.setdefault("auto_import", {"tries": 0})
    if ai.get("tries", 0) >= AUTO_IMPORT_TRIES or (ai.get("at") and _age(ai["at"]) < AUTO_IMPORT_EVERY):
        return False
    ids = list(g.get("episode_ids") or [])
    if not ids:
        ai.update(note="not imported automatically: which episodes it holds wasn't recorded")
        return False
    ai.update(at=db.now())  # tries are counted when an import is sent
    try:
        files = [f for f in sonarr.manual_import_files(g["download_id"])
                 if _VIDEO.search(f.get("path") or "") and "sample" not in (f.get("name") or f.get("path") or "").lower()]
    except sonarr.SonarrError as e:
        ai.update(note=f"couldn't read the download's files: {e}"[:200])
        return False
    # only files inside the download itself — never anything from the library
    out = _norm_path(item.get("outputPath"))
    if not out:
        ai.update(note="not imported automatically: Sonarr doesn't say where the download is")
        return False
    files = [f for f in files if _norm_path(f.get("path")) == out or _norm_path(f.get("path")).startswith(out + "/")]
    if not files:
        ai.update(note="Sonarr lists no video file in the download")
        return False
    by_code = {}
    try:
        by_code = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in sonarr.episodes(sid)}
    except sonarr.SonarrError:
        pass
    if len(files) == 1:
        # a file whose name carries another episode's title isn't what was grabbed
        named = titles_in_release(files[0].get("name") or Path(files[0].get("path") or "").name,
                                  series.get("episodes") or [], series.get("name") or "")
        if named and not {by_code.get(c) for c in named} <= set(ids):
            ai.update(note=f"not imported automatically: {Path(files[0].get('path') or '').name} "
                           f"names {', '.join(named)}, not the episodes it was grabbed for")
            return False
        plan = [(files[0], ids)]
    else:
        plan, covered = [], set()
        for f in files:
            codes = titles_in_release(f.get("name") or Path(f.get("path") or "").name,
                                      series.get("episodes") or [], series.get("name") or "")
            fids = [by_code[c] for c in codes if c in by_code]
            if not fids or not set(fids) <= set(ids) or covered & set(fids):
                ai.update(note=f"not imported automatically: can't tell which episode "
                               f"{Path(f.get('path') or '').name} is from its name")
                return False
            covered |= set(fids)
            plan.append((f, fids))
        if covered != set(ids):
            ai.update(note="not imported automatically: the files don't cover the episodes it was grabbed for")
            return False
    body = []
    for f, fids in plan:
        x = {"path": f.get("path"), "folderName": f.get("folderName"), "seriesId": sid or (f.get("series") or {}).get("id"),
             "episodeIds": fids, "releaseGroup": f.get("releaseGroup"), "quality": f.get("quality"),
             "languages": f.get("languages"), "language": f.get("language"),  # v4 / v3
             "indexerFlags": f.get("indexerFlags") or 0, "releaseType": f.get("releaseType"),
             "downloadId": g["download_id"]}
        body.append({k: v for k, v in x.items() if v is not None})
    ai.update(tries=ai.get("tries", 0) + 1)
    try:
        sonarr.manual_import(body)
    except sonarr.SonarrError as e:
        ai.update(note=f"Sonarr refused the import: {e}"[:200])
        log(f"{g.get('title')}: automatic import refused — {e}")
        return False
    names = ", ".join(c for c, i in by_code.items() if i in ids) or "its episodes"
    ai.update(note=f"import was blocked — EpisodeID imported it as {names}", sent=True)
    log(f"{g.get('title')}: Sonarr's import was blocked; imported it as {names}")
    return True


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
    # the same release posted to another indexer would fail the same way (and Sonarr has
    # blocklisted the failed one by name)
    tried_titles = {_norm(g.get("title")) for g in grabs}
    try:
        sonarr_eps = {_code(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in sonarr.episodes(sid)}
    except sonarr.SonarrError as e:
        log(f"{rel}: couldn't retry — {e}")
        return None
    for c in r.get("candidates") or []:
        if c["guid"] in tried or _norm(c["title"]) in tried_titles or c["kind"] != "title" or c.get("extra") \
                or c.get("hard_rejections") or not need <= set(c["covers"]):
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


def clear_finished(series: dict) -> int:
    """Forget re-downloads that are over: done, put back, or failed with nothing left in
    the backup folder to deal with. Returns how many were cleared."""
    st = load_state(series["id"])
    req = st.get("requested") or {}
    gone = [rel for rel, r in req.items() if r.get("status") in ("imported", "cancelled")
            or (r.get("status") == "failed" and (not r.get("moved") or r.get("restored")))]
    for rel in gone:
        req.pop(rel)
    _save_state(series["id"], st)
    return len(gone)


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


_STATES = {"downloading": "downloading", "queued": "queued in the download client", "paused": "paused",
           "delay": "waiting (Sonarr's delay profile)", "downloadClientUnavailable": "download client unreachable",
           "completed": "downloaded", "failed": "failed", "warning": "needs attention"}
_TRACKED = {"importPending": "waiting to import", "importing": "importing", "imported": "imported",
            "importBlocked": "import blocked", "failedPending": "failing", "failed": "failed"}


def attach_progress(req: dict, queue: list[dict]) -> None:
    """Add where each download in progress is, from Sonarr's queue: per cent done, time
    left, and its state ("downloading", "waiting to import", "import blocked: …")."""
    for r in req.values():
        if r.get("status") != "waiting":
            continue
        g = next((x for x in reversed(r.get("grabs") or []) if x.get("status") == "waiting"), None)
        if not g:
            continue
        ids = set(g.get("episode_ids") or [])
        q = next((x for x in queue if g.get("download_id") and x.get("downloadId") == g["download_id"]), None) or \
            next((x for x in queue if _norm(x.get("title")) == _norm(g.get("title"))), None) or \
            next((x for x in queue if x.get("episodeId") in ids), None)
        if not q:
            g["progress"] = {"state": "not in Sonarr's queue (finished, or not started yet)"}
            continue
        size, left = float(q.get("size") or 0), float(q.get("sizeleft") or 0)
        state = _TRACKED.get(q.get("trackedDownloadState")) if q.get("trackedDownloadState") not in (None, "downloading") \
            else _STATES.get(q.get("status"), q.get("status") or "")
        msgs = [m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or []] + \
            ([q["errorMessage"]] if q.get("errorMessage") else [])
        g["progress"] = {"pct": round(100 * (size - left) / size) if size else None, "timeleft": q.get("timeleft"),
                         "state": state, "message": "; ".join(dict.fromkeys(msgs))[:200] or None,
                         "client": q.get("downloadClient")}


def summary(series: dict) -> dict:
    if sonarr.configured():
        try:
            follow_up(series)
        except Exception:  # noqa: BLE001
            log_.exception("following re-downloads")
    st = load_state(series["id"])
    req = st.get("requested") or {}
    if sonarr.configured() and any(r.get("status") == "waiting" for r in req.values()):
        try:
            attach_progress(req, sonarr.queue(st.get("sonarr_series_id")))
        except Exception:  # noqa: BLE001 — progress is a nice-to-have
            log_.exception("reading Sonarr's queue")
    return {"searched_at": st.get("searched_at"), "files": st.get("files") or {},
            "requested": req, "missing": missing_episodes(series),
            "sonarr_redownload": sonarr_redownload_settings() if sonarr.configured() else None}
