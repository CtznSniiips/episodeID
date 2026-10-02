"""Settings: defaults < /config/settings.json < environment variables.

Settings edited in the web UI are written to /config/settings.json.
Environment variables (e.g. TVDB_API_KEY) always win, so secrets can live
in docker-compose / the Unraid template instead of the JSON file.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config")).resolve()
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache")).resolve()

SETTINGS_FILE = CONFIG_DIR / "settings.json"

# key -> (default, env var name, is_secret)
SCHEMA: dict[str, tuple[object, str, bool]] = {
    # TVDB (required)
    "tvdb_api_key": ("", "TVDB_API_KEY", True),
    "tvdb_pin": ("", "TVDB_PIN", True),
    "tvdb_language": ("eng", "TVDB_LANGUAGE", False),
    # OpenSubtitles
    "opensubtitles_api_key": ("", "OPENSUBTITLES_API_KEY", True),
    "opensubtitles_username": ("", "OPENSUBTITLES_USERNAME", False),
    "opensubtitles_password": ("", "OPENSUBTITLES_PASSWORD", True),
    "subtitle_language": ("en", "SUBTITLE_LANGUAGE", False),
    # Whisper
    "whisper_enabled": (True, "WHISPER_ENABLED", False),
    "whisper_model": ("small", "WHISPER_MODEL", False),
    "whisper_device": ("auto", "WHISPER_DEVICE", False),
    "whisper_compute_type": ("default", "WHISPER_COMPUTE_TYPE", False),
    "whisper_language": ("en", "WHISPER_LANGUAGE", False),
    # LLM (OpenAI-compatible; Ollama: http://host:11434/v1)
    "llm_enabled": (False, "LLM_ENABLED", False),
    "llm_base_url": ("http://ollama:11434/v1", "LLM_BASE_URL", False),
    "llm_api_key": ("", "LLM_API_KEY", True),
    "llm_model": ("qwen3:8b", "LLM_MODEL", False),
    # Title cards (OCR of the episode title shown on screen)
    "titlecard_enabled": (True, "TITLECARD_ENABLED", False),
    "titlecard_scan_seconds": (180, "TITLECARD_SCAN_SECONDS", False),
    "titlecard_fps": (1.0, "TITLECARD_FPS", False),
    "titlecard_vision": (False, "TITLECARD_VISION", False),
    "llm_vision_model": ("", "LLM_VISION_MODEL", False),
    # Sonarr
    "sonarr_url": ("", "SONARR_URL", False),
    "sonarr_api_key": ("", "SONARR_API_KEY", True),
    "sonarr_rescan_after_apply": (True, "SONARR_RESCAN_AFTER_APPLY", False),
    # Matching
    "window_seconds": (90, "WINDOW_SECONDS", False),
    "window_step_seconds": (45, "WINDOW_STEP_SECONDS", False),
    "min_segment_seconds": (240, "MIN_SEGMENT_SECONDS", False),
    "min_score": (0.12, "MIN_SCORE", False),
    "min_margin": (0.04, "MIN_MARGIN", False),
    # Naming (Sonarr "Standard" default)
    "naming_format": ("{series} - S{season:02d}E{episode:02d} - {title} {quality}", "NAMING_FORMAT", False),
    "multi_episode_style": ("prefixed_range", "MULTI_EPISODE_STYLE", False),
    "season_folder_format": ("Season {season:02d}", "SEASON_FOLDER_FORMAT", False),
    "specials_folder": ("Specials", "SPECIALS_FOLDER", False),
    "backup_folder": ("_episodeid_backup", "BACKUP_FOLDER", False),
    "allow_splits": (True, "ALLOW_SPLITS", False),
}

_lock = threading.Lock()


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return "" if value is None else str(value)


def _load_file() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_settings() -> dict:
    with _lock:
        stored = _load_file()
    out = {}
    for key, (default, env, _secret) in SCHEMA.items():
        val = stored.get(key, default)
        if env in os.environ:
            val = os.environ[env]
        out[key] = _coerce(val, default)
    return out


def env_locked_keys() -> list[str]:
    return [k for k, (_, env, _) in SCHEMA.items() if env in os.environ]


def public_settings() -> dict:
    """Settings for the UI: secrets are masked, never returned."""
    s = get_settings()
    out = {}
    for key, (_d, _e, secret) in SCHEMA.items():
        out[key] = ("********" if s[key] else "") if secret else s[key]
    return out


def save_settings(update: dict) -> None:
    with _lock:
        stored = _load_file()
        for key, val in update.items():
            if key not in SCHEMA:
                continue
            default, _env, secret = SCHEMA[key]
            if secret and val == "********":
                continue  # unchanged masked value
            stored[key] = _coerce(val, default)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(stored, indent=2))
        tmp.replace(SETTINGS_FILE)


def safe_media_path(rel_or_abs: str | Path) -> Path:
    """Resolve a path and guarantee it stays inside MEDIA_ROOT."""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = MEDIA_ROOT / p
    p = p.resolve()
    if p != MEDIA_ROOT and MEDIA_ROOT not in p.parents:
        raise ValueError(f"Path is outside the media root: {p}")
    return p
