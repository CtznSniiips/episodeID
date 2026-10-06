"""Title-card detection: read the episode title off the screen.

Frames are sampled with ffmpeg (default 1 per second) from a window near the
start of the file — and near the start of each later episode in multi-episode
files — near-duplicate frames are dropped, and the rest are run through
RapidOCR (PaddleOCR models on ONNX Runtime, CPU, bundled with the package so
nothing is downloaded at run time).

A frame counts as a title card when large on-screen text closely matches one of
the series' TVDB episode titles and no other title comes close. Because this
identifies episodes by *title*, it doesn't depend on references or on how any
site numbers the episodes — it is independent evidence next to the dialogue match.

The raw OCR output per frame is cached, so changing titles or thresholds never
requires re-reading video.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .config import CACHE_DIR, get_settings
from .media_text import _cache_key
from .references import norm_title

_ocr = None
_ocr_lock = threading.Lock()

FRAME_W = 640           # OCR works on frames scaled to this width
FRAME_H = 360           # …and roughly this height (16:9)
MIN_BOX_FRAC = 0.035    # lines this tall (fraction of frame height) can be part of a title…
MIN_TITLE_FRAC = 0.045  # …but every read needs at least one line this tall (ignores small signs)
MIN_CONF = 0.75         # OCR confidence for a line to be considered
CARD_SETTLE = 6         # keep reading this many seconds after a card first appears…
CARD_SETTLE_PARTIAL = 12  # …or this long if only part of a title has been read so far


class TitleCardUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


_accel: dict = {}  # what's actually in use, for the UI and logs


def _ocr_gpu_available() -> bool:
    try:
        import onnxruntime as ort
        if hasattr(ort, "preload_dlls"):
            try:  # CUDA/cuDNN from NVIDIA's pip wheels (CUDA image)
                ort.preload_dlls(cuda=True, cudnn=True, msvc=False)
            except Exception:  # noqa: BLE001
                pass
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:  # noqa: BLE001
        return False


def _engine():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as e:  # noqa: BLE001
            raise TitleCardUnavailable(f"OCR engine not installed ({e})")
        want = get_settings()["titlecard_ocr_device"]
        gpu = want != "cpu" and not _accel.get("force_cpu") and _ocr_gpu_available()
        # Frames are already small (640x360). RapidOCR's default upscales them to a
        # 736 px short side before text detection, which more than doubles the work
        # and doesn't help with large title text.
        kw = {"det_limit_side_len": FRAME_H, "det_limit_type": "min"}
        if gpu:
            kw.update(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        _ocr = RapidOCR(**kw)
        _accel["ocr"] = "NVIDIA GPU (CUDA)" if gpu else "CPU"
    return _ocr


# ------------------------------------------------------------- video decode

def _hwaccels() -> set[str]:
    if "ffmpeg_hwaccels" not in _accel:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"], capture_output=True,
                               text=True, timeout=20)
            _accel["ffmpeg_hwaccels"] = {l.strip() for l in r.stdout.splitlines()[1:] if l.strip()}
        except Exception:  # noqa: BLE001
            _accel["ffmpeg_hwaccels"] = set()
    return _accel["ffmpeg_hwaccels"]


def _render_node() -> str | None:
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []
    return str(nodes[0]) if nodes else None


def decode_method() -> str:
    """'cuda', 'vaapi' or 'cpu' — what ffmpeg will use to decode video for title cards."""
    if _accel.get("decode_failed"):
        return "cpu"
    want = get_settings()["titlecard_hwaccel"]
    have = _hwaccels()
    if want == "off":
        return "cpu"
    nvidia = Path("/dev/nvidia0").exists() or Path("/dev/nvidiactl").exists()
    if want in ("auto", "cuda") and "cuda" in have and nvidia:
        return "cuda"
    if want in ("auto", "vaapi") and "vaapi" in have and _render_node():
        return "vaapi"
    return "cpu"


def _hw_args(method: str) -> list[str]:
    if method == "cuda":
        return ["-hwaccel", "cuda"]
    if method == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", _render_node() or "/dev/dri/renderD128"]
    return []


# --------------------------------------------------------------- matching

def _ns(t: str) -> str:
    """Normalised title without spaces — OCR often drops them ("SAVETHE", "MIGHTYPUPS")."""
    return norm_title(t).replace(" ", "")


class TitleIndex:
    """Fuzzy lookup of on-screen text against a series' episode titles."""

    def __init__(self, episodes: list[dict], include_specials: bool = False):
        self.titles: list[dict] = []
        for e in episodes:
            if e["season"] == 0 and not include_specials:
                continue
            n = norm_title(e.get("title") or "")
            if len(n) >= 2 and not re.fullmatch(r"episode \d+", n):
                self.titles.append({"code": f"S{e['season']:02d}E{e['episode']:02d}",
                                    "title": e["title"], "ns": n.replace(" ", "")})

    def read(self, text: str) -> dict | None:
        """What this on-screen text says about the episode.
        A *full* read clearly matches one title. A *partial* read is the beginning of
        longer titles — a card still animating in ("PUPS SAVE RYDER'S" → "…Surprise"),
        or a banner shared by many titles ("MIGHTY PUPS") — and only lists candidates."""
        c = _ns(text)
        if len(c) < 3:
            return None
        scored = sorted(((difflib.SequenceMatcher(None, c, t["ns"]).ratio(), i)
                         for i, t in enumerate(self.titles)
                         if abs(len(t["ns"]) - len(c)) <= max(4, len(t["ns"]) // 2)), reverse=True)
        # Titles this text could be the beginning of. Allow about one OCR slip per dozen
        # characters — "PUPS MAKETHE" must not count as the start of "Pups Save the…".
        need_prefix = 1 - max(1, len(c) // 12) / len(c)
        longer = [t for t in self.titles if len(t["ns"]) >= len(c) + 3
                  and difflib.SequenceMatcher(None, c, t["ns"][:len(c)]).ratio() >= need_prefix]
        top = self.titles[scored[0][1]] if scored else None
        score = scored[0][0] if scored else 0.0
        second = next((sc for sc, i in scored[1:] if self.titles[i]["ns"] != top["ns"]), 0.0) \
            if top else 0.0
        need = 0.9 if top and len(top["ns"]) <= 8 else 0.85
        full = top is not None and score >= need and score - second >= 0.06
        if full:
            # The matched title is itself how other titles begin ("Mighty Pups" →
            # "Mighty Pups Stop the…"): a banner, or a card still animating in — even if
            # OCR added junk in front ("AW MIGHTYPUPS").
            longer += [t for t in self.titles if len(t["ns"]) >= len(top["ns"]) + 3
                       and t["ns"].startswith(top["ns"]) and t not in longer]
        if full and not longer:
            return {"code": top["code"], "title": top["title"], "text": text,
                    "score": round(score, 3), "partial": False}
        if longer and len(c) >= 6 and len(longer) <= 25:
            cands = ([top["code"]] if full else []) + [t["code"] for t in longer]
            return {"code": top["code"] if full else None, "title": top["title"] if full else None,
                    "text": text, "score": round(score, 3), "partial": True,
                    "candidates": list(dict.fromkeys(cands))}
        return None

    def match_frame(self, lines: list[list]) -> dict | None:
        """lines: [[text, conf, height_fraction, y_fraction], ...] for one frame."""
        # Lines without a real word (counters, clocks, "214", "09:09:09") never belong to
        # a title and would hide that a read is only the start of a longer title.
        big = [l for l in lines if l[1] >= MIN_CONF and l[2] >= MIN_BOX_FRAC
               and re.search(r"[^\W\d_]{2,}", l[0])]
        if not big:
            return None
        big.sort(key=lambda l: l[3])
        # Titles often wrap ("THE" / "VOICE", "RESCUE WHEELS" / "PUPS" / "SAVE THE" / …):
        # try single lines and runs of consecutive lines.
        cands = []
        for i in range(len(big)):
            for j in range(i + 1, min(i + 5, len(big)) + 1):  # cards can span 4–5 lines
                if max(l[2] for l in big[i:j]) >= MIN_TITLE_FRAC:
                    cands.append(" ".join(l[0] for l in big[i:j]))
        reads = [r for r in (self.read(c) for c in dict.fromkeys(cands)) if r]
        if not reads:
            return None
        # Prefer complete reads, then the most specific (longest) text.
        reads.sort(key=lambda r: (not r["partial"], len(_ns(r["text"])), r["score"]), reverse=True)
        return reads[0]


# --------------------------------------------------------------- sampling

def _sample(video: Path, start: float, length: float, fps: float,
            mode: str = "full") -> list[tuple[float, "object"]]:
    """Frames at `fps` from [start, start+length].
    mode "key":  decode keyframes only — ~8x faster; title cards almost always begin
                 on a cut, where encoders place a keyframe.
    mode "full": decode everything except B-frames (same frames out, ~1/3 less work)."""
    import cv2
    skip = ["-skip_frame", "nokey"] if mode == "key" else ["-skip_frame", "bidir"]
    method = decode_method()
    for attempt_method in ([method, "cpu"] if method != "cpu" else ["cpu"]):
        tmp = Path(tempfile.mkdtemp(prefix="tc_", dir=str(CACHE_DIR)))
        try:
            cmd = (["ffmpeg", "-nostdin", "-v", "error"] + _hw_args(attempt_method) + skip +
                   ["-ss", f"{max(0.0, start):.2f}", "-t", f"{length:.2f}", "-i", str(video),
                    "-map", "0:v:0", "-an", "-sn", "-vf", f"fps={fps},scale={FRAME_W}:-2",
                    "-q:v", "3", str(tmp / "%05d.jpg")])
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            files = sorted(tmp.glob("*.jpg"))
            if attempt_method != "cpu" and (r.returncode != 0 or not files):
                # Hardware decode not usable here (no device passed through, unsupported
                # codec, missing driver…): use the CPU from now on.
                err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
                why = next((l for l in err if re.search(r"(?i)cuda|vaapi|va_|device|hwaccel|driver", l)),
                           err[0] if err else "no frames produced")
                _accel["decode_failed"] = f"{attempt_method}: {why.strip()[:200]}"
                continue
            _accel["decode"] = attempt_method
            frames = []
            for i, f in enumerate(files):
                img = cv2.imread(str(f))
                if img is not None:
                    frames.append((round(max(0.0, start) + i / fps, 2), img))
            return frames
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return []


def error_summary(e: BaseException) -> str:
    """The informative part of an error. RapidOCR wraps ONNX Runtime failures in a
    full traceback string; the cause is on its last lines, not the first."""
    lines = [l.strip() for l in str(e).splitlines() if l.strip()]
    if not lines:
        return type(e).__name__
    last = re.sub(r"^[\w.]*?(\w+(Error|Exception|Fail)):\s*", r"\1: ", lines[-1])
    return last[-400:]


def _ocr_frame(img) -> list[list]:
    global _ocr
    h, w = img.shape[:2]
    with _ocr_lock:
        try:
            res, _ = _engine()(img, use_cls=False)
        except Exception as e:  # noqa: BLE001
            if _accel.get("ocr", "").startswith("NVIDIA"):
                # GPU OCR loaded but can't run here (driver/GPU too old for the bundled
                # CUDA libraries, a missing library, out of memory…): use the CPU.
                _accel["ocr_gpu_error"] = error_summary(e)
                _accel["force_cpu"] = True
                _ocr = None
                res, _ = _engine()(img, use_cls=False)
            else:
                raise TitleCardUnavailable(f"OCR failed: {error_summary(e)}") from e
    out = []
    for box, text, conf in res or []:
        ys = [p[1] for p in box]
        out.append([str(text).strip(), round(float(conf), 3),
                    round((max(ys) - min(ys)) / h, 3), round(sum(ys) / len(ys) / h, 3)])
    return [l for l in out if l[0]]


def _cache_path(video: Path, start: float, length: float, fps: float,
                mode: str = "full") -> Path:
    tag = f"v2|{start:.0f}|{length:.0f}|{fps}|{mode}"
    spec = hashlib.sha1(tag.encode()).hexdigest()[:10]
    return CACHE_DIR / "titlecards" / f"{_cache_key(video)}_{spec}.json"


def scan_window(video: Path, start: float, length: float, index: TitleIndex,
                ctx=None, stop_on_hit: bool = True, force: float = 0) -> list[dict]:
    """OCR one window of the video. Returns per-frame records [{t, lines, hit}].
    Keyframes are read first (fast); the window is only fully decoded if that
    finds no title card. force=<timestamp> ignores OCR cached before that time
    ("re-read title cards": each window is read once per job, not once per call)."""
    frames = _scan_pass(video, start, length, index, ctx, stop_on_hit, "key", force)
    if any(fr.get("hit") and not fr["hit"]["partial"] for fr in frames):
        return frames
    full = _scan_pass(video, start, length, index, ctx, stop_on_hit, "full", force)
    return full if any(fr.get("hit") for fr in full) else frames


def _settle(full_seen: bool) -> float:
    return CARD_SETTLE if full_seen else CARD_SETTLE_PARTIAL


def _reuse_cached(data: dict, index: TitleIndex, fps: float) -> list[dict] | None:
    """Cached frames re-matched with the current rules, or None if they don't cover
    what the current rules need (a scan stopped early, after a card that is now read
    differently or now needs longer to settle)."""
    frames = data["frames"]
    for fr in frames:
        fr["hit"] = index.match_frame(fr["lines"])
    if data.get("complete", True):
        return frames
    first, full_seen = None, False
    for i, fr in enumerate(frames):
        if first is not None and fr["t"] > first + _settle(full_seen):
            return frames[:i]  # the current rules would have stopped here
        if fr["hit"]:
            first = fr["t"] if first is None else first
            full_seen = full_seen or not fr["hit"]["partial"]
    if first is None:
        return None  # stopped on a frame that no longer counts as a card
    # Where the old scan stopped (older caches: the sample after the last stored frame).
    stopped = data.get("stopped_at", frames[-1]["t"] + 1.0 / max(fps, 0.01))
    return frames if stopped > first + _settle(full_seen) else None


def _scan_pass(video: Path, start: float, length: float, index: TitleIndex,
               ctx, stop_on_hit: bool, mode: str, force: float = 0) -> list[dict]:
    fps = float(get_settings()["titlecard_fps"])
    cache = _cache_path(video, start, length, fps, mode)
    if cache.exists() and not (force and cache.stat().st_mtime < force):
        frames = _reuse_cached(json.loads(cache.read_text()), index, fps)
        if frames is not None:
            return frames

    import cv2
    raw = _sample(video, start, length, fps, mode)
    frames, last_thumb = [], None
    complete, stopped_at = True, None
    first_hit_t, full_seen = None, False
    for t, img in raw:
        if ctx:
            ctx.check_cancel()
        if first_hit_t is not None and stop_on_hit and t > first_hit_t + _settle(full_seen):
            complete, stopped_at = False, t  # the card has had time to finish animating in
            break
        thumb = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 18)).astype("int16")
        if first_hit_t is None and last_thumb is not None and abs(thumb - last_thumb).mean() < 3:
            continue  # same shot as the previous OCR'd frame (not skipped once a card is showing)
        last_thumb = thumb
        lines = _ocr_frame(img)
        hit = index.match_frame(lines)
        frames.append({"t": t, "lines": lines, "hit": hit})
        if hit:
            first_hit_t = t if first_hit_t is None else first_hit_t
            full_seen = full_seen or not hit["partial"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"complete": complete, "stopped_at": stopped_at,
                                 "frames": [{"t": f["t"], "lines": f["lines"]} for f in frames]}))
    return frames


