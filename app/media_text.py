"""Getting timed dialogue out of a video file.

Order of preference:
  1. sidecar subtitle file next to the video (.srt/.ass/.ssa/.vtt)
  2. embedded text subtitle stream (subrip, ass, mov_text, webvtt)
  3. Whisper transcription of the audio (also used for image-based subs)

Results are cached under /cache/text keyed by path+size+mtime.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR, get_settings

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".webm", ".mpg"}
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ---------------------------------------------------------------- parsing

_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_TAGS = re.compile(r"<[^>]+>|\{[^}]*\}")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        return 0.0
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def _clean(text: str) -> str:
    text = html.unescape(_TAGS.sub(" ", text))
    text = text.replace("\\N", " ").replace("\\n", " ")
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|♪", " ", text)  # sound cues, lyrics marks
    return re.sub(r"\s+", " ", text).strip()


def parse_srt_vtt(raw: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                text = _clean(" ".join(lines[i + 1:]))
                if text:
                    cues.append(Cue(_ts(a), _ts(b), text))
                break
    return cues


def parse_ass(raw: str) -> list[Cue]:
    cues = []
    fmt = None
    for line in raw.splitlines():
        if line.startswith("Format:") and fmt is None and "Text" in line:
            fmt = [f.strip().lower() for f in line[7:].split(",")]
        elif line.startswith("Dialogue:"):
            fields = fmt or ["layer", "start", "end", "style", "name", "marginl",
                             "marginr", "marginv", "effect", "text"]
            parts = line[9:].split(",", len(fields) - 1)
            if len(parts) < len(fields):
                continue
            d = dict(zip(fields, parts))
            m = re.match(r"\s*(\d+):(\d+):(\d+)\.(\d+)", d["start"])
            n = re.match(r"\s*(\d+):(\d+):(\d+)\.(\d+)", d["end"])
            if not (m and n):
                continue
            st = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 100
            en = int(n[1]) * 3600 + int(n[2]) * 60 + int(n[3]) + int(n[4]) / 100
            text = _clean(d["text"])
            if text:
                cues.append(Cue(st, en, text))
    cues.sort(key=lambda c: c.start)
    return cues


def parse_subtitle_text(raw: str, ext: str) -> list[Cue]:
    if ext in (".ass", ".ssa") or "[Script Info]" in raw[:200]:
        return parse_ass(raw)
    return parse_srt_vtt(raw)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ------------------------------------------------------------------ probe

def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def duration(info: dict) -> float:
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _lang_ok(stream: dict, lang: str) -> bool:
    tag = ((stream.get("tags") or {}).get("language") or "").lower()
    if not lang:
        return True
    aliases = {"en": {"en", "eng", "english"}, "es": {"es", "spa"}, "fr": {"fr", "fre", "fra"},
               "de": {"de", "ger", "deu"}, "it": {"it", "ita"}, "pt": {"pt", "por"},
               "nl": {"nl", "dut", "nld"}, "ja": {"ja", "jpn"}}
    return tag in aliases.get(lang, {lang}) or tag in ("", "und")


# ---------------------------------------------------------------- extract

def _cache_key(path: Path) -> str:
    st = path.stat()
    # No path in the key: renames keep size+mtime, so Whisper transcripts survive an apply.
    key = f"{st.st_size}|{st.st_mtime_ns}"
    # A sidecar subtitle added/changed later must invalidate the cache.
    stem = path.with_suffix("").name
    for f in sorted(path.parent.glob(glob_escape(stem) + "*")):
        if f.suffix.lower() in SUB_EXTS:
            fs = f.stat()
            key += f"|{f.name[len(stem):]}|{fs.st_size}|{fs.st_mtime_ns}"
    return hashlib.sha1(key.encode()).hexdigest()


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def _cache_file(path: Path) -> Path:
    return CACHE_DIR / "text" / f"{_cache_key(path)}.json"


def sidecar_subtitle(video: Path, lang: str) -> Path | None:
    stem = video.with_suffix("").name
    candidates = []
    for f in video.parent.iterdir():
        if f.suffix.lower() in SUB_EXTS and f.name.startswith(stem):
            middle = f.name[len(stem):-len(f.suffix)].lower().strip(".")
            score = 0
            if not middle:
                score = 1
            elif lang and lang in middle.split("."):
                score = 2
            if "forced" in middle:
                score = -1
            candidates.append((score, f))
    candidates = [c for c in candidates if c[0] >= 0]
    return max(candidates, key=lambda c: c[0])[1] if candidates else None


def extract_embedded(video: Path, info: dict, lang: str) -> tuple[list[Cue], str] | None:
    subs = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    text_subs = [s for s in subs if s.get("codec_name") in TEXT_SUB_CODECS]
    if not text_subs:
        return None

    def rank(s):
        disp = s.get("disposition") or {}
        title = ((s.get("tags") or {}).get("title") or "").lower()
        return (_lang_ok(s, lang), not disp.get("forced") and "forced" not in title,
                "sdh" not in title, -s["index"])

    best = max(text_subs, key=rank)
    if not _lang_ok(best, lang):
        return None
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-map", f"0:{best['index']}",
         "-f", "srt", "-"], capture_output=True, timeout=600)
    if r.returncode != 0:
        return None
    cues = parse_srt_vtt(r.stdout.decode("utf-8", "replace"))
    return (cues, f"embedded #{best['index']}") if cues else None


def get_dialogue(video: Path, ctx=None, allow_whisper: bool = True) -> dict:
    """Returns {"source": str, "duration": float, "cues": [[start, end, text], ...]}.
    source is "none" when no text could be produced."""
    s = get_settings()
    lang = s["subtitle_language"]
    cache = _cache_file(video)
    if cache.exists():
        data = json.loads(cache.read_text())
        if data["source"] != "none" or not allow_whisper or not s["whisper_enabled"]:
            return data

    info = probe(video)
    dur = duration(info)
    result = None

    sc = sidecar_subtitle(video, lang)
    if sc:
        cues = parse_subtitle_text(read_text_file(sc), sc.suffix.lower())
        if cues:
            result = (cues, f"sidecar {sc.name}")
    if result is None:
        result = extract_embedded(video, info, lang)
    if result is None and allow_whisper and s["whisper_enabled"]:
        from .transcribe import transcribe
        if ctx:
            ctx.log(f"  no text subtitles; transcribing with Whisper ({s['whisper_model']})…")
        cues = transcribe(video, ctx)
        if cues:
            result = (cues, f"whisper {s['whisper_model']}")

    if result is None:
        data = {"source": "none", "duration": dur, "cues": []}
    else:
        cues, source = result
        data = {"source": source, "duration": dur,
                "cues": [[round(c.start, 2), round(c.end, 2), c.text] for c in cues]}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def has_image_subs(info: dict) -> bool:
    return any(s.get("codec_name") in IMAGE_SUB_CODECS for s in info.get("streams", []))
