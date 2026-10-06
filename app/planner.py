"""Turning scan results into a rename plan.

A plan is a list of items. Each item is one source file and what should happen
to it. Nothing here touches the disk except reading directory listings; the
executor carries plans out.

Item kinds:
  ok         file already correct — nothing to do
  rename     move/rename to the correct episode name (may include season folder)
  split      cut a multi-episode file into one file per episode, then rename
  aside      move into the backup folder (duplicate, or its name is needed by
             another file and this one couldn't be verified)
  review     not enough evidence; shown so you can type the right episode in
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import get_settings
from .media_text import SUB_EXTS, VIDEO_EXTS
from .references import describe_conflict, ref_meta

META_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".tbn", ".xml"}
QUALITY_RE = re.compile(
    r"(?i)\b((?:WEB[- .]?DL|WEB[- .]?Rip|Blu[- ]?Ray|HDTV|DVD(?:Rip)?|SDTV|Remux|Raw-HD)"
    r"(?:[- .]?\d{3,4}[pi])?(?:[ .-]Proper)?|\d{3,4}p)\b")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# Sonarr's FileNameBuilder: with "Replace Illegal Characters" on, these are swapped
# for the matching character; off, they're removed. Colons follow "Colon Replacement".
_SONARR_BAD = ("\\", "/", "<", ">", "?", "*", "|", '"')
_SONARR_GOOD = ("+", "+", "", "", "!", "-", "", "")
_CONTROL = re.compile(r"[\x00-\x1f]")


def replace_colons(name: str, mode: str = "smart", custom: str = "") -> str:
    """Colons can't be in filenames; replace them the way Sonarr's "Colon Replacement"
    setting does, so names match what Sonarr would write."""
    if mode == "delete":
        return name.replace(":", "")
    if mode == "dash":
        return name.replace(":", "-")
    if mode == "space_dash":
        return name.replace(":", " -")
    if mode == "space_dash_space":
        return name.replace(":", " - ")
    if mode == "custom":
        return name.replace(":", custom)
    # smart: "Up: Pups" → "Up - Pups", "10:30" → "10-30"
    return name.replace(": ", " - ").replace(":", "-")


def clean_token(text: str, settings: dict | None = None) -> str:
    """Clean a series or episode title for a filename exactly as Sonarr does."""
    st = settings or {}
    replace = st.get("replace_illegal_characters", True)
    for bad, good in zip(_SONARR_BAD, _SONARR_GOOD):
        text = text.replace(bad, good if replace else "")
    text = replace_colons(text, st.get("colon_replacement", "smart"),
                          st.get("colon_replacement_custom", ""))
    text = _CONTROL.sub("", text)
    return text.lstrip(" .").rstrip(" ")


def sanitize(name: str) -> str:
    """Last-resort safety for the assembled name (e.g. characters typed into the
    naming format itself)."""
    name = clean_token(name, {"replace_illegal_characters": False, "colon_replacement": "delete"})
    return re.sub(r"\s{2,}", " ", name).strip()


class _EpNum:
    """Formats like an int for {episode:02d}, but renders ranges for multi-episode files."""

    def __init__(self, nums: list[int], style: str):
        self.nums, self.style = nums, style

    def __format__(self, spec):
        parts = [format(n, spec) for n in self.nums]
        if len(parts) == 1:
            return parts[0]
        if self.style == "repeat":          # S01E01E02
            return "E".join(parts)
        if self.style == "extend":          # S01E01-02
            return f"{parts[0]}-{parts[-1]}"
        if self.style == "duplicate":       # S01E01.S01E02 is rarely wanted; fall back
            return f"{parts[0]}-E{parts[-1]}"
        return f"{parts[0]}-E{parts[-1]}"   # prefixed_range (Sonarr default): S01E01-E02


_MULTI_PART = re.compile(r"(?:\:?\s?(?:\(\d+\)|(Part|Pt\.?)\s?\d+))$", re.I)
MAX_NAME_BYTES = 255


def _episode_titles(eps: list[dict]) -> list[str]:
    """Titles as Sonarr prepares them for {Episode Title} (GetEpisodeTitles)."""
    raw = [(e.get("title") or "").rstrip(" .?") for e in eps]
    if len(raw) == 1:
        return raw
    titles = list(dict.fromkeys(_MULTI_PART.sub("", t).strip() for t in raw))
    if all(not t.strip() for t in titles):
        titles = list(dict.fromkeys(raw))
    return titles


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_bytes(s: str, n: int) -> str:
    return s.encode("utf-8")[:max(0, n)].decode("utf-8", "ignore")


def _episode_title(titles: list[str], max_len: int) -> str:
    """Sonarr's GetEpisodeTitle: all titles joined with " + " if they fit, else
    "first...last", else "first...", else the first title cut short."""
    joined = " + ".join(titles)
    if _bytes(joined) <= max_len:
        return joined
    first = titles[0]
    if len(titles) >= 2 and _bytes(first) + _bytes(titles[-1]) + 3 <= max_len:
        return f"{first.rstrip(' .')}...{titles[-1]}"
    if len(titles) > 1 and _bytes(first) + 3 <= max_len:
        return f"{first.rstrip(' .')}..."
    if len(titles) == 1 and _bytes(first) <= max_len:
        return first
    return f"{_truncate_bytes(first, max_len - 3).rstrip(' .')}..."


def episode_filename(series_name: str, eps: list[dict], ext: str, original: str,
                     settings: dict) -> str:
    q = QUALITY_RE.search(original)
    tokens = {
        "series": clean_token(series_name, settings),
        "season": eps[0]["season"],
        "episode": _EpNum([e["episode"] for e in eps], settings["multi_episode_style"]),
        "title": "",
        "quality": q.group(1) if q else "",
        "year": "",
    }
    fmt = settings["naming_format"]
    try:
        without_title = fmt.format(**tokens)
    except (KeyError, ValueError, IndexError):
        fmt = "{series} - S{season:02d}E{episode:02d} - {title}"
        without_title = fmt.format(**tokens)
    # As Sonarr: the title gets whatever is left of a 255-byte file name once the
    # extension and the rest of the name are counted; it is measured before illegal
    # characters are replaced.
    max_len = MAX_NAME_BYTES - _bytes(ext) - _bytes(without_title)
    tokens["title"] = clean_token(_episode_title(_episode_titles(eps), max_len), settings)
    name = re.sub(r"\s{2,}", " ", fmt.format(**tokens)).strip(" -")
    name = sanitize(name)
    if _bytes(name) > MAX_NAME_BYTES - _bytes(ext):
        name = _truncate_bytes(name, MAX_NAME_BYTES - _bytes(ext)).rstrip()
    return name + ext


def season_dir(root: Path, season: int, settings: dict, layout: dict) -> Path:
    if layout["flat"]:
        return root
    if season in layout["season_dirs"]:
        return layout["season_dirs"][season]
    if season == 0:
        return root / settings["specials_folder"]
    return root / settings["season_folder_format"].format(season=season)


def detect_layout(root: Path, backup_name: str) -> dict:
    season_dirs: dict[int, Path] = {}
    for d in root.iterdir():
        if not d.is_dir() or d.name == backup_name:
            continue
        m = re.fullmatch(r"(?i)(?:season|series|s)\s*0*(\d+)", d.name.strip())
        if m:
            season_dirs[int(m[1])] = d
        elif d.name.lower() in ("specials", "season 0", "season 00"):
            season_dirs[0] = d
    root_videos = any(f.suffix.lower() in VIDEO_EXTS for f in root.iterdir() if f.is_file())
    return {"season_dirs": season_dirs, "flat": not season_dirs and root_videos}


def list_videos(root: Path, backup_name: str) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        rel = p.relative_to(root).parts
        if rel[0] == backup_name or any(part.startswith(".") for part in rel):
            continue
        low = p.name.lower()
        if "sample" in low and p.stat().st_size < 150 * 1024 * 1024:
            continue
        if any(part.lower() in ("extras", "featurettes", "behind the scenes") for part in rel):
            continue
        out.append(p)
    return sorted(out)


def sidecars(video: Path) -> list[Path]:
    stem = video.with_suffix("").name
    out = []
    for f in video.parent.iterdir():
        if f == video or not f.is_file() or not f.name.startswith(stem):
            continue
        rest = f.name[len(stem):]
        if rest[:1] in (".", "-") and (f.suffix.lower() in SUB_EXTS or
                                       f.suffix.lower() in META_EXTS):
            out.append(f)
    return out


# ---------------------------------------------------------------------------

def _segment_choice(seg: dict) -> tuple[str | None, str, bool]:
    """(code, strength, ai). strength: 'high' | 'ai' | 'low'."""
    if seg.get("override"):
        return seg["override"], "manual", False
    if seg.get("confidence") == "high":
        return seg["code"], "high", False
    if seg.get("llm"):
        return seg["llm"]["code"], "ai", True
    return seg.get("code"), "low", False


def build_plan(series: dict, scan: dict) -> list[dict]:
    s = get_settings()
    root = Path(series["path"])
    backup = s["backup_folder"]
    layout = detect_layout(root, backup)
    eps = {f"S{e['season']:02d}E{e['episode']:02d}": e for e in series["episodes"]}
    series_name = (series.get("options") or {}).get("name_in_files") or root.name

    files = scan["files"]
    items: list[dict] = []
    overrides = (series.get("options") or {}).get("overrides") or {}

    # Manual overrides typed in the UI: one code per segment, or one code for the whole file.
    for f in files:
        ov = overrides.get(f["rel"])
        if not ov:
            continue
        segs = [dict(sg) for sg in (f.get("segments") or [])]
        if len(ov) == len(segs) and len(ov) > 1:
            for sg, cd in zip(segs, ov):
                sg["override"] = cd
        elif len(ov) == 1:
            segs = [{"start": 0, "end": f.get("duration") or 0, "code": ov[0],
                     "override": ov[0], "score": 1.0, "confidence": "manual"}]
        else:  # several codes for a file that didn't segment that way → treat as multi-ep file
            segs = [{"start": 0, "end": f.get("duration") or 0, "code": cd, "override": cd,
                     "score": 1.0, "confidence": "manual"} for cd in ov]
        f["segments"] = segs
        f["status"] = "MANUAL"

    # 1. Decide each file's episode list from its segments.
    decided: dict[str, list[tuple[dict, str, str, bool]]] = {}
    for f in files:
        chosen = []
        for seg in f.get("segments") or []:
            code, strength, ai = _segment_choice(seg)
            if code:
                chosen.append((seg, code, strength, ai))
        decided[f["rel"]] = chosen

    # 2. Claims: which (file, segment) owns each episode. Best evidence wins.
    rank = {"manual": 3, "high": 2, "ai": 1, "low": 0}
    claims: dict[str, list[tuple]] = {}
    for f in files:
        for seg, code, strength, ai in decided[f["rel"]]:
            if strength == "low":
                continue
            # Prefer: stronger evidence, then the file already named for it, then score.
            named = code in (f.get("expected") or [])
            claims.setdefault(code, []).append((rank[strength], named, seg.get("score", 0),
                                                f["rel"], seg.get("start", 0)))
    winner: dict[str, tuple[str, float]] = {}
    for code, cl in claims.items():
        cl.sort(reverse=True)
        winner[code] = (cl[0][3], cl[0][4])

    # Files currently occupying each episode code (by filename).
    occupants: dict[str, list[str]] = {}
    for f in files:
        for code in f.get("expected") or []:
            occupants.setdefault(code, []).append(f["rel"])

    moving: set[str] = set()   # files that will leave their current path
    claimed_targets: dict[str, str] = {}

    for f in files:
        rel = f["rel"]
        src = root / rel
        chosen = decided[rel]
        base = {"source": rel, "status": f["status"], "note": f.get("note", ""),
                "text_source": f.get("text_source"), "expected": f.get("expected") or [],
                "segments": f.get("segments") or [], "selected": False}

        usable = [c for c in chosen if c[2] != "low"]
        if f["status"] in ("NO_TEXT",) or not usable or len(usable) != len(chosen) \
                or (f.get("hold") and f["status"] != "MANUAL"):
            # Not enough evidence to act on every part of this file.
            items.append({**base, "kind": "review",
                          "reason": f.get("note") or "Needs a decision",
                          "suggested": [c[1] for c in chosen]})
            continue

        codes = [c[1] for c in usable]
        lost = [c for c in usable if winner.get(c[1], (None,))[0] != rel or
                winner[c[1]][1] != c[0].get("start", 0)]
        ai = any(c[3] for c in usable)
        manual = any(c[2] == "manual" for c in usable)

        if len(lost) == len(usable):
            items.append({**base, "kind": "aside", "bucket": "duplicates",
                          "reason": f"Duplicate of {', '.join(codes)} (a better match exists)",
                          "targets": [{"path": f"{backup}/duplicates/{rel}"}],
                          "selected": not ai})
            moving.add(rel)
            continue

        if f["status"] == "OK" and not ai and not manual:
            items.append({**base, "kind": "ok", "reason": "Correct"})
            continue

        expected = f.get("expected") or []
        if codes == expected:
            # Same episodes as the filename already says (e.g. the AI agreeing with it):
            # nothing to fix. Title formatting alone is never a reason to rename.
            items.append({**base, "kind": "ok", "reason": (
                "Correct — the episodes you set are the ones in the filename" if manual else
                "Correct — the evidence agrees with the filename" + (" (AI)" if ai else ""))})
            continue
        covered = sum(max(0.0, float(c[0].get("end", 0)) - float(c[0].get("start", 0))) for c in usable)
        dur = float(f.get("duration") or 0)
        if (expected and not manual and set(codes) < set(expected)
                and (ai or (dur and covered < 0.75 * dur))):
            # Evidence for only some of the filename's episodes, and nothing saying the
            # rest isn't there: renaming would drop them from the name. Leave it alone.
            missing = [c for c in expected if c not in codes]
            items.append({**base, "kind": "review",
                          "reason": f"{', '.join(codes)} confirmed{' by AI' if ai else ''}; no evidence "
                                    f"either way for {', '.join(missing)} — left as named",
                          "suggested": expected})
            continue

        missing_meta = [cd for cd in codes if cd not in eps]
        if missing_meta:
            items.append({**base, "kind": "review",
                          "reason": f"Unknown episode(s) {', '.join(missing_meta)} on TVDB",
                          "suggested": codes})
            continue
        first = eps[codes[0]]
        consecutive = (len(set(codes)) == len(codes) and
                       all(eps[cd]["season"] == first["season"] for cd in codes) and
                       [eps[cd]["episode"] for cd in codes] ==
                       list(range(first["episode"], first["episode"] + len(codes))))

        if len(codes) == 1 or consecutive:
            if lost:
                items.append({**base, "kind": "review",
                              "reason": "Part of this file duplicates another file",
                              "suggested": codes})
                continue
            ep_list = [eps[cd] for cd in codes]
            name = episode_filename(series_name, ep_list, src.suffix, src.name, s)
            dst = season_dir(root, ep_list[0]["season"], s, layout) / name
            dst_rel = str(dst.relative_to(root))
            if dst_rel == rel:
                items.append({**base, "kind": "ok", "reason": "Correct"})
                continue
            items.append({**base, "kind": "rename",
                          "reason": ("AI suggestion — check before applying" if ai else
                                     f.get("note") or "Name does not match content"),
                          "ai": ai, "codes": codes,
                          "targets": [{"path": dst_rel, "codes": codes}],
                          "selected": not ai})
            moving.add(rel)
            for cd in codes:
                claimed_targets[cd] = rel
            continue

        # Multiple episodes that can't share one name → split.
        if not s["allow_splits"]:
            items.append({**base, "kind": "review",
                          "reason": "Needs splitting (splits are disabled in settings)",
                          "suggested": codes})
            continue
        if any(c[0].get("end", 0) <= c[0].get("start", 0) for c in usable):
            items.append({**base, "kind": "review",
                          "reason": "These episodes need a split, but no boundaries were "
                                    "detected in this file",
                          "suggested": codes})
            continue
        targets = []
        for seg, cd, strength, is_ai in usable:
            e = eps[cd]
            is_dup = (winner.get(cd, (None,))[0] != rel or winner[cd][1] != seg.get("start", 0))
            name = episode_filename(series_name, [e], src.suffix, src.name, s)
            if is_dup:
                path = f"{backup}/duplicates/{name}"
            else:
                path = str((season_dir(root, e["season"], s, layout) / name).relative_to(root))
                claimed_targets[cd] = rel
            targets.append({"path": path, "codes": [cd], "start": seg["start"],
                            "end": seg["end"], "duplicate": is_dup})
        items.append({**base, "kind": "split", "codes": codes, "ai": ai,
                      "reason": ("Episodes out of order inside the file"
                                 if f["status"] == "OK_ORDER_DIFFERS" else
                                 "Episodes that can't share one filename"),
                      "targets": targets, "selected": not ai})
        moving.add(rel)

    # 3. Distrust changes that rest on a reference whose numbering is disputed
    #    (OpenSubtitles/IMDb number the episode differently from TVDB). Those can
    #    hold the neighbouring episode's dialogue, which looks exactly like two
    #    correctly named files that need swapping.
    meta = ref_meta(series["tvdb_id"])
    for it in items:
        if it["kind"] not in ("rename", "split", "aside"):
            continue
        involved = set(it.get("codes") or []) | set(it.get("expected") or [])
        shaky = sorted(c for c in involved if (meta.get(c) or {}).get("low_trust"))
        segs = it.get("segments") or []
        by_title = bool(segs) and all("title" in (sg.get("evidence") or "") for sg in segs)
        if shaky and not by_title and not any(sg.get("override") for sg in segs):
            details = "; ".join(describe_conflict(meta[c], c, (eps.get(c) or {}).get("title"))
                                for c in shaky)
            it["selected"] = False
            it["ref_warning"] = True
            it["reason"] = (f"Check first — {details}. Fetch references again once more wiki "
                            "transcripts are in to re-check it, or upload the right subtitle.")

    # 4. Files whose episode name is now needed by a confirmed file, but which
    #    themselves stay put unverified → move aside so Sonarr doesn't see two.
    #    Only pre-ticked when the file taking the name is itself pre-ticked (not an AI
    #    guess, not held back by a reference warning), and never for a file whose own
    #    title card confirmed its name: two files then claim the same episode, and
    #    which one really has it is for you to decide.
    by_rel = {it["source"]: it for it in items}
    for code, new_owner in claimed_targets.items():
        owner = by_rel[new_owner]
        for occ in occupants.get(code, []):
            if occ == new_owner or occ in moving:
                continue
            it = by_rel[occ]
            if it["kind"] not in ("review",):
                continue
            card_ok = sorted({sg["code"] for sg in it.get("segments") or []
                              if sg.get("title_card") and sg.get("code") in it["expected"]})
            how = "AI suggestion" if owner.get("ai") else "title card" if all(
                "title" in (sg.get("evidence") or "") for sg in owner.get("segments") or []
                if sg.get("code") == code) else "dialogue"
            if card_ok:
                it["reason"] = (f"{(it.get('reason') or '').rstrip('. ')}. {code} is also claimed by "
                                f"{new_owner} ({how}); this file's title card confirms "
                                f"{', '.join(card_ok)} — check which file really has {code}.")
                it["claimed_by"] = new_owner
                continue
            it.update(kind="aside", bucket="unverified",
                      reason=f"Its name ({code}) is needed by {new_owner} ({how}); "
                             f"this file couldn't be verified",
                      targets=[{"path": f"{backup}/unverified/{occ}"}],
                      selected=bool(owner.get("selected")))
            moving.add(occ)

    for i, it in enumerate(items):
        it["id"] = i
    order = {"split": 0, "rename": 1, "aside": 2, "review": 3, "ok": 4}
    items.sort(key=lambda it: (order[it["kind"]], it["source"]))
    return items