def cards_from_frames(frames: list[dict]) -> list[dict]:
    """Group frames showing text into card appearances (frames within 12 s of each
    other) and keep the best reading of each: a complete read if there is one —
    the most frequent title, then the most specific text — otherwise the most
    specific partial read with its candidate titles."""
    groups: list[list[dict]] = []
    for fr in frames:
        if not fr.get("hit"):
            continue
        if groups and fr["t"] - groups[-1][-1]["t"] <= 12:
            groups[-1].append(fr)
        else:
            groups.append([fr])
    cards = []
    for g in groups:
        hits = [fr["hit"] for fr in g]
        full = [h for h in hits if not h["partial"]]
        if full:
            counts: dict[str, int] = {}
            for h in full:
                counts[h["code"]] = counts.get(h["code"], 0) + 1
            best = max(full, key=lambda h: (counts[h["code"]], len(_ns(h["text"])), h["score"]))
        else:
            best = max(hits, key=lambda h: (len(_ns(h["text"])), -len(h.get("candidates") or [])))
        cards.append({**best, "time": g[0]["t"], "last": g[-1]["t"], "frames": len(g)})
    return cards


def episode_starts(duration: float, segments: list[dict],
                   runtime_min: float | None, n_expected: int = 0) -> list[float]:
    """Where each episode in the file probably begins: at 0, where the dialogue
    switches episode, where a stretch of unmatched dialogue begins (an episode with no
    reference matches nothing), and where the filename's episodes would start
    ("S09E31E32" → half way)."""
    starts = [0.0]
    for sg in segments[1:]:
        starts.append(float(sg.get("start", 0)) - 20)
    for a, b in zip(segments, segments[1:] + [{"start": duration}]):
        if float(b["start"]) - float(a.get("end", 0)) >= 60 and float(a.get("end", 0)) < duration - 120:
            starts.append(float(a["end"]) - 20)
    if n_expected > 1 and duration > 0:
        starts += [duration * k / n_expected - 90 for k in range(1, n_expected)]
    elif len(segments) <= 1 and runtime_min and duration > 1.6 * runtime_min * 60:
        starts.append(duration / 2 - 90)  # likely a second episode, no dialogue split
    out: list[float] = []
    for x in sorted(round(max(0.0, x), 1) for x in starts):
        if x < duration and not (out and x - out[-1] < 45):  # scan windows overlap anyway
            out.append(x)
    return out


