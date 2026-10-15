<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/static/icon-dark.svg">
    <img src="app/static/icon-light.svg" alt="EpisodeID" width="128">
  </picture>
</p>

# EpisodeID

Works out which episode each video file **really** contains by reading its dialogue, then renames (and if needed splits) files to TVDB numbering so they line up with Sonarr. It works with any series and runs as a Docker container with a web UI.

## How it works

1. **Episode list** comes from TVDB (default/aired order, the same order Sonarr uses). It's refreshed before a scan or reference fetch when it's more than 12 hours old, so renumbering on TVDB (episodes moved into a new season, say) is picked up; references and episodes you set by hand move to the new numbers with them. A filename naming an episode the list doesn't have is never renamed.
2. **References** are what each episode is supposed to say, fetched once and cached:
   - files you upload yourself (*Episodes & references* tab), highest priority
   - a Fandom wiki transcript page (no quota cost). The wiki is found automatically from the series name, its transcript pages are listed from the wiki itself and matched to TVDB episodes by title (so "The Re-Run" finds "The Rerun/Transcript"). You can still set or override it under *Series options*.
   - OpenSubtitles, searched by season/episode (series IMDb ID) and by the episode's IMDb ID, checking the title. When neither finds it — older shows are often numbered differently on IMDb — or what's found is titled as another episode, it searches by episode title within the series. It also stands in when the wiki doesn't answer (after 3 failures in a row the wiki is left alone for that fetch); a later fetch replaces those with wiki transcripts. Wikis are asked politely — EpisodeID identifies itself, keeps to about one request a second per wiki, uses the MediaWiki API, and backs off as long as asked when a wiki answers 403/429 — since Fandom blocks scripts that hammer it. A subtitle's own file name (usually matching the uploader's video file) is checked too: this episode's TVDB title there verifies the subtitle, another episode's title rules it out. Already-downloaded references that aren't trusted yet are re-checked this way on the next *Fetch references* (a search only — no download quota).
3. **Your files' dialogue** comes from a sidecar `.srt/.ass/.vtt`, else an embedded text subtitle track, else **Whisper** (faster-whisper; also covers image-based PGS/VobSub subs). Results are cached, and survive renames.
4. **Matching**: the dialogue is cut into 90 s windows (every 45 s), each scored against every reference with TF-IDF cosine similarity. A Viterbi pass picks one episode per window with a switching penalty, giving clean segments, so two-episode files and swapped halves are detected. Each segment is re-scored as a whole for its confidence.
5. **AI fallback (optional)**: any OpenAI-compatible endpoint (Ollama `/v1`, OpenAI, LM Studio, vLLM, OpenRouter…). Only consulted for files the matcher couldn't settle, choosing among ≤12 unclaimed candidates using TVDB summaries. Its picks are marked **AI** and are never ticked for apply automatically.
6. **Plan → Apply → Undo**: the plan is shown as a table you can tick, untick and correct (*Set episode…*). Apply requires typing `APPLY`. Above the plan, *Episodes after applying* shows how many aired episodes will have a file once the ticked changes are applied — which would be lost, gained, still missing, or end up with two files — and updates as you tick and untick.

### Title cards

For shows that put the episode title on screen, EpisodeID can read it with OCR (RapidOCR, bundled models, CPU) and use it as evidence that doesn't depend on subtitles, references or anyone's episode numbering:

- **Auto** (default per series): the first scan tests up to 6 files. Title cards are used only if they're found on most of them, and their usual position is learned so later files are checked quickly. Change it under *Series options*: Auto / On / Off, and whether to check every file or only those the dialogue match didn't confirm.
- Title card agrees with the dialogue → confirmed. Title card **and** filename agree but the dialogue doesn't → the file is left alone and the log names the reference to check. Weak dialogue → the title card decides, even against the filename, when it's a clear read (complete title, 2+ frames; *Series options → Trust title cards*, on by default). Strong dialogue disagreeing with the card → review. Two files whose cards name each other's episode are a confirmed swap.
- Files with no subtitles and no transcript can be identified from their cards alone, including two-episode files with two cards.
- Optional: send hard-to-read cards to a vision-capable model on the AI endpoint (*Settings → Title cards*).
- **Speed / hardware acceleration.** Keyframes are read first (about 8x less decoding; title cards nearly always start on a cut), and a window is only fully decoded when that finds nothing. OCR runs on the GPU in the `-cuda` image (ONNX Runtime CUDA), and video decoding uses NVDEC there, or VAAPI on Intel/AMD integrated graphics when `/dev/dri` is passed to the container. Everything falls back to the CPU on its own; *Settings → Title cards → Test* shows what's in use and how fast OCR runs.
- Text from the opening titles that looks like an episode title is spotted on each scan from a sample of files spread across the library: the same title read at the same point in several files whose names say otherwise is ignored at that point, and still counts as a title card anywhere else.
- A read that contradicts the filename (an intro roll call of names, a sign) doesn't end the search for the card: the scan keeps going, and a card that fits the filename wins over other text read within 2 minutes of it. A file whose card really contradicts its name is still caught.
- Only large on-screen text counts, and it must match one title clearly better than any other, so signs and credits in the background are ignored. OCR results are cached; to read the cards from the videos again, use *Series options → Rescan, re-reading title cards*.

### Safety
- Nothing is deleted unless you ask: *Series options → Backup folder* shows what's there (count and size per folder) and deletes the folders you tick, after typing DELETE. Displaced files go to `_episodeid_backup/<Show>/` **beside** the series folder (in the same TV folder — not inside the series, so Sonarr doesn't pick them up; a backup folder left inside a series by an older version is moved there automatically, and Undo keeps working): `duplicates/`, `unverified/` (its name was needed by a confirmed file), `split_originals/`, `metadata/` (stale `.nfo`/thumbnails), `conflicts/`, `undone/`. The top of the backup folder gets ignore files so media servers that scan the TV folder skip it: `.ignore` (Jellyfin; Emby 4.8), `.embyignore` (Emby 4.9+), `.plexignore` (Plex) and `.nomedia` (Kodi). Ones already there are left as they are.
- Every disk operation is logged as it happens, so even an interrupted apply can be undone.
- Renames go through temporary names in two passes, so swaps and rotations are safe.
- Splits are lossless stream copies cut on a keyframe inside the black (or silent) gap between episodes. To check or change where, use *Choose cut points…* on a split: the candidate places (black, silence, or both), each with a picture from just before and just after, plus your own time (snapped to the nearest keyframe).
- Before any original is moved, every split piece is checked: it has the expected length and plays at its start and its end. Free space is checked first too (splitting needs about as much again as the files being split). If either check fails, the apply stops with nothing changed.
- Undo works newest-first and restores every move; split pieces are parked in `undone/`.
- After an apply the plan updates itself — renamed files show as correct under their new names, split pieces as files of their own — so what's left (unticked changes, reviews) is all that remains. No rescan needed; an undo puts the plan back too.
- All paths are confined to the mounted media folder.
- Optional login: set a username and password under *Settings → Security* (or `AUTH_USERNAME` / `AUTH_PASSWORD`) and the browser asks for them before the web UI opens. The container's own healthcheck still works. Locked out? Set the two variables on the container, or remove them from `/config/settings.json`.

