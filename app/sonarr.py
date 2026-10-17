"""Talking to Sonarr: finding a series, its episode files, and the import webhook.

Paths: Sonarr and EpisodeID usually see the library at different mount points
(/tv/Show vs /media/tv/Show), so files are matched by their path *relative to the
series folder*, which is the same in both."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .config import get_settings

log = logging.getLogger("episodeid.sonarr")


class SonarrError(RuntimeError):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s["sonarr_url"] and s["sonarr_api_key"])


def _get(path: str, **params):
    s = get_settings()
    if not configured():
        raise SonarrError("Sonarr isn't set up (Settings → Sonarr).")
    try:
        r = httpx.get(f"{s['sonarr_url'].rstrip('/')}/api/v3/{path}", params=params,
                      headers={"X-Api-Key": s["sonarr_api_key"]}, timeout=60)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SonarrError(f"Couldn't reach Sonarr: {e}") from e


def _send(method: str, path: str, body) -> object:
    s = get_settings()
    if not configured():
        raise SonarrError("Sonarr isn't set up (Settings → Sonarr).")
    try:
        r = httpx.request(method, f"{s['sonarr_url'].rstrip('/')}/api/v3/{path}", json=body,
                          headers={"X-Api-Key": s["sonarr_api_key"]}, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else None
    except (httpx.HTTPError, ValueError) as e:
        raise SonarrError(f"Sonarr refused the change: {e}") from e


def find_series(tvdb_id: int) -> dict | None:
    found = _get("series", tvdbId=tvdb_id)
    return next((x for x in found or [] if x.get("tvdbId") == tvdb_id), None)


def code(season: int, episode: int) -> str:
    return f"S{int(season):02d}E{int(episode):02d}"


def files_by_path(sonarr_series_id: int) -> dict[str, dict]:
    """relative path → {"codes": [...], "quality": …} for every file Sonarr has."""
    files = {f["id"]: f for f in _get("episodefile", seriesId=sonarr_series_id) or []}
    out: dict[str, dict] = {}
    for ep in _get("episode", seriesId=sonarr_series_id) or []:
        f = files.get(ep.get("episodeFileId"))
        if not f:
            continue
        rel = (f.get("relativePath") or "").replace("\\", "/")
        e = out.setdefault(rel, {"codes": [], "quality": ((f.get("quality") or {}).get("quality") or {}).get("name")})
        e["codes"].append(code(ep["seasonNumber"], ep["episodeNumber"]))
    for e in out.values():
        e["codes"] = sorted(set(e["codes"]))
    for f in files.values():  # files Sonarr has but no episode points at
        rel = (f.get("relativePath") or "").replace("\\", "/")
        out.setdefault(rel, {"codes": [], "quality": None})
    return out


def compare(series: dict, scan_files: list[dict]) -> dict:
    """Where EpisodeID's picture of the series' files and Sonarr's disagree."""
    sr = find_series(series["tvdb_id"])
    if not sr:
        raise SonarrError(f"Sonarr doesn't have {series['name']} (TVDB {series['tvdb_id']}).")
    theirs = files_by_path(sr["id"])
    ours = {f["rel"]: f for f in scan_files}
    root = Path(series["path"])
    rows = []
    for rel in sorted(set(theirs) | set(ours)):
        t, o = theirs.get(rel), ours.get(rel)
        if t and not o:
            if (root / rel).exists():
                continue  # not in the last scan yet (new): nothing to compare
            rows.append({"rel": rel, "kind": "sonarr_only", "sonarr": t["codes"],
                         "why": "Sonarr lists this file, but it isn't in the series folder EpisodeID sees"})
        elif o and not t:
            rows.append({"rel": rel, "kind": "not_imported", "name": o.get("expected") or [],
                         "why": "Sonarr doesn't know this file — it hasn't been imported, so Sonarr counts "
                                "its episode as missing"})
        else:
            name = sorted(o.get("expected") or [])
            if t["codes"] != name:
                rows.append({"rel": rel, "kind": "differs", "sonarr": t["codes"], "name": name,
                             "why": f"Sonarr has it as {', '.join(t['codes']) or 'no episode'}; "
                                    f"its name says {', '.join(name) or 'nothing EpisodeID can read'}"})
    return {"sonarr_title": sr.get("title"), "sonarr_type": sr.get("seriesType"),
            "files": {"sonarr": len(theirs), "episodeid": len(ours)}, "rows": rows}


def webhook_file(payload: dict) -> tuple[int | None, str | None, str]:
    """(TVDB id, file path relative to the series folder, event) from a Sonarr webhook."""
    ev = payload.get("eventType") or ""
    s = payload.get("series") or {}
    f = payload.get("episodeFile") or {}
    rel = (f.get("relativePath") or "").replace("\\", "/") or None
    return s.get("tvdbId"), rel, ev


def episodes(sonarr_series_id: int) -> list[dict]:
    return _get("episode", seriesId=sonarr_series_id) or []


def unmonitor_seasons(sonarr_series_id: int, seasons: list[int]) -> dict:
    """Turn monitoring off for these seasons and their episodes (no more searches or
    upgrades for them)."""
    full = _get(f"series/{sonarr_series_id}")
    changed = []
    for se in full.get("seasons") or []:
        if se.get("seasonNumber") in seasons and se.get("monitored"):
            se["monitored"] = False
            changed.append(se["seasonNumber"])
    if changed:
        _send("PUT", f"series/{sonarr_series_id}", full)
    ids = [e["id"] for e in episodes(sonarr_series_id)
           if e.get("seasonNumber") in seasons and e.get("monitored")]
    if ids:
        _send("PUT", "episode/monitor", {"episodeIds": ids, "monitored": False})
    return {"seasons": sorted(set(seasons)), "season_flags": changed, "episodes": len(ids)}
