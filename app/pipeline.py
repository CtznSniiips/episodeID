"""Job handlers: the long-running work behind each button in the UI."""
from __future__ import annotations

from pathlib import Path

from . import db, llm, tvdb
from .config import get_settings
from .executor import apply_plan, sonarr_rescan, undo_apply
from .jobs import handler
from .matcher import Matcher, classify, parse_filename_episodes
from .media_text import get_dialogue
from .planner import build_plan, list_videos
from .references import detect_fandom_wiki, ep_code, load_refs, refresh_references


def _series(ctx) -> dict:
    s = db.get_series(ctx.series_id)
    if s is None:
        raise RuntimeError("Series not found")
    return s


def _ensure_episodes(series: dict, ctx, force: bool = False) -> dict:
    if series["episodes"] and not force:
        return series
    ctx.status("Loading episode list from TVDB")
    eps = tvdb.series_episodes(series["tvdb_id"])
    # keep IMDb IDs we already looked up
    old = {(e["season"], e["episode"]): e for e in series["episodes"]}
    for e in eps:
        prev = old.get((e["season"], e["episode"]))
        if prev and prev.get("imdb_id") is not None:
            e["imdb_id"] = prev["imdb_id"]
    db.update_series(series["id"], episodes=eps, episodes_updated=db.now())
    ctx.log(f"TVDB: {len(eps)} episodes in "
            f"{len({e['season'] for e in eps if e['season'] > 0})} seasons.")
    return db.get_series(series["id"])


@handler("refresh_episodes")
def job_refresh_episodes(ctx, params):
    series = _ensure_episodes(_series(ctx), ctx, force=True)
    return {"episodes": len(series["episodes"])}


@handler("fetch_refs")
def job_fetch_refs(ctx, params):
    series = _ensure_episodes(_series(ctx), ctx)
    opts = dict(series.get("options") or {})
    if not opts.get("fandom_wiki") and not opts.get("fandom_checked"):
        # Free transcripts first: look for a Fandom wiki before touching OpenSubtitles quota.
        ctx.status("Looking for a Fandom transcript wiki")
        slug = detect_fandom_wiki(series["name"], series["episodes"], ctx)
        opts["fandom_checked"] = True
        if slug:
            opts["fandom_wiki"] = slug
            ctx.log(f"Found transcripts on {slug}.fandom.com — using them before OpenSubtitles. "
                    "(Change this under Series options.)")
        else:
            ctx.log("No Fandom transcript wiki found for this series.")
        db.update_series(series["id"], options=opts)
        series = db.get_series(series["id"])
    ctx.status("Fetching references")
    result = refresh_references(series, ctx, force_codes=params.get("codes"),
                                retry_misses=bool(params.get("retry_misses")))
    db.update_series(series["id"], episodes=series["episodes"])  # cached IMDb ids
    return result


