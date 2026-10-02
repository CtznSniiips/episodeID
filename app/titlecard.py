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
MIN_BOX_FRAC = 0.06     # title text must be at least this fraction of frame height
MIN_CONF = 0.75         # OCR confidence for a line to be considered


class TitleCardUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _engine():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as e:  # noqa: BLE001
            raise TitleCardUnavailable(f"OCR engine not installed ({e})")
        _ocr = RapidOCR()
    return _ocr


# --------------------------------------------------------------- matching

class TitleIndex:
    """Fuzzy lookup of on-screen text against a series' episode titles."""

    def __init__(self, episodes: list[dict], include_specials: bool = False):
        self.titles: list[tuple[str, str, str]] = []  # (code, title, normalised)
        for e in episodes:
            if e["season"] == 0 and not include_specials:
                continue
            n = norm_title(e.get("title") or "")
            if len(n) >= 2 and not re.fullmatch(r"episode \d+", n):
                self.titles.append((f"S{e['season']:02d}E{e['episode']:02d}", e["title"], n))

    def best(self, text: str) -> tuple[str, str, float, float] | None:
        """(code, title, score, runner_up_score) for the best-matching title."""
        n = norm_title(text)
        if len(n) < 2:
            return None
        scored = []
        for code, title, tn in self.titles:
            if abs(len(tn) - len(n)) > max(4, len(tn) // 2):
                continue
            scored.append((difflib.SequenceMatcher(None, n, tn).ratio(), code, title, tn))
        if not scored:
            return None
        scored.sort(reverse=True)
        top = scored[0]
        # runner-up must be a *different title* (two-part episodes share text)
        second = next((s[0] for s in scored[1:] if s[3] != top[3]), 0.0)
        return top[1], top[2], top[0], second

    def match_frame(self, lines: list[list]) -> dict | None:
        """lines: [[text, conf, height_fraction, y_fraction], ...] for one frame."""
        big = [l for l in lines if l[1] >= MIN_CONF and l[2] >= MIN_BOX_FRAC]
        if not big:
            return None
        big.sort(key=lambda l: l[3])
        # Titles often wrap ("THE" / "VOICE"): try single lines and runs of 2–3 lines.
        cands = []
        for i in range(len(big)):
            for j in range(i + 1, min(i + 3, len(big)) + 1):
                cands.append(" ".join(l[0] for l in big[i:j]))
        best = None
        for c in dict.fromkeys(cands):
            m = self.best(c)
            if not m:
                continue
            code, title, score, second = m
            need = 0.9 if len(norm_title(title)) <= 6 else 0.82
            if score >= need and score - second >= 0.08:
                if best is None or score > best["score"]:
                    best = {"code": code, "title": title, "text": c, "score": round(score, 3)}
        return best


# --------------------------------------------------------------- sampling

def _sample(video: Path, start: float, length: float, fps: float) -> list[tuple[float, "object"]]:
    import cv2
    tmp = Path(tempfile.mkdtemp(prefix="tc_", dir=str(CACHE_DIR)))
    try:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.2f}",
               "-t", f"{length:.2f}", "-i", str(video), "-map", "0:v:0", "-an", "-sn",
               "-vf", f"fps={fps},scale={FRAME_W}:-2", "-q:v", "3", str(tmp / "%05d.jpg")]
        subprocess.run(cmd, capture_output=True, timeout=900)
        frames = []
        for i, f in enumerate(sorted(tmp.glob("*.jpg"))):
            img = cv2.imread(str(f))
            if img is not None:
                frames.append((round(max(0.0, start) + i / fps, 2), img))
        return frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ocr_frame(img) -> list[list]:
    h, w = img.shape[:2]
    with _ocr_lock:
        res, _ = _engine()(img, use_cls=False)
    out = []
    for box, text, conf in res or []:
        ys = [p[1] for p in box]
        out.append([str(text).strip(), round(float(conf), 3),
                    round((max(ys) - min(ys)) / h, 3), round(sum(ys) / len(ys) / h, 3)])
    return [l for l in out if l[0]]


