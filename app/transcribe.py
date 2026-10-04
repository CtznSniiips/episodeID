"""Whisper transcription via faster-whisper. The model is loaded once and kept
in memory; models are downloaded into /cache/whisper on first use.

Audio is decoded with the ffmpeg binary rather than faster-whisper's built-in
PyAV decoder: faster-whisper 1.2.x calls av.open(metadata_errors=...), which
newer PyAV releases removed, and ffmpeg also handles every container/codec
the library might hold."""
from __future__ import annotations

import re
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
_gpu_error: str | None = None  # set when GPU transcription failed; CPU is used afterwards


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
    if _gpu_error:
        device = "cpu"
    if compute == "default":
        compute = "int8"
        if device == "cuda":
            # Pascal and older run float16 very slowly (or not at all): use the best
            # type this GPU supports rather than assuming float16.
            try:
                import ctranslate2
                supported = ctranslate2.get_supported_compute_types("cuda")
            except Exception:  # noqa: BLE001
                supported = {"float32"}
            compute = next(c for c in ("float16", "int8_float16", "int8_float32", "int8", "float32")
                           if c in supported or c == "float32")
    model_name = s["whisper_model"]
    if _gpu_error and re.match(r"(large|medium|distil-large)", model_name):
        model_name = "small"  # GPU-sized model would crawl on the CPU fallback
    key = (model_name, device, compute)
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel
        _model = WhisperModel(model_name, device=device, compute_type=compute,
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
    global _gpu_error, _model
    with _lock:
        try:
            return _run(_load(), audio, lang, video, ctx)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "Cancelled" or not _model_key or _model_key[1] != "cuda":
                raise
            # GPU transcription failed (unsupported GPU/driver, out of memory…): CPU from now on.
            _gpu_error = str(e).strip().splitlines()[-1][:300] if str(e).strip() else type(e).__name__
            _model = None
            if ctx:
                ctx.log(f"  Whisper on the GPU failed ({_gpu_error}); using the CPU instead "
                        "(with the 'small' model if a large one was selected).")
            return _run(_load(), audio, lang, video, ctx)


def _run(model, audio, lang, video: Path, ctx) -> list[Cue]:
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
