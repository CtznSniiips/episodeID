"""Which seasons are complete and correct — every aired episode has a file, and the plan
has nothing left to change or review for that season — so they can be unmonitored in
Sonarr (no more searches or "upgrades" that might bring in a wrongly labelled release)."""
from __future__ import annotations

from datetime import date

from . import autocheck, db, sonarr
from .matcher import parse_filename_episodes
from .planner import ACTIONABLE


def _season(code: str) -> int:
    return int(code[1:code.index("E")])


def seasons_status(series: dict) -> dict:
    """{"seasons": [{season, episodes, have, complete, monitored, eligible, why}], "sonarr": …}"""
    sid = series["id"]
    opts = series.get("options") or {}
    specials = bool(opts.get("include_specials"))
    today = date.today().isoformat()
    eps: dict[int, list[dict]] = {}
    for e in series.get("episodes") or []:
        if e["season"] > 0 or specials:
            eps.setdefault(e["season"], []).append(e)
    rows = {n: {"season": n, "episodes": len(es), "have": 0, "complete": False, "why": []} for n, es in eps.items()}
    # (a season still airing isn't complete; its unaired episodes aren't "missing")

    plan = db.latest_plan(sid)
    scan = db.latest_scan_summary(sid)
    if not plan or not scan or plan["created"] < scan["created"]:
        for r in rows.values():
            r["why"].append("not scanned yet" if not scan else "the plan is older than the last scan")
        return _finish(series, rows)

    have: dict[int, set] = {}
    busy: dict[int, list[str]] = {}
    for it in plan["items"]:
        codes = set(it.get("expected") or []) | set(it.get("codes") or []) | {
            c for t in it.get("targets") or [] for c in t.get("codes") or []}
        if it["kind"] == "ok":
            for c in it.get("expected") or []:
                have.setdefault(_season(c), set()).add(c)
        elif it["kind"] in ACTIONABLE or it["kind"] == "review":
            for n in {_season(c) for c in codes}:
                busy.setdefault(n, []).append(f"{it['kind']}: {it['source']}")
    st = autocheck.load_state(sid)
    changed = [r for k in ("added", "changed", "removed") for r in ((st.get("pending") or {}).get("files") or {}).get(k, [])]
    changed += [x["rel"] for x in st.get("imports") or [] if not x.get("ok")]
    for rel in changed:
        for c in parse_filename_episodes(rel.rsplit("/", 1)[-1]):
            busy.setdefault(_season(c), []).append(f"changed since the last scan: {rel}")

    for n, r in rows.items():
        es = eps[n]
        aired = [e for e in es if not e.get("aired") or e["aired"] <= today]
        if len(aired) < len(es):
            r["why"].append(f"{len(es) - len(aired)} episode(s) haven't aired yet")
        codes = {f"S{e['season']:02d}E{e['episode']:02d}" for e in aired}
        r["have"] = len(codes & have.get(n, set()))
        missing = sorted(codes - have.get(n, set()))
        if missing:
            r["why"].append(f"{len(missing)} without a correct file: " + ", ".join(missing[:6]) +
                            ("…" if len(missing) > 6 else ""))
        if busy.get(n):
            r["why"].append(f"{len(busy[n])} change(s) or review(s) in the plan, e.g. {busy[n][0]}")
        r["complete"] = not r["why"]
    return _finish(series, rows)


def _finish(series: dict, rows: dict) -> dict:
    out = {"seasons": [], "sonarr": None}
    sr = None
    if sonarr.configured():
        try:
            sr = sonarr.find_series(series["tvdb_id"])
            out["sonarr"] = "ok" if sr else f"Sonarr doesn't have {series['name']}"
        except sonarr.SonarrError as e:
            out["sonarr"] = str(e)
    else:
        out["sonarr"] = "Sonarr isn't set up (Settings → Sonarr)"
    sep: dict[int, list[dict]] = {}
    smon: dict[int, bool] = {}
    if sr:
        for e in sonarr.episodes(sr["id"]):
            sep.setdefault(e.get("seasonNumber"), []).append(e)
        smon = {x.get("seasonNumber"): bool(x.get("monitored")) for x in sr.get("seasons") or []}
        out["sonarr_id"] = sr["id"]
    for n in sorted(rows):
        r = rows[n]
        r["monitored"] = None
        if sr:
            r["monitored"] = smon.get(n, False) or any(e.get("monitored") for e in sep.get(n, []))
            nofile = [e for e in sep.get(n, []) if not e.get("hasFile")]
            if r["complete"] and nofile:
                r["why"].append(f"Sonarr has no file for {len(nofile)} episode(s), e.g. "
                                f"S{n:02d}E{nofile[0]['episodeNumber']:02d} — rescan in Sonarr or import first")
                r["complete"] = False
        r["eligible"] = bool(sr) and r["complete"] and bool(r["monitored"])
        r["why"] = "; ".join(r["why"]) if r["why"] else (
            "already unmonitored in Sonarr" if sr and not r["monitored"] else "")
        out["seasons"].append(r)
    return out


def unmonitor(series: dict, seasons: list[int]) -> dict:
    """Unmonitor these seasons — only the ones that are still complete and correct."""
    st = seasons_status(series)
    ok = [r["season"] for r in st["seasons"] if r["eligible"] and r["season"] in seasons]
    skipped = [r["season"] for r in st["seasons"] if r["season"] in seasons and r["season"] not in ok]
    res = sonarr.unmonitor_seasons(st["sonarr_id"], ok) if ok else {"seasons": [], "episodes": 0}
    return {**res, "skipped": skipped}