## Running

```bash
docker compose up -d   # then open http://SERVER:8686
```

Volumes:

| Path | Purpose |
|---|---|
| `/media/...` | Your TV library (read-write). Mount one or more folders under `/media`. |
| `/config` | Settings, SQLite database, manually uploaded references |
| `/cache` | Downloaded references, extracted subtitles/transcripts, title-card OCR, Whisper models. Dialogue and OCR are keyed by each file's size and modified time, so they follow renames and a replaced file is read again. *Settings → Cache → Clear unused cache* removes entries for files no longer in any series. |

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
| `SONARR_URL`, `SONARR_API_KEY` | | Triggers a series rescan after apply/undo; *Copy from Sonarr* (Settings → Naming) copies its naming settings |
| `NAMING_FORMAT` | `{series} - S{season:02d}E{episode:02d} - {title} {quality}` | Sonarr's tokens work too (`{Series Title}`, `{Series TitleYear}`, `{season:00}`, `{Episode Title}`, `{Episode CleanTitle}`, `{Quality Full}`, …). Quality is carried over from the old filename; MediaInfo and release-group tokens are left out |
| `REPLACE_ILLEGAL_CHARACTERS` | `true` | As Sonarr: `\ /` → `+`, `?` → `!`, `*` → `-`, `< > \| "` removed (`false`: all removed) |
| `COLON_REPLACEMENT` | `smart` | As Sonarr: `smart` ("Up: Pups" → "Up - Pups"), `delete`, `dash`, `space_dash`, `space_dash_space`, `custom` (+ `COLON_REPLACEMENT_CUSTOM`) |
| `MULTI_EPISODE_STYLE` | `prefixed_range` | As Sonarr: `prefixed_range` S01E01-E03 (default), `extend` S01E01-02-03, `duplicate` S01E01 - S01E02 - S01E03, `repeat` S01E01E02E03, `scene` S01E01-E02-E03, `range` S01E01-03 |
| `AUTH_USERNAME`, `AUTH_PASSWORD` | | Both set: the web UI asks for a login |
| `AUTO_CHECK_HOURS` | `24` | How often to look for added, replaced or removed video files in scanned series (names, sizes, dates — no video is read). A series with changes is flagged on its card ("⚑ 2 new since the last scan") and gets references fetched for new episodes; it isn't scanned — that's left to you. The flag clears when the series is scanned. `0` = off. *Settings → File check → Check now* runs it at once. |
| `MIN_SCORE`, `MIN_MARGIN`, `MIN_SEGMENT_SECONDS` | `0.12`, `0.04`, `240` | Matching thresholds |

`{series}` defaults to the series folder name; change it per series under *Series options*.

**Copy from Sonarr** reads Sonarr's Media Management naming (episode format, season folder, colon replacement, illegal characters, multi-episode style) and saves it here; settings locked by environment variables are left alone. It also lists any series Sonarr treats as *anime* or *daily*: Sonarr names those by absolute number or air date, while EpisodeID always uses season and episode numbers. Scans of such a series say so in the log too.

## Workflow

1. **Settings** → add the TVDB key (and OpenSubtitles / Sonarr / AI if wanted) and press *Test*.
2. **Add series** → pick the show folder → pick the TVDB match. Folders named the Sonarr
   way (`Show (2011) {tvdb-248482}`) are recognised.
3. **Fetch references**. Re-run it after the OpenSubtitles quota resets until coverage is
   complete; upload anything that can't be found.
4. **Scan files**, review the plan, fix anything in *Needs review*, **Apply**.
5. If something looks wrong: **History → Undo**.
