"""Whisper transcription via faster-whisper. The model is loaded once and kept
in memory; models are downloaded into /cache/whisper on first use.

Audio is decoded with the ffmpeg binary rather than faster-whisper's built-in
PyAV decoder: faster-whisper 1.2.x calls av.open(metadata_errors=...), which
newer PyAV releases removed, and ffmpeg also handles every container/codec
the library might hold."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np

from .config import CACHE_DIR, get_settings
from .media_text import Cue, _lang_ok, probe

SAMPLE_RATE = 16000  # what Whisper expects

_model = None
_model_key = None
_lock = threading.Lock()


def _load():
    global _model, _model_key
    s = get_settings()
    device = s["whisper_device"]
    compute = s["whisper_compute_type"]
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    if compute == "default":
        compute = "float16" if device == "cuda" else "int8"
    key = (s["whisper_model"], device, compute)
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel
        _model = WhisperModel(s["whisper_model"], device=device, compute_type=compute,
                              download_root=str(CACHE_DIR / "whisper"))
        _model_key = key
    return _model


def _pick_audio_stream(video: Path, lang: str) -> str:
    """ffmpeg map spec for the best audio track: matching language, not commentary."""
    try:
        streams = [s for s in probe(video).get("streams", []) if s.get("codec_type") == "audio"]
    except Exception:  # noqa: BLE001
        return "0:a:0"
    if not streams:
        raise RuntimeError("file has no audio track")

    def rank(s):
        title = ((s.get("tags") or {}).get("title") or "").lower()
        disp = s.get("disposition") or {}
        return (_lang_ok(s, lang) if lang else True,
                "commentary" not in title and not disp.get("comment"),
                bool(disp.get("default")), -s["index"])

    return f"0:{max(streams, key=rank)['index']}"


def decode_audio(video: Path, lang: str = "") -> np.ndarray:
    """Whole audio track as 16 kHz mono float32 (≈85 MB for a 22-minute episode)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
           "-map", _pick_audio_stream(video, lang), "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=3600)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"ffmpeg could not decode audio: "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(video: Path, ctx=None) -> list[Cue]:
    s = get_settings()
    lang = s["whisper_language"] or None
    audio = decode_audio(video, lang or "")
    with _lock:
        model = _load()
        segments, info = model.transcribe(
            audio, language=lang, vad_filter=True,
            beam_size=1, condition_on_previous_text=False)
        cues = []
        total = info.duration or (len(audio) / SAMPLE_RATE) or 1
        for seg in segments:
            text = seg.text.strip()
            if text:
                cues.append(Cue(seg.start, seg.end, text))
            if ctx:
                ctx.check_cancel()
                ctx.status(f"Whisper: {video.name} {seg.end / total:.0%}")
        return cues
