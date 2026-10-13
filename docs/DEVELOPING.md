# Developing Auto Clip Compiler

## Layout

| Path | What it is |
|---|---|
| `clipcompiler/gemini.py` | Prompt, schema, prices, 480p proxy, upload-judge-delete with retries |
| `clipcompiler/skill.py` | Skill judging: triage + close-up prompts/schemas, the pure scoring formula, gating, windows, skill cache records, skill moments |
| `clipcompiler/skill_profiles/` | Per-game scoring weights, crop panel and prompt hints (`cs2`, `rust`, `generic`); users override key by key from `%APPDATA%\AutoClipCompiler\skill_profiles\` |
| `clipcompiler/reel.py` | Clip discovery, moments from judgments, replay dedupe, Funny+Skill merge, pick (with Both-mode shares), split into parts |
| `clipcompiler/batch.py` | Batch API: submit (resumable uploads), refresh, collect (idempotent), cancel; state in `<job>/batch.json` |
| `clipcompiler/job.py` | `scan`, `JudgeJob`, `render_reel`, `render_parts`: cancellable, report through events |
| `clipcompiler/render.py` | Frame-exact cuts, atomic publish (MP4 + manifest + FCPXML), progress + cancel |
| `clipcompiler/media.py` | ffmpeg/ffprobe (no console window, cancellable), bundled-ffmpeg lookup |
| `clipcompiler/store.py` | Content hashes, `HashIndex` (path+size+mtime), atomic JSON, keep-awake |
| `clipcompiler/settings.py` | `settings.json` in `%APPDATA%`; API key in Credential Manager; log redaction |
| `clipcompiler/paths.py` | App data dirs; `ACC_HOME` overrides the root (tests, portable runs) |
| `clipcompiler/ui/` | `api.py` (the JS bridge), `server.py` (loopback UI + preview server), `main.py`, `web/` |
| `tools/` | CLI: `gemini_reel.py` (live judging + render) and `gemini_batch.py` (Batch API, half price) |
| `packaging/` | `build.ps1` (assembles the signed-Python app folder), Inno Setup script, icon |

Data lives in `%LOCALAPPDATA%\AutoClipCompiler\` (`gemini-cache\`, `previews\`, `hash-index.json`,
`logs\`) and `%APPDATA%\AutoClipCompiler\settings.json`. Funny judgments are `gemini-cache\<sha256>.json`
(unchanged since v1); skill records are `gemini-cache\skill\<sha256>.json` = triage + one close-up per
gated play, saved after each stage so nothing is paid twice. Skill scores are never stored: they are
recomputed from the saved facts, so presets and profile edits are free. The judgment cache is keyed by each clip's
SHA-256, so it survives renames and is shared by the app and the CLI. Each compilation is a
`Compilation <date>` folder in the output folder with `run.json` (moments + review decisions),
`report.json` and the rendered videos.

## Skill compilations

`content_mode` is `funny` (v1 behaviour, byte for byte), `skill` or `both`.

1. **Triage** (`skill.TRIAGE_PROMPT`): the same 480p proxy as funny judging. In Both mode one upload
   serves both questions (`gemini.upload_and_ask`). Returns the game and candidate plays.
2. **Gate** (`skill.plays_to_review`): preliminary score >= profile threshold or at least one kill,
   best first, at most 3 per clip.
3. **Close-up** (`media.window`): `[start-2s, end+1s]` (max 8 s) cut from the original into a 1280x480
   composite (zoomed center | profile panel over full frame, with audio), sent with
   `video_metadata.fps = skill.WINDOW_FPS`. Gemini returns facts in enum buckets; `skill.to_clip_time`
   maps window times back to the clip.
4. **Score** (`skill.value` / `skill.score`): quality = weighted mechanics (best kill + 0.35 x second)
   and situation (1vX clutch, low HP, decisive, outnumbered), scaled by `blend.quality`; plus
   `weights.multi` x kill-count credit (`skill.kill_count_value`: `per_kill` per kill plus an
   escalating n(n-1)/2, over `ramp`, so every kill adds and each extra adds more; an easy kill counts
   `min_credit`..1 of a kill); times penalty factors; blended 70/30 with the model's holistic
   difficulty. The Aim preset halves the kill-count weight. `skill.tags` explains it.
5. **Reel**: funny and skill moments are thresholded on their own scales, a play that is both is
   merged into one moment with both tags (`reel.merge_lenses`), and Both mode fills a skill share
   and a funny share of the target, each by its own score (`reel.pick(..., skill_share)`).

Batch: stage 1 asks funny and/or triage per upload (keys `<sha>` and `<sha>:triage`); when it is
collected the app submits stage 2 (`batch.submit_details`, keys `<sha>:detail:<play>`) to `batch-2\`
and opens review after that one. Typical token counts for the estimates (`skill.TYPICAL_*`,
`skill.PLAYS_PER_CLIP`) and `WINDOW_FPS` are guesses until the live pilot measures them.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install Gyan.FFmpeg
.\.venv\Scripts\python.exe autoclip.py          # the app
$env:ACC_DEBUG = '1'                             # right-click > Inspect in the window
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

- No network; Gemini is faked (`judge_fn`, `skill_fn`), clips are synthetic ffmpeg test patterns.
- `tests/test_skill_score.py` holds the golden orderings (flick headshot > sloppy 2-kill spray,
  1v3 clutch > kills on unaware enemies, ...) from `tests/fixtures/skill/*.json`, plus properties:
  penalties never raise a score, a harder bucket never lowers it, scores stay 1-10, junk is neutral.
  Change the formula or a profile and these say whether the taste still holds.
- `tests/test_skill_job.py`, `test_api_skill.py`: two-stage judging, resume after triage without
  re-paying, budget, Both-mode shared upload, chained batch, and a check that skill/both runs leave
  the funny cache byte-identical.
- `tests/test_api_pick.py`: the clip picker (`Api.scan_clips` / `select_clips` / `open_clip`). Counts
  and estimates follow the ticked clips with no API calls, only ticked clips are judged or batched,
  unknown paths are ignored, ticking everything means "all", a new scan resets the choice.
- `tests/ui/picker_check.js`: drives the picker in Chromium over the mock bridge (tick rows and days,
  filters, search, play button, cancel/done, no clipped day boxes, no page errors) and saves screenshots.
  Run with Playwright available: `$env:PLAYWRIGHT = '<node_modules\playwright>'; node tests\ui\picker_check.js <dir>`.
- `tests/test_reel.py` includes a parity test that replays the real 2026-10-12 library plan from
  `%LOCALAPPDATA%\AutoClipCompiler-dev\parity\` when that fixture exists.
- `tests/test_batch.py` / `test_api_batch.py` run batch mode against `tests/fake_gemini.py`, an
  in-memory Files + Batches API that returns results in the real REST shape.
- `tests/test_live.py` hits the real API (about $0.02, the batch test waits for Google, usually ~10 min):
  `$env:ACC_LIVE = '1'`.
- UI screenshots without Python: inject `tests/ui/mock_bridge.js` before `app.js` (e.g. Playwright
  `addInitScript`) and open `clipcompiler/ui/web/index.html`.

## Build the installer

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

There is deliberately **no exe of our own**. Windows Smart App Control blocks unsigned executables it
has no reputation for (a PyInstaller exe was blocked on the author's PC on 2026-10-13), and it has no
"run anyway". So the app ships as:

| Folder | Contents |
|---|---|
| `python\` | The official python.org embeddable package matching the venv's version (build fails unless every binary is signed by the PSF or Microsoft). `python3xx._pth` points at `..\app` and `..\site-packages`. |
| `site-packages\` | `pip install --target` of `requirements.txt`, pinned to the venv's versions: the same PyPI files everyone uses. |
| `app\` | The `clipcompiler` package, precompiled. |
| `ffmpeg\` | `ffmpeg.exe`, `ffprobe.exe` from PATH (or `$env:ACC_FFMPEG_DIR`) and the license. |

The Start menu shortcut runs `python\pythonw.exe -m clipcompiler.ui.main` with `app.ico` and
AppUserModelID `KalebDieterle.AutoClipCompiler`, which the app also sets at runtime so the taskbar shows
our icon. `build.ps1` runs the tests, assembles `dist\AutoClipCompiler\`, launches it in self-test mode
(`ACC_SELFTEST=1`: page booted through the bridge, bundled ffmpeg, Credential Manager, window icon), then
builds `dist\AutoClipCompiler-Setup-<version>.exe` with Inno Setup 6
(`winget install JRSoftware.InnoSetup --scope user`). The Setup.exe itself is still unsigned; it passed
Smart App Control on 2026-10-13, but that verdict is per file and can change. Code signing is the only
complete fix.

## CLI

```powershell
$env:PYTHONPATH = (Get-Location).Path
python tools\gemini_reel.py --source "D:\Clips" --output "D:\Out" --target-minutes 30
python tools\gemini_reel.py --source "D:\Clips" --output "D:\Out" --parts-minutes 30 --max-clips 5000 --cache-only
python tools\gemini_batch.py submit --job-dir "D:\Out\batch" --source "D:\Clips"
python tools\gemini_reel.py --source "D:\Clips" --output "D:\Out" --content both --skill-preset aim
python tools\gemini_batch.py submit --job-dir "D:\Out\b1" --source "D:\Clips" --content skill   # then collect
python tools\gemini_batch.py details --job-dir "D:\Out\b2" --source "D:\Clips"                  # close-ups; then collect
```

The key comes from `GEMINI_API_KEY` or the one saved in the app. `--cache` defaults to the app's
cache folder (as set in Settings). The old local Ollama/whisper pipeline is at tag `legacy-local-pipeline`.
