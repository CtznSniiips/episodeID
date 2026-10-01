"""Whisper transcription via faster-whisper. The model is loaded once and kept
in memory; models are downloaded into /cache/whisper on first use."""
from __future__ import annotations

import threading
from pathlib import Path

from .config import CACHE_DIR, get_settings
from .media_text import Cue

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


def transcribe(video: Path, ctx=None) -> list[Cue]:
    s = get_settings()
    with _lock:
        model = _load()
        segments, info = model.transcribe(
            str(video), language=s["whisper_language"] or None, vad_filter=True,
            beam_size=1, condition_on_previous_text=False)
        cues = []
        total = info.duration or 1
        for seg in segments:
            text = seg.text.strip()
            if text:
                cues.append(Cue(seg.start, seg.end, text))
            if ctx:
                ctx.check_cancel()
                ctx.status(f"Whisper: {video.name} {seg.end / total:.0%}")
        return cues