def detect(video: Path, duration: float, segments: list[dict], index: TitleIndex,
           learned_window: list | None = None, runtime_min: float | None = None,
           ctx=None, force: float = 0, n_expected: int = 0,
           searched: list | None = None) -> list[dict]:
    """Title cards found in the file: [{code, title, text, score, time, frames}].
    For each probable episode start, look in the window learned for this series
    first (fast), then the full window if nothing turned up.
    searched: if given, receives the [start, end] windows that were read (for the log)."""
    full = float(get_settings()["titlecard_scan_seconds"])
    found: list[dict] = []
    log = searched if searched is not None else []

    def scan(a: float, length: float) -> list[dict]:
        a, length = max(0.0, a), min(length, max(10.0, duration - max(0.0, a)))
        log.append([round(a), round(a + length)])
        return cards_from_frames(scan_window(video, a, length, index, ctx, force=force))

    def add(cards):
        for c in cards:
            if not any(c.get("code") == f.get("code") and c.get("text") == f.get("text")
                       and abs(c["time"] - f["time"]) < 30 for f in found):
                found.append(c)

    for s in episode_starts(duration, segments, runtime_min, n_expected):
        cards = []
        if learned_window:
            a, b = learned_window
            cards = scan(s + a, b - a)
        if not any(not c["partial"] for c in cards):
            more = scan(s, full)
            cards = more if more else cards
        add(cards)
    found.sort(key=lambda c: c["time"])
    return found


