# Developing Auto Clip Compiler

## Layout

| Path | What it is |
|---|---|
| `clipcompiler/gemini.py` | Prompt, schema, prices, 480p proxy, upload-judge-delete with retries |
| `clipcompiler/reel.py` | Clip discovery, moments from judgments, replay dedupe, pick, split into parts |
| `clipcompiler/batch.py` | Batch API: submit (resumable uploads), refresh, collect (idempotent), cancel; state in `<job>/batch.json` |
| `clipcompiler/job.py` | `scan`, `JudgeJob`, `render_reel`, `render_parts`: cancellable, report through events |
| `clipcompiler/render.py` | Frame-exact cuts, atomic publish (MP4 + manifest + FCPXML), progress + cancel |
| `clipcompiler/media.py` | ffmpeg/ffprobe (no console window, cancellable), bundled-ffmpeg lookup |
| `clipcompiler/store.py` | Content hashes, `HashIndex` (path+size+mtime), atomic JSON, keep-awake |
| `clipcompiler/settings.py` | `settings.json` in `%APPDATA%`; API key in Credential Manager; log redaction |
| `clipcompiler/paths.py` | App data dirs; `ACC_HOME` overrides the root (tests, portable runs) |
| `clipcompiler/ui/` | `api.py` (the JS bridge), `server.py` (loopback UI + preview server), `main.py`, `web/` |
| `tools/` | CLI: `gemini_reel.py` (live judging + render) and `gemini_batch.py` (Batch API, half price) |
| `packaging/` | PyInstaller spec, Inno Setup script, icon, `build.ps1` |

Data lives in `%LOCALAPPDATA%\AutoClipCompiler\` (`gemini-cache\`, `previews\`, `hash-index.json`,
`logs\`) and `%APPDATA%\AutoClipCompiler\settings.json`. The judgment cache is keyed by each clip's
SHA-256, so it survives renames and is shared by the app and the CLI. Each compilation is a
`Compilation <date>` folder in the output folder with `run.json` (moments + review decisions),
`report.json` and the rendered videos.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
winget install Gyan.FFmpeg
.\.venv\Scripts\python.exe autoclip.py          # the app
$env:ACC_DEBUG = '1'                             # right-click > Inspect in the window
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

- No network; Gemini is faked (`judge_fn`), clips are synthetic ffmpeg test patterns.
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

Runs the tests, stages `ffmpeg.exe`/`ffprobe.exe` from PATH (or `$env:ACC_FFMPEG_DIR`), builds
`dist\AutoClipCompiler\` with PyInstaller, launches it in self-test mode (`ACC_SELFTEST=1`: checks the
page booted through the bridge, the bundled ffmpeg and the Credential Manager backend), then builds
`dist\AutoClipCompiler-Setup-<version>.exe` with Inno Setup 6 (`winget install JRSoftware.InnoSetup --scope user`).

## CLI

```powershell
$env:PYTHONPATH = (Get-Location).Path
python tools\gemini_reel.py --source "D:\Clips" --output "D:\Out" --target-minutes 30
python tools\gemini_reel.py --source "D:\Clips" --output "D:\Out" --parts-minutes 30 --max-clips 5000 --cache-only
python tools\gemini_batch.py submit --job-dir "D:\Out\batch" --source "D:\Clips"
```

The key comes from `GEMINI_API_KEY` or the one saved in the app. `--cache` defaults to the app's
cache folder (as set in Settings). The old local Ollama/whisper pipeline is at tag `legacy-local-pipeline`.
