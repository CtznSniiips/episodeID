"""TheTVDB v4 API client. Episode numbering uses TVDB "default" (aired) order,
the same order Sonarr uses, so renamed files line up with Sonarr."""
from __future__ import annotations

import time

import httpx

from .config import get_settings

BASE = "https://api4.thetvdb.com/v4"
_token: dict = {"key": None, "token": None, "at": 0.0}


class TVDBError(RuntimeError):
    pass


def _get_token(client: httpx.Client) -> str:
    s = get_settings()
    key = s["tvdb_api_key"]
    if not key:
        raise TVDBError("TVDB API key is not set (Settings → TVDB).")
    cache_key = f"{key}:{s['tvdb_pin']}"
    if _token["key"] == cache_key and time.time() - _token["at"] < 20 * 86400:
        return _token["token"]
    body = {"apikey": key}
    if s["tvdb_pin"]:
        body["pin"] = s["tvdb_pin"]
    r = client.post(f"{BASE}/login", json=body)
    if r.status_code != 200:
        raise TVDBError(f"TVDB login failed ({r.status_code}): {r.text[:200]}")
    tok = r.json()["data"]["token"]
    _token.update(key=cache_key, token=tok, at=time.time())
    return tok


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": "EpisodeID"})


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    for attempt in range(2):
        tok = _get_token(client)
        r = client.get(f"{BASE}{path}", params=params,
                       headers={"Authorization": f"Bearer {tok}"})
        if r.status_code == 401 and attempt == 0:
            _token["key"] = None  # expired; log in again
            continue
        if r.status_code == 404:
            raise TVDBError(f"TVDB: not found ({path})")
        if r.status_code != 200:
            raise TVDBError(f"TVDB {path} failed ({r.status_code}): {r.text[:200]}")
        return r.json()
    raise TVDBError("TVDB authentication failed")


def search_series(query: str) -> list[dict]:
    with _client() as c:
        data = _get(c, "/search", {"query": query, "type": "series"}).get("data") or []
    out = []
    for d in data[:20]:
        imdb = next((x.get("id") for x in d.get("remote_ids") or []
                     if (x.get("sourceName") or "").upper() == "IMDB"), None)
        name = (d.get("translations") or {}).get("eng") or d.get("name")
        out.append({
            "tvdb_id": int(d.get("tvdb_id") or d.get("id", "0").split("-")[-1]),
            "name": name,
            "year": d.get("year") or "",
            "overview": (d.get("overviews") or {}).get("eng") or d.get("overview") or "",
            "image": d.get("image_url") or d.get("thumbnail"),
            "network": d.get("network") or "",
            "imdb_id": imdb,
        })
    return out


def series_info(tvdb_id: int) -> dict:
    with _client() as c:
        d = _get(c, f"/series/{tvdb_id}/extended", {"short": "true"})["data"]
    imdb = next((x.get("id") for x in d.get("remoteIds") or []
                 if (x.get("sourceName") or "").upper() == "IMDB"), None)
    return {"tvdb_id": tvdb_id, "name": d.get("name"), "year": d.get("year") or "",
            "imdb_id": imdb}


def series_episodes(tvdb_id: int) -> list[dict]:
    """All episodes in default (aired) order, translated to the configured language."""
    lang = get_settings()["tvdb_language"] or "eng"
    episodes: list[dict] = []
    page = 0
    with _client() as c:
        while True:
            try:
                j = _get(c, f"/series/{tvdb_id}/episodes/default/{lang}", {"page": page})
            except TVDBError:
                if page == 0 and lang != "eng":
                    lang = "eng"
                    continue
                if page == 0:
                    j = _get(c, f"/series/{tvdb_id}/episodes/default", {"page": page})
                else:
                    raise
            eps = (j.get("data") or {}).get("episodes") or []
            episodes.extend(eps)
            nxt = (j.get("links") or {}).get("next")
            if not nxt or not eps:
                break
            page += 1
    out = []
    for e in episodes:
        if e.get("seasonNumber") is None or e.get("number") is None:
            continue
        out.append({
            "tvdb_episode_id": e.get("id"),
            "season": int(e["seasonNumber"]),
            "episode": int(e["number"]),
            "title": (e.get("name") or "").strip() or f"Episode {e['number']}",
            "overview": (e.get("overview") or "").strip(),
            "aired": e.get("aired"),
            "runtime": e.get("runtime"),
        })
    out.sort(key=lambda x: (x["season"] == 0, x["season"], x["episode"]))
    return out


def episode_imdb_id(tvdb_episode_id: int) -> str | None:
    with _client() as c:
        d = _get(c, f"/episodes/{tvdb_episode_id}/extended", {"meta": "translations"})["data"]
    return next((x.get("id") for x in d.get("remoteIds") or []
                 if (x.get("sourceName") or "").upper() == "IMDB"), None)
