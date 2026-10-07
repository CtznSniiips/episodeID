"""Job handlers: the long-running work behind each button in the UI."""
from __future__ import annotations

import time
from pathlib import Path

from . import db, fandom, llm, titlecard, tvdb
from .config import get_settings
from .executor import apply_plan, sonarr_rescan, undo_apply
from .jobs import handler
from .matcher import Matcher, classify, parse_filename_episodes
from .media_text import get_dialogue
from .planner import build_plan, list_videos
from .references import (SAME_EPISODE_SCORE, detect_fandom_wiki, ep_code, load_refs, refresh_references,
                         verify_flagged)


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


def _find_wiki(series: dict, ctx, force: bool = False) -> dict:
    """Discover the series' Fandom wiki and/or map its transcript pages to episodes.
    Runs when no wiki is known yet, or when a wiki was typed in but its pages
    haven't been mapped. Returns the updated series."""
    opts = dict(series.get("options") or {})
    regular = sum(1 for e in series["episodes"] if e["season"] >= 1)
    wiki = (opts.get("fandom_wiki") or "").strip()
    if wiki and not force:
        if opts.get("fandom_page_map") is not None:
            return series
        ctx.status(f"Listing transcript pages on {wiki}")
        with fandom._client() as c:
            pages, how = fandom.list_transcript_pages(c, wiki)
        mapping = fandom.map_pages([e for e in series["episodes"] if e["season"] >= 1], pages)
        opts["fandom_page_map"] = mapping
        opts["fandom_discovery"] = {"wiki": wiki, "mapped": len(mapping), "total": regular,
                                    "found_via": how, "auto": False, "at": db.now()}
        ctx.log(f"{wiki}: {len(pages)} transcript pages ({how}); {len(mapping)} of {regular} "
                "episodes matched to a page by title.")
    elif force or not opts.get("fandom_checked"):
        ctx.status("Looking for a Fandom transcript wiki")
        ctx.log(f"Looking for a Fandom wiki for '{series['name']}'…")
        found = fandom.discover(series["name"], series["episodes"], ctx)
        opts["fandom_checked"] = True
        if found:
            opts["fandom_wiki"] = found["wiki"]
            opts["fandom_page_map"] = found["page_map"]
            if found.get("pattern") and not opts.get("fandom_page_pattern"):
                opts["fandom_page_pattern"] = found["pattern"]
            opts["fandom_discovery"] = {"wiki": found["wiki"], "sitename": found["sitename"],
                                        "mapped": found["mapped"], "total": regular,
                                        "found_via": found["found_via"], "auto": True,
                                        "at": db.now()}
            ctx.log(f"Found {found['wiki']}.fandom.com ({found['sitename']}): {found['mapped']} of "
                    f"{regular} episodes have a transcript page — using them before OpenSubtitles.")
        else:
            # Wikis without a transcript category or searchable titles: try the classic layout.
            slug = detect_fandom_wiki(series["name"], series["episodes"], ctx)
            if slug:
                opts["fandom_wiki"] = slug
                opts["fandom_page_map"] = {}
                opts["fandom_discovery"] = {"wiki": slug, "mapped": 0, "total": regular,
                                            "found_via": "{title}/Transcript pages", "auto": True,
                                            "at": db.now()}
                ctx.log(f"Found transcripts on {slug}.fandom.com ({{title}}/Transcript pages).")
            else:
                opts["fandom_discovery"] = {"wiki": None, "at": db.now()}
                ctx.log("No Fandom transcript wiki found for this series.")
    else:
        return series
    db.update_series(series["id"], options=opts)
    return db.get_series(series["id"])


