<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/static/icon-dark.svg">
    <img src="app/static/icon-light.svg" alt="EpisodeID" width="128">
  </picture>
</p>

# EpisodeID

Works out which episode each video file **really** contains by reading its dialogue, then renames (and if needed splits) files to TVDB numbering so they line up with Sonarr. It works with any series and runs as a Docker container with a web UI.

## How it works

1. **Episode list** comes from TVDB (default/aired order, the same order Sonarr uses).
2. **References** are what each episode is supposed to say, fetched once and cached:
   - files you upload yourself (*Episodes & references* tab), highest priority
   - a Fandom wiki transcript page (no quota cost). The wiki is found automatically from
     the series name, its transcript pages are listed from the wiki itself and matched to
     TVDB episodes by title (so "The Re-Run" finds "The Rerun/Transcript"). You can still
     set or override it under *Series options*.
   - OpenSubtitles, looked up by the episode's IMDb ID from TVDB. If only the series ID is available it searches by season/episode and checks the title, then falls back to searching by title, so differing numbering schemes don't silently give you the wrong reference.
3. **Your files' dialogue** comes from a sidecar `.srt/.ass/.vtt`, else an embedded text subtitle track, else **Whisper** (faster-whisper; also covers image-based PGS/VobSub subs). Results are cached, and survive renames.
4. **Matching**: the dialogue is cut into 90 s windows (every 45 s), each scored against every reference with TF-IDF cosine similarity. A Viterbi pass picks one episode per window with a switching penalty, giving clean segments, so two-episode files and swapped halves are detected. Each segment is re-scored as a whole for its confidence.
5. **AI fallback (optional)**: any OpenAI-compatible endpoint (Ollama `/v1`, OpenAI, LM Studio, vLLM, OpenRouter…). Only consulted for files the matcher couldn't settle, choosing among ≤12 unclaimed candidates using TVDB summaries. Its picks are marked **AI** and are never ticked for apply automatically.
6. **Plan → Apply → Undo**: the plan is shown as a table you can tick, untick and correct (*Set episode…*). Apply requires typing `APPLY`.

### Title cards

For shows that put the episode title on screen, EpisodeID can read it with OCR
(RapidOCR, bundled models, CPU) and use it as evidence that doesn't depend on
subtitles, references or anyone's episode numbering:

- **Auto** (default per series): the first scan tests up to 6 files. Title cards are
  used only if they're found on most of them, and their usual position is learned so
  later files are checked quickly. Change it under *Series options*: Auto / On / Off,
  and whether to check every file or only those the dialogue match didn't confirm.
- Title card agrees with the dialogue → confirmed. Title card **and** filename agree but
  the dialogue doesn't → the file is left alone and the log names the reference to check.
  Weak dialogue → the title card decides. Strong dialogue disagreeing with both → review.
- Files with no subtitles and no transcript can be identified from their cards alone,
  including two-episode files with two cards.
- Optional: send hard-to-read cards to a vision-capable model on the AI endpoint
  (*Settings → Title cards*).
- Only large on-screen text counts, and it must match one title clearly better than any
  other, so signs and credits in the background are ignored. OCR results are cached.

### Safety
- Nothing is deleted. Displaced files go to `<series>/_episodeid_backup/`: `duplicates/`, `unverified/` (its name was needed by a confirmed file), `split_originals/`, `metadata/` (stale `.nfo`/thumbnails), `conflicts/`, `undone/`.
- Every disk operation is logged as it happens, so even an interrupted apply can be undone.
- Renames go through temporary names in two passes, so swaps and rotations are safe.
- Splits are lossless stream copies cut on a keyframe inside the black (or silent) gap between episodes.
- Undo works newest-first and restores every move; split pieces are parked in `undone/`.
- All paths are confined to the mounted media folder.

## Running

```bash
docker compose up -d   # then open http://SERVER:8686
```

Volumes:

| Path | Purpose |
|---|---|
| `/media/...` | Your TV library (read-write). Mount one or more folders under `/media`. |
| `/config` | Settings, SQLite database, manually uploaded references |
| `/cache` | Downloaded references, extracted subtitles/transcripts, Whisper models |

`PUID`/`PGID` default to Unraid's `99:100`. An Unraid template is in `unraid/episodeid.xml`.

**GPU Whisper**: use the `-cuda` image (`Dockerfile.cuda`), run with `--runtime=nvidia` / `--gpus all`. Only the NVIDIA driver is needed on the host; cuBLAS and cuDNN 9 ship inside the image as pip wheels. It defaults to `large-v3` on CUDA. On CPU, `small` is a good balance; expect a few minutes per 22-minute episode.

## Settings

Everything is editable in the web UI; any environment variable overrides the UI value (shown as *locked*). Secrets are never sent back to the browser.

| Env var | Default | |
|---|---|---|
| `TVDB_API_KEY`, `TVDB_PIN` | | Required. PIN only for user-supported keys. |
| `OPENSUBTITLES_API_KEY`, `_USERNAME`, `_PASSWORD` | | Downloads need an account. Free accounts have a small daily cap — fetches resume where they stopped. |
| `SUBTITLE_LANGUAGE` | `en` | Reference language and embedded-track preference |
| `WHISPER_ENABLED` / `WHISPER_MODEL` / `WHISPER_DEVICE` | `true` / `small` / `auto` | |
| `LLM_ENABLED`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | off | e.g. `http://ollama:11434/v1`, `qwen3:8b` |
| `SONARR_URL`, `SONARR_API_KEY` | | Triggers a series rescan after apply/undo |
| `NAMING_FORMAT` | `{series} - S{season:02d}E{episode:02d} - {title} {quality}` | `{quality}` is carried over from the old filename |
| `MULTI_EPISODE_STYLE` | `prefixed_range` | `S01E01-E02` (Sonarr default), `extend`, `repeat` |
| `MIN_SCORE`, `MIN_MARGIN`, `MIN_SEGMENT_SECONDS` | `0.12`, `0.04`, `240` | Matching thresholds |

`{series}` defaults to the series folder name; change it per series under *Series options*.

## Workflow

1. **Settings** → add the TVDB key (and OpenSubtitles / Sonarr / AI if wanted) and press *Test*.
2. **Add series** → pick the show folder → pick the TVDB match. Folders named the Sonarr
   way (`Show (2011) {tvdb-248482}`) are recognised.
3. **Fetch references**. Re-run it after the OpenSubtitles quota resets until coverage is
   complete; upload anything that can't be found.
4. **Scan files**, review the plan, fix anything in *Needs review*, **Apply**.
5. If something looks wrong: **History → Undo**.