@handler("scan")
def job_scan(ctx, params):
    s = get_settings()
    series = _ensure_episodes(_series(ctx), ctx)
    root = Path(series["path"])
    opts = series.get("options") or {}
    include_specials = bool(opts.get("include_specials"))
    eps = {ep_code(e["season"], e["episode"]): e for e in series["episodes"]}

    refs = {c: r["text"] for c, r in load_refs(series["tvdb_id"]).items()
            if c in eps and (include_specials or eps[c]["season"] > 0)}
    ctx.log(f"References available for {len(refs)} of "
            f"{sum(1 for e in eps.values() if include_specials or e['season'] > 0)} episodes.")
    matcher = Matcher(refs, s) if refs else None
    if matcher is None:
        ctx.log("No references yet — every file will need the AI fallback or a manual decision.")

    videos = list_videos(root, s["backup_folder"])
    if params.get("only"):
        only = set(params["only"])
        videos = [v for v in videos if str(v.relative_to(root)) in only]
    ctx.log(f"Scanning {len(videos)} video files in {root}")
    files = []
    for i, v in enumerate(videos):
        ctx.check_cancel()
        rel = str(v.relative_to(root))
        ctx.progress(i / max(1, len(videos)), f"Scanning {v.name}")
        expected = parse_filename_episodes(v.name)
        try:
            dlg = get_dialogue(v, ctx, allow_whisper=params.get("whisper", True))
        except Exception as e:  # noqa: BLE001
            ctx.log(f"{rel}: could not read ({e})")
            files.append({"rel": rel, "size": v.stat().st_size, "duration": 0,
                          "text_source": "none", "expected": expected, "segments": [],
                          "whole": [], "status": "ERROR", "note": str(e)[:200]})
            continue
        res = matcher.match_file(dlg["cues"], dlg["duration"]) if matcher and dlg["cues"] \
            else {"segments": [], "whole": []}
        status, note = classify(expected, res["segments"], dlg["source"])
        files.append({"rel": rel, "size": v.stat().st_size, "duration": dlg["duration"],
                      "text_source": dlg["source"], "expected": expected,
                      "segments": res["segments"], "whole": res["whole"],
                      "status": status, "note": note})
        det = " + ".join(f"{sg['code']}{'' if sg['confidence'] == 'high' else '?'}"
                         for sg in res["segments"]) or "-"
        ctx.log(f"{status:<16} {rel}  →  {det}")
        for sg in res["segments"]:
            if sg.get("why"):
                ctx.log(f"{'':<16}   {sg['code']}? {sg['why']}")

    if llm.available():
        _llm_pass(series, files, eps, refs, ctx)

    counts: dict[str, int] = {}
    for f in files:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    scan = {"files": files, "counts": counts, "reference_count": len(refs)}
    scan_id = db.add_scan(series["id"], scan)
    items = build_plan(db.get_series(series["id"]), db.latest_scan(series["id"])["results"])
    plan_id = db.add_plan(series["id"], scan_id, items)
    ctx.log("Summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return {"scan_id": scan_id, "plan_id": plan_id, "counts": counts}


def _llm_pass(series, files, eps, refs, ctx):
    claimed = {sg["code"] for f in files for sg in f["segments"] if sg["confidence"] == "high"}
    no_ref = [c for c in eps if c not in refs and eps[c]["season"] > 0]
    todo = [f for f in files if f["text_source"] != "none" and
            (f["status"] in ("LOW_CONFIDENCE", "NO_MATCH"))]
    if not todo:
        return
    ctx.log(f"AI fallback: {len(todo)} files the dialogue match couldn't settle "
            f"(LOW_CONFIDENCE / NO_MATCH) → asking {get_settings()['llm_model']}")
    for n, f in enumerate(todo):
        ctx.check_cancel()
        ctx.progress(n / len(todo), f"AI: {Path(f['rel']).name}")
        dlg = get_dialogue(Path(series["path"]) / f["rel"], ctx)
        segs = f["segments"]
        if not segs:
            segs = [{"start": 0, "end": dlg["duration"], "code": None, "score": 0,
                     "margin": 0, "confidence": "none", "alternatives": []}]
            f["segments"] = segs
        for sg in segs:
            if sg["confidence"] == "high":
                continue
            cands: list[str] = []
            seasons = {c[:3] for c in f["expected"]}
            pool = (list(f["expected"]) + ([sg["code"]] if sg["code"] else []) +
                    [a["code"] for a in sg.get("alternatives") or []] +
                    [w["code"] for w in f.get("whole") or []] +
                    [c for c in no_ref if c[:3] in seasons] + no_ref)
            for c in pool:
                if c in eps and c not in claimed and c not in cands:
                    cands.append(c)
                if len(cands) >= 12:
                    break
            if not cands:
                continue
            text = " ".join(c[2] for c in dlg["cues"] if c[1] > sg["start"] and c[0] < sg["end"])
            try:
                verdict = llm.judge(text, [{"code": c, "title": eps[c]["title"],
                                            "overview": eps[c]["overview"]} for c in cands],
                                    series["name"])
            except Exception as e:  # noqa: BLE001
                ctx.log(f"  AI error: {e}")
                return
            if verdict:
                sg["llm"] = verdict
                ctx.log(f"  {f['rel']}: AI suggests {verdict['code']} "
                        f"({verdict['confidence']:.0%}) — {verdict['reason']}")
            else:
                ctx.log(f"  {f['rel']}: AI had no confident answer")


@handler("replan")
def job_replan(ctx, params):
    series = _series(ctx)
    scan = db.latest_scan(series["id"])
    if not scan:
        raise RuntimeError("Scan the series first.")
    items = build_plan(series, scan["results"])
    plan_id = db.add_plan(series["id"], scan["id"], items)
    return {"plan_id": plan_id}


@handler("apply")
def job_apply(ctx, params):
    series = _series(ctx)
    plan = db.get_plan(params["plan_id"])
    if plan is None or plan["series_id"] != series["id"]:
        raise RuntimeError("Plan not found")
    if plan["status"] != "draft":
        raise RuntimeError("This plan was already applied. Rescan to make a new plan.")
    result = apply_plan(series, plan, ctx)
    if result.get("applied") and get_settings()["sonarr_rescan_after_apply"]:
        sonarr_rescan(series, ctx)
    return result


@handler("undo")
def job_undo(ctx, params):
    series = _series(ctx)
    ap = db.get_apply(params["apply_id"])
    if ap is None or ap["series_id"] != series["id"]:
        raise RuntimeError("Apply not found")
    if ap["undone_at"]:
        raise RuntimeError("Already undone")
    result = undo_apply(series, ap, ctx)
    if get_settings()["sonarr_rescan_after_apply"]:
        sonarr_rescan(series, ctx)
    return result
