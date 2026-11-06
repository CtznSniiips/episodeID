"""External subtitle sidecars can be disabled without reusing stale cache data."""
import os
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_sidecar_subtitles"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(
    MEDIA_ROOT=str(W / "media"),
    CONFIG_DIR=str(W / "config"),
    CACHE_DIR=str(W / "cache"),
    WHISPER_ENABLED="false",
)
os.environ.pop("SIDECAR_SUBTITLES", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, media_text  # noqa: E402

video = W / "media" / "tv" / "Show" / "Season 1" / "Show - S01E01.mkv"
video.parent.mkdir(parents=True)
video.write_bytes(b"not a real video")
sidecar = video.with_name(video.stem + ".en.srt")
sidecar.write_text("1\n00:00:00,000 --> 00:00:02,000\nwrong external dialogue\n")

media_text.probe = lambda _video: {"format": {"duration": "2"}, "streams": []}
media_text.extract_embedded = lambda _video, _info, _lang: (
    [media_text.Cue(0.0, 2.0, "embedded dialogue")], "embedded #2"
)

# The default preserves the existing behavior.
first = media_text.get_dialogue(video)
assert first["source"] == "sidecar " + sidecar.name, first
cached = media_text.cached_dialogue(video)
assert cached is not None
assert cached["source"].startswith("sidecar ")

# Disabling the feature must reject an old sidecar-derived cache entry and use
# the embedded stream instead; no manual cache clearing is required.
config.save_settings({"sidecar_subtitles": False})
assert media_text.cached_dialogue(video) is None
second = media_text.get_dialogue(video)
assert second["source"] == "embedded #2", second
assert second["cues"][0][2] == "embedded dialogue", second
cached = media_text.cached_dialogue(video)
assert cached is not None
assert cached["source"] == "embedded #2"

# The documented environment variable is recognized and takes precedence over
# the settings file.
os.environ["SIDECAR_SUBTITLES"] = "false"
config.save_settings({"sidecar_subtitles": True})
assert config.get_settings()["sidecar_subtitles"] is False
print("PASS")