@handler("fetch_refs")
def job_fetch_refs(ctx, params):
    series = _ensure_episodes(_series(ctx), ctx)
    before = dict((series.get("options") or {}).get("fandom_page_map") or {})
    series = _find_wiki(series, ctx, force=bool(params.get("find_wiki")))
    after = (series.get("options") or {}).get("fandom_page_map") or {}
    # Episodes that just gained a transcript page deserve another try even if they were
    # recorded as "not found" earlier.
    retry = [c for c in after if before.get(c) != after[c]]
    ctx.status("Fetching references")
    result = refresh_references(series, ctx, force_codes=params.get("codes"),
                                retry_misses=bool(params.get("retry_misses")),
                                retry_codes=retry)
    ctx.status("Cross-checking flagged OpenSubtitles references")
    result["verification"] = verify_flagged(db.get_series(series["id"]), ctx)
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
        _discount_unreferenced(res["segments"], expected, dlg["duration"], refs)
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

    _titlecard_pass(series, files, eps, ctx, reread=bool(params.get("reread_cards")))

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


# ------------------------------------------------------------- title cards

def _fmt_t(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _merge_windows(ws: list) -> list:
    out: list = []
    for a, b in sorted(ws):
        if out and a <= out[-1][1] + 5:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _discount_unreferenced(segs: list[dict], expected: list[str], duration: float,
                           refs: dict) -> None:
    """Dialogue matched to an episode other than the filename's only counts against
    the filename when the filename's episode has a reference. Without one the
    matcher can only ever pick the closest other episode — and shows that reuse
    plots (PAW Patrol has several elephant rescues) make that look convincing."""
    for sg in segs:
        if not expected or sg["code"] in expected:
            continue
        pos = _expected_at(expected, (sg["start"] + sg["end"]) / 2, duration)
        cands = [pos] if pos else expected
        if any(c in refs for c in cands) or float(sg.get("score") or 0) >= SAME_EPISODE_SCORE:
            continue  # the filename's episode was a candidate, or this is plainly that episode
        sg.update(confidence="low", no_ref_for=cands[0],
                  why=f"{' / '.join(cands)} (per the filename) has no reference yet, so the "
                      f"dialogue could only match other episodes — {sg['code']} was the closest")


def _expected_at(expected: list[str], t: float, duration: float) -> str | None:
    """The episode the filename puts at time t: for "S11E38E39" the first half is
    S11E38 and the second S11E39."""
    if not expected:
        return None
    if len(expected) == 1:
        return expected[0]
    if duration <= 0:
        return None  # position unknown: callers fall back to all of the filename's episodes
    return expected[min(len(expected) - 1, int(t / (duration / len(expected))))]


def _resolve_partial(c: dict, sg: dict | None, expected: list[str]) -> str | None:
    """A partial read ("PUPS SAVE RYDER'S…", "MIGHTY PUPS") lists the titles it could
    be the start of. It can confirm the dialogue's match or the filename when either
    is one of those titles — it never picks an episode on its own."""
    cands = set(c.get("candidates") or [])
    if sg and sg.get("confidence") == "high" and sg.get("code") in cands:
        return sg["code"]                       # confirms a confident dialogue match
    for e in expected:
        if e in cands:
            return e                            # confirms the filename
    if sg:
        for code in [sg.get("code")] + [a["code"] for a in sg.get("alternatives") or []]:
            if code in cands:
                return code                     # agrees with a weaker dialogue candidate
    return None


def _apply_title_cards(f: dict, cards: list[dict], runtimes: dict | None = None) -> list[str]:
    """Merge title-card evidence into a scanned file's segments. Returns log notes.
    runtimes: episode code → minutes (TVDB), used to judge whether a stretch of
    weak dialogue fits inside a card-confirmed episode."""
    keep = ("code", "title", "text", "score", "time", "partial", "candidates")
    f["title_cards"] = [{k: c.get(k) for k in keep} for c in cards]
    if not cards:
        return []
    notes = []
    segs = f["segments"]
    expected = f.get("expected") or []
    dur = float(f.get("duration") or 0)
    if not segs:
        # No usable dialogue: the title cards alone identify the episode(s).
        usable = []
        for i, c in enumerate(f["title_cards"]):
            pos = _expected_at(expected, c["time"], dur)
            code = c["code"] if not c.get("partial") else \
                _resolve_partial(c, None, [pos] if pos else expected)
            if code:
                usable.append((code, c))
        for i, (code, c) in enumerate(usable):
            if i == 0:
                # Starts the file — unless the filename puts an earlier episode before it
                # (whose card wasn't read): then it starts around its card.
                later = code in expected and expected.index(code) > 0
                start = max(0.0, c["time"] - 60) if later else 0.0
            else:
                start = max(segs[-1]["start"] + 60, c["time"] - 5)
            if segs:
                segs[-1]["end"] = max(segs[-1]["start"] + 30, c["time"] - 90)
            segs.append({"start": round(start, 1), "end": round(dur, 1), "code": code,
                         "score": c["score"], "margin": 0, "confidence": "high",
                         "alternatives": [], "evidence": "title" if not c.get("partial")
                         else "title+filename", "why": "", "title_card": {**c, "code": code}})
        if usable:
            notes.append("identified from title card" + ("s" if len(usable) > 1 else ""))
    else:
        confirmed: set[str] = set()  # filename episodes a title card has confirmed
        originals = {id(sg): dict(sg) for sg in segs}  # what the dialogue said, before cards
        for c in f["title_cards"]:
            # The stretch of dialogue this card belongs to: one it falls inside (a card can
            # come up to 60 s before its dialogue starts, and 15 s after a stretch ends, as
            # dialogue is matched in overlapping windows). None → the card is in a stretch
            # where the dialogue matched nothing — at the start, between, or at the end.
            inside = [j for j, sg in enumerate(segs)
                      if sg["start"] - 60 <= c["time"] <= sg["end"] + 15]
            idx = max(inside) if inside else None
            sg = segs[idx] if idx is not None else None
            pos_expected = _expected_at(expected, c["time"], dur)
            if c.get("partial"):
                code = _resolve_partial(c, sg, [pos_expected] if pos_expected else expected)
                if not code:
                    continue  # incomplete read that matches nothing else: no evidence
                c = {**c, "code": code}
            if sg is None:
                # Typically an episode with no reference: the card starts a segment of its
                # own, filling the unmatched stretch.
                prev = max([j for j, x in enumerate(segs) if x["end"] <= c["time"]] or [-1])
                lo = segs[prev]["end"] if prev >= 0 else 0.0
                hi = segs[prev + 1]["start"] if prev + 1 < len(segs) else dur
                ok = c["code"] in expected or not expected or set(expected) <= confirmed
                new = {"start": lo, "end": hi, "code": c["code"], "score": c["score"],
                       "margin": 0, "alternatives": [], "title_card": c,
                       "confidence": "high" if ok else "conflict",
                       "evidence": "title+filename" if c["code"] in expected else "title",
                       "why": "" if ok else f"title card reads '{c['text']}' ({c['code']}) where the "
                                            f"dialogue matched nothing; the filename says "
                                            f"{', '.join(expected)}"}
                segs.insert(prev + 1, new)
                if c["code"] in expected:
                    confirmed.add(c["code"])
                notes.append(f"title card '{c['text']}' → {c['code']} where the dialogue matched nothing")
                continue
            if sg.get("title_card"):
                first = sg["title_card"]
                lead = min(120.0, max(5.0, float(first["time"]) - float(sg["start"])))
                cut = float(c["time"]) - lead
                # The new episode runs to the next stretch of dialogue (or the end of the
                # file) — including any unmatched stretch right after this one.
                piece_end = segs[idx + 1]["start"] if idx + 1 < len(segs) else dur or sg["end"]
                if first.get("code") == c["code"] or cut - sg["start"] < 240 or piece_end - cut < 120:
                    continue  # same title again, or too close to the first card to be another episode
                # A second, different title inside one stretch of dialogue: the dialogue
                # matched one episode across both (e.g. neither has a reference), so the
                # card marks where the next episode starts. Split the segment there; the
                # new part starts from what the dialogue said, before the first card.
                o = originals.get(id(sg), sg)
                piece = {k: v for k, v in o.items() if k not in (
                    "title_card", "dialogue_code", "evidence", "title_conflict", "why", "no_ref_for")}
                piece.update(start=round(cut, 1), end=round(max(piece_end, sg["end"]), 1))
                if o.get("no_ref_for"):
                    piece["no_ref_for"] = pos_expected or o["no_ref_for"]
                sg["end"] = round(cut, 1)
                segs.insert(idx + 1, piece)
                idx, sg = idx + 1, piece
            sg["title_card"] = c
            old = sg["code"]
            alts = {a["code"] for a in sg.get("alternatives") or []}
            if c["code"] in expected:
                confirmed.add(c["code"])
            if c["code"] == old:
                sg.update(confidence="high", evidence="dialogue+title", why="")
            elif c["code"] in expected:
                if old in expected:
                    why = (f"the dialogue matched {old} through this part as well; title card and "
                           f"filename both say {c['code']}")
                    notes.append(f"title card '{c['text']}' confirms {c['code']} (filename)")
                elif sg.get("no_ref_for") == c["code"]:
                    why = (f"no reference for {c['code']} yet, so the dialogue matched the closest "
                           f"other episode ({old}); title card and filename both say {c['code']}")
                    notes.append(f"title card '{c['text']}' confirms the filename ({c['code']} "
                                 f"has no reference; dialogue's closest was {old})")
                else:
                    why = (f"dialogue matched {old}; title card and filename both say "
                           f"{c['code']} — the reference for {old} may be wrong")
                    notes.append(f"title card '{c['text']}' confirms the filename; dialogue said {old} "
                                 f"(check the reference for {old})")
                sg.update(dialogue_code=old, code=c["code"], confidence="high",
                          evidence="title+filename", why=why)
            elif sg.get("confidence") != "high" and (c["code"] in alts or not expected
                                                     or set(expected) <= confirmed):
                # Weak dialogue, and the card either is one of its runner-up matches or names
                # an extra episode in a file whose named episodes are all accounted for.
                sg.update(dialogue_code=old, code=c["code"], confidence="high",
                          evidence="title", why="")
                notes.append(f"weak dialogue match{' ' + old if old else ''} settled by title card")
            else:
                # The card alone disagrees with the filename and with what the dialogue
                # points to: don't let it decide by itself.
                sg.update(confidence="conflict", title_conflict=c["code"],
                          why=f"title card reads '{c['text']}' ({c['code']}) but "
                              + (f"dialogue matches {old}" if sg.get("confidence") == "high" else
                                 f"the filename says {', '.join(expected) or '—'} and the dialogue "
                                 f"is unclear ({old or '—'})"))
                notes.append(f"CONFLICT: title card says {c['code']}, dialogue {old or '—'}, "
                             f"filename {', '.join(expected) or '—'}")
        # A weak stretch of dialogue right after a card-confirmed episode, naming an
        # episode the filename doesn't, is part of that episode (as long as it fits in
        # the episode's runtime) — not a separate episode to cut out.
        absorbed: list[dict] = []
        for sg in segs:
            prev = absorbed[-1] if absorbed else None
            if (prev and prev.get("title_card") and prev.get("confidence") == "high"
                    and not sg.get("title_card") and sg.get("confidence") == "low"
                    and sg["code"] not in expected and sg["code"] != prev["code"]
                    and (len(expected) < 2 or _expected_at(
                        expected, (sg["start"] + sg["end"]) / 2, dur) in (None, prev["code"]))):
                rt_min = (runtimes or {}).get(prev["code"])
                if not rt_min or sg["end"] - prev["start"] <= rt_min * 60 * 1.4:
                    prev["end"] = sg["end"]
                    prev.setdefault("absorbed", []).append(
                        {"start": sg["start"], "end": sg["end"], "code": sg["code"],
                         "score": sg.get("score")})
                    notes.append(f"weak {sg['code']} match at {_fmt_t(sg['start'])}–{_fmt_t(sg['end'])} "
                                 f"kept with {prev['code']} (title card)")
                    continue
            absorbed.append(sg)
        merged: list[dict] = []
        for sg in absorbed:
            if merged and merged[-1]["code"] == sg["code"] and \
                    merged[-1].get("confidence") == sg.get("confidence"):
                merged[-1]["end"] = sg["end"]
            else:
                merged.append(sg)
        segs = merged
    f["segments"] = segs
    f.pop("hold", None)
    f["status"], note = classify(expected, segs, f.get("text_source") or "none")
    detected = {sg["code"] for sg in segs}
    if f["status"] == "MISMATCH" and expected and detected < set(expected):
        # Cards confirm part of the filename and nothing contradicts the rest: there is
        # just no evidence for the rest (no reference, card not found). Leave it alone.
        missing = [c for c in expected if c not in detected]
        f["status"], f["hold"] = "LOW_CONFIDENCE", True
        where = ", ".join(f"{_fmt_t(a)}–{_fmt_t(b)}" for a, b in f.get("title_scan") or [])
        notes.append(f"no evidence either way for {', '.join(missing)} — left as named"
                     + (f" (no title card read for it; looked at {where})" if where else ""))
    if notes and f["status"] != "LOW_CONFIDENCE":
        note = "; ".join(notes)
    elif notes and f["status"] == "LOW_CONFIDENCE" and not note.startswith("At least"):
        note = "; ".join(notes)
    f["note"] = note
    return notes


def _titlecard_pass(series: dict, files: list[dict], eps: dict, ctx, reread: bool = False) -> None:
    s = get_settings()
    opts = dict(series.get("options") or {})
    mode = opts.get("title_cards", "auto")
    if not s["titlecard_enabled"] or mode == "off" or not files:
        return
    if not titlecard.available():
        ctx.log("Title cards: OCR engine not installed in this image — skipped.")
        return
    try:
        acc = titlecard.acceleration_status()
        ctx.log(f"Title cards: video decode on {acc['decode'].upper()}"
                + (f" ({acc['decode_note']})" if acc["decode_note"] else ""))
    except Exception:  # noqa: BLE001
        pass
    root = Path(series["path"])
    status0 = dict(opts.get("title_cards_status") or {})
    rec0 = status0.get("recurring")
    recurring: dict = dict(rec0) if isinstance(rec0, dict) and not reread else {}
    index = titlecard.TitleIndex(series["episodes"], bool(opts.get("include_specials")), recurring)
    runtimes = sorted(e["runtime"] for e in eps.values() if e.get("runtime") and e["season"] > 0)
    runtime = runtimes[len(runtimes) // 2] if runtimes else None
    rt = {c: e["runtime"] for c, e in eps.items() if e.get("runtime")}
    status = dict(opts.get("title_cards_status") or {})
    if not isinstance(status.get("recurring", {}), dict):
        # Found by an earlier version, which didn't record where the text appears:
        # check again.
        status.pop("recurring", None)
        status.pop("recurring_checked", None)
    force = 0.0
    if reread:
        # Re-read the videos instead of reusing cached OCR, and redo the auto test
        # and learned position, since both came from the old reads.
        force = time.time()
        if mode == "auto":
            status = {}
        else:
            for k in ("window", "recurring", "recurring_checked"):
                status.pop(k, None)
        ctx.log("Title cards: re-reading from the video files (cached OCR ignored).")
    scope_all = opts.get("title_cards_scope") == "all"
    done: set[str] = set()
    hit_times: list[float] = []

    def run(f: dict, learned) -> list[dict]:
        searched: list = []
        cards = titlecard.detect(root / f["rel"], float(f.get("duration") or 0), f["segments"],
                                 index, learned, runtime, ctx, force, len(f.get("expected") or []),
                                 searched)
        f["title_scan"] = _merge_windows(searched)
        for c in cards:
            starts = titlecard.episode_starts(float(f.get("duration") or 0), f["segments"], runtime,
                                              len(f.get("expected") or []))
            base = max([x for x in starts if x <= c["time"]] or [0.0])
            hit_times.append(c["time"] - base)
        return cards

    def learn() -> list | None:
        if len(hit_times) < 2:
            return status.get("window")
        ts = sorted(hit_times)
        med = ts[len(ts) // 2]
        return [max(0.0, round(med - 40)), round(med + 60)]

    def report(f, cards, notes):
        full = {c.get("code") for c in cards if not c.get("partial")} | {
            sg["code"] for sg in f.get("segments") or [] if sg.get("title_card")}
        looked = ", ".join(f"{_fmt_t(a)}–{_fmt_t(b)}" for a, b in f.get("title_scan") or [])
        short = len(full) < len(f.get("expected") or [1])
        where = f" (looked at {looked})" if looked and short and not any(
            "looked at" in n for n in notes) else ""
        if cards:
            desc = ", ".join(
                f"'{c['text']}' @{_fmt_t(c['time'])} → " + (c["code"] if not c.get("partial") else
                f"partial read, could be {', '.join((c.get('candidates') or [])[:4])}"
                + ("…" if len(c.get("candidates") or []) > 4 else "")) for c in cards)
            ctx.log(f"  title card {f['rel']}: {desc}{where}" + (f" — {'; '.join(notes)}" if notes else ""))
        elif looked:
            ctx.log(f"  title card {f['rel']}: none read (looked at {looked})")

    if "recurring_checked" not in status:
        # Text from the opening titles can look like an episode title (Bluey's intro
        # names "BINGO" in every episode, and "Bingo" is an episode). A title read in
        # most of a sample of files whose names say otherwise is that kind of text:
        # from then on only an exact read of it counts. Checked once per series.
        sample = [f for f in files if f["status"] != "ERROR" and f.get("expected")][:8]
        if len(sample) >= 3:
            ctx.log(f"Title cards: checking {len(sample)} files for text that appears in every "
                    "episode (opening titles)…")
            seen: dict[str, set] = {}
            texts: dict[str, set] = {}
            at: dict[str, list] = {}
            for n, f in enumerate(sample):
                ctx.check_cancel()
                ctx.progress(n / len(sample), f"Title cards (opening titles): {Path(f['rel']).name}")
                dur = float(f.get("duration") or 0)
                starts = titlecard.episode_starts(dur, f["segments"], runtime, len(f["expected"]))
                for c in run(f, None):
                    if not c.get("partial") and c.get("code") and c["code"] not in f["expected"]:
                        seen.setdefault(c["code"], set()).add(f["rel"])
                        texts.setdefault(c["code"], set()).add(c["text"])
                        base = max([x for x in starts if x <= c["time"]] or [0.0])
                        at.setdefault(c["code"], []).append(c["time"] - base)
            new = {code for code, rels in seen.items()
                   if len(rels) >= 3 and len(rels) >= len(sample) / 2} - set(recurring)
            if new:
                for code in new:
                    ts = sorted(at[code])
                    recurring[code] = round(ts[len(ts) // 2], 1)
                index = titlecard.TitleIndex(series["episodes"], bool(opts.get("include_specials")),
                                             recurring)
                status.pop("window", None)  # learned from the opening titles' position
                for code in sorted(new):
                    ctx.log(f"Title cards: '{sorted(texts[code])[0]}' shows in {len(seen[code])} of "
                            f"{len(sample)} files about {_fmt_t(recurring[code])} into the episode — "
                            f"opening titles, not the title of {code}. Ignored there; a {code} "
                            "title card anywhere else still counts.")
            hit_times.clear()
            status["recurring"] = recurring
            status["recurring_checked"] = True
            opts["title_cards_status"] = status
            db.update_series(series["id"], options=opts)

    if mode == "auto" and "has_cards" not in status:
        pool = [f for f in files if f["status"] == "OK"][:6]
        if len(pool) < 4:  # few confirmed files: top up with others so the test means something
            pool += [f for f in files if f["status"] not in ("OK", "ERROR")][:6 - len(pool)]
        ctx.log(f"Title cards: checking {len(pool)} files to see whether this series shows "
                "episode titles on screen…")
        hits = 0
        for n, f in enumerate(pool):
            ctx.check_cancel()
            ctx.progress(n / max(1, len(pool)), f"Title cards (probe): {Path(f['rel']).name}")
            confirmed = {sg["code"] for sg in f["segments"] if sg.get("confidence") == "high"}
            cards = run(f, None)
            # A confirmed file's card must agree with it; for unconfirmed files any
            # readable episode title counts as "this show has title cards".
            if cards and (f["status"] != "OK" or any(c["code"] in confirmed for c in cards)):
                hits += 1
            report(f, cards, _apply_title_cards(f, cards, rt))
            done.add(f["rel"])
        has = bool(pool) and hits >= max(2, (len(pool) + 1) // 2)
        status = {"has_cards": has, "probed": len(pool), "hits": hits,
                  **{k: status[k] for k in ("recurring", "recurring_checked") if k in status}}
        if has:
            status["window"] = learn()
        opts["title_cards_status"] = status
        db.update_series(series["id"], options=opts)
        if has:
            w = status.get("window")
            ctx.log(f"Title cards: found on {hits}/{len(pool)} probe files"
                    + (f" (usually {_fmt_t(w[0])}–{_fmt_t(w[1])} into an episode)" if w else "")
                    + " — using them.")
        else:
            ctx.log(f"Title cards: found on {hits}/{len(pool)} probe files — not used for this "
                    "series. (Force them on under Series options.)")
            return
    elif mode == "auto" and not status.get("has_cards"):
        return

    todo = [f for f in files if f["rel"] not in done and f["status"] != "ERROR"
            and (scope_all or f["status"] != "OK")]
    if not todo:
        return
    ctx.log(f"Title cards: checking {len(todo)} files"
            + ("" if scope_all else " the dialogue match didn't already confirm") + ".")
    use_vision = llm.vision_available()
    for n, f in enumerate(todo):
        ctx.check_cancel()
        ctx.progress(n / len(todo), f"Title cards: {Path(f['rel']).name}")
        cards = run(f, learn())
        if not cards and use_vision:
            cards = _vision_card(root / f["rel"], f, index, series["name"], ctx)
        report(f, cards, _apply_title_cards(f, cards, rt))
    if titlecard._accel.get("ocr_gpu_error") and not titlecard._accel.get("gpu_error_logged"):
        titlecard._accel["gpu_error_logged"] = True
        ctx.log(f"Title cards: GPU OCR failed, using the CPU instead — {titlecard._accel['ocr_gpu_error']}")
    if mode == "on" or status.get("has_cards"):
        w = learn()
        if w and w != status.get("window"):
            status["window"] = w
            opts = dict(db.get_series(series["id"])["options"] or {})
            opts["title_cards_status"] = status
            db.update_series(series["id"], options=opts)


def _vision_card(video: Path, f: dict, index, series_name: str, ctx) -> list[dict]:
    full = float(get_settings()["titlecard_scan_seconds"])
    dur = float(f.get("duration") or 0)
    frames = titlecard.vision_frames(video, 0.0, min(full, max(10.0, dur)))
    if not frames:
        return []
    try:
        text = llm.read_title_card([b for _, b in frames], series_name)
    except Exception as e:  # noqa: BLE001
        ctx.log(f"  vision model error: {e}")
        return []
    r = index.read(text) if text else None
    if not r:
        return []
    return [{**r, "text": f"{text} (vision)", "time": frames[0][0], "frames": 1}]


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