def _cache_path(video: Path, start: float, length: float, fps: float) -> Path:
    spec = hashlib.sha1(f"{start:.0f}|{length:.0f}|{fps}".encode()).hexdigest()[:10]
    return CACHE_DIR / "titlecards" / f"{_cache_key(video)}_{spec}.json"


def scan_window(video: Path, start: float, length: float, index: TitleIndex,
                ctx=None, stop_on_hit: bool = True) -> list[dict]:
    """OCR one window of the video. Returns per-frame records [{t, lines, hit}]."""
    fps = float(get_settings()["titlecard_fps"])
    cache = _cache_path(video, start, length, fps)
    if cache.exists():
        data = json.loads(cache.read_text())
        frames = data["frames"]
        for fr in frames:
            fr["hit"] = index.match_frame(fr["lines"])
        complete = data.get("complete", True)
        # A cached early-stopped scan is only reusable if its hit still matches.
        if complete or any(fr["hit"] for fr in frames):
            return frames

    import cv2
    raw = _sample(video, start, length, fps)
    frames, last_thumb = [], None
    complete = True
    for t, img in raw:
        if ctx:
            ctx.check_cancel()
        thumb = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 18)).astype("int16")
        if last_thumb is not None and abs(thumb - last_thumb).mean() < 3:
            continue  # same shot as the previous OCR'd frame
        last_thumb = thumb
        lines = _ocr_frame(img)
        hit = index.match_frame(lines)
        frames.append({"t": t, "lines": lines, "hit": hit})
        if hit and stop_on_hit:
            complete = False
            break
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"complete": complete,
                                 "frames": [{"t": f["t"], "lines": f["lines"]} for f in frames]}))
    return frames


def cards_from_frames(frames: list[dict]) -> list[dict]:
    """Collapse consecutive frames showing the same title into cards."""
    cards: list[dict] = []
    for fr in frames:
        h = fr.get("hit")
        if not h:
            continue
        if cards and cards[-1]["code"] == h["code"] and fr["t"] - cards[-1]["last"] <= 12:
            c = cards[-1]
            c["last"] = fr["t"]
            c["frames"] += 1
            if h["score"] > c["score"]:
                c.update(score=h["score"], text=h["text"])
        else:
            cards.append({**h, "time": fr["t"], "last": fr["t"], "frames": 1})
    return cards


def episode_starts(duration: float, segments: list[dict],
                   runtime_min: float | None) -> list[float]:
    """Where each episode in the file probably begins."""
    starts = [0.0]
    for sg in segments[1:]:
        starts.append(max(0.0, float(sg.get("start", 0)) - 20))
    if len(segments) <= 1 and runtime_min and duration > 1.6 * runtime_min * 60:
        starts.append(max(0.0, duration / 2 - 90))  # likely a second episode, no dialogue split
    return [x for x in dict.fromkeys(round(x, 1) for x in starts) if x < duration]


def detect(video: Path, duration: float, segments: list[dict], index: TitleIndex,
           learned_window: list | None = None, runtime_min: float | None = None,
           ctx=None) -> list[dict]:
    """Title cards found in the file: [{code, title, text, score, time, frames}].
    For each probable episode start, look in the window learned for this series
    first (fast), then the full window if nothing turned up."""
    full = float(get_settings()["titlecard_scan_seconds"])
    found: list[dict] = []
    for s in episode_starts(duration, segments, runtime_min):
        cards = []
        if learned_window:
            a, b = learned_window
            cards = cards_from_frames(scan_window(video, s + a, b - a, index, ctx))
        if not cards:
            cards = cards_from_frames(
                scan_window(video, s, min(full, max(10.0, duration - s)), index, ctx))
        for c in cards:
            if not any(c["code"] == f["code"] and abs(c["time"] - f["time"]) < 30 for f in found):
                found.append(c)
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