def vision_frames(video: Path, start: float, length: float,
                  k: int = 4) -> list[tuple[float, bytes]]:
    """JPEGs of the frames with the most large text — for a vision model to read."""
    import cv2
    fps = float(get_settings()["titlecard_fps"])
    cache = _cache_path(video, start, length, fps)
    if not cache.exists():
        return []
    frames = json.loads(cache.read_text())["frames"]
    scored = sorted(((sum(l[2] for l in fr["lines"] if l[2] >= MIN_BOX_FRAC / 2), fr["t"])
                     for fr in frames), reverse=True)
    out = []
    for area, t in scored[:k]:
        if area <= 0:
            break
        got = _sample(video, t, 1.0 / fps, fps)
        if got:
            ok, buf = cv2.imencode(".jpg", got[0][1], [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                out.append((t, buf.tobytes()))
    return out


def gpu_info() -> str | None:
    """'Quadro P2000 (compute 6.1)' for the first NVIDIA GPU, if one is visible."""
    if "gpu_info" not in _accel:
        info = None
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,driver_version",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                name, cap, drv = [x.strip() for x in r.stdout.splitlines()[0].split(",")[:3]]
                info = f"{name} (compute {cap}, driver {drv})"
                try:
                    if float(cap) < 7.5:
                        info += " — pre-Turing GPU"
                except ValueError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        _accel["gpu_info"] = info
    return _accel["gpu_info"]


def acceleration_status(benchmark: bool = False) -> dict:
    """What title-card detection runs on, and optionally how fast OCR is."""
    out = {"decode": decode_method(), "decode_note": "", "ocr": None, "ocr_ms": None}
    if _accel.get("decode_failed"):
        out["decode_note"] = "hardware decode failed earlier, using CPU: " + _accel["decode_failed"][-160:]
    if benchmark:
        import time
        import numpy as np
        import cv2
        img = np.full((FRAME_H, FRAME_W, 3), 40, np.uint8)
        cv2.putText(img, "THE TEST CARD", (60, 200), cv2.FONT_HERSHEY_DUPLEX, 2.0, (255, 255, 255), 5)
        _ocr_frame(img)  # load models / warm up
        t = time.time()
        for _ in range(3):
            _ocr_frame(img)
        out["ocr_ms"] = round((time.time() - t) / 3 * 1000)
    out["gpu"] = gpu_info()
    out["ocr"] = _accel.get("ocr")
    if _accel.get("ocr_gpu_error"):
        out["ocr"] = f"{out['ocr']} (GPU OCR failed, fell back: {_accel['ocr_gpu_error']})"
    return out
