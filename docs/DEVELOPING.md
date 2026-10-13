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
| `packaging/` | `build.ps1` (assembles the signed-Python app folder), Inno Setup script, icon |

Data lives in `%LOCALAPPDATA%\AutoClipCompiler\` (`gemini-cache\`, `previews\`, `hash-index.json`,
`logs\`) and `%APPDATA%\AutoClipCompiler\settings.json`. The judgment cache is keyed by each clip's
SHA-256, so it survives renames and is shared by the app and the CLI. Each compilation is a
`Compilation <date>` folder in the output folder with `run.json` (moments + review decisions),
`report.json` and the rendered videos.

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

There is deliberately **no exe of our own**. Windows Smart App Control blocks unsigned executables it
has no reputation for (a PyInstaller exe was blocked on Kaleb's PC on 2026-10-13), and it has no
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
```

The key comes from `GEMINI_API_KEY` or the one saved in the app. `--cache` defaults to the app's
cache folder (as set in Settings). The old local Ollama/whisper pipeline is at tag `legacy-local-pipeline`.
