# Auto Clip Compiler

Turn a folder of game clips into ranked **funny-moment highlight reels** — automatically.

Works on **any file-based clips** (OBS replay buffer, ShadowPlay, screen recordings, etc.). It finds
the funny moments by laughter (and optionally by what's *said*, using an LLM judge), trims each to a
clean in/out point, and exports rendered MP4 compilations plus DaVinci-Resolve timelines.

**Rule #0 — non-destructive.** Your source folder is the master copy and is *never* modified, moved, or
deleted. The tool only ever **copies** (and in `--all-games` / `--every-clip` it reads from source
without copying at all). All output goes to a separate folder.

---

## Quick start

```powershell
# 1. one-time external tools
winget install Gyan.FFmpeg
winget install UB-Mannheim.TesseractOCR

# 2. one-time Python setup (from this folder, in a NEW PowerShell)
./setup.ps1

# 3. sanity-check, then run
.\.venv\Scripts\python.exe autoclip.py --preflight 8 --source "D:\Clips"
.\.venv\Scripts\python.exe autoclip.py --source "D:\Clips" --output "D:\Highlights" --resolve --render --combine
```

---

## Setup guide (Windows / PowerShell)

> Linux/macOS work too — the only Windows-specific bits are the `winget` installs and the `.ps1`
> helper. Install ffmpeg/tesseract via your package manager and create the venv manually.

### 1. External tools (one-time)
```powershell
winget install Gyan.FFmpeg              # provides ffmpeg + ffprobe
winget install UB-Mannheim.TesseractOCR # OCR for game detection (skippable if you only use --all-games)
```
**Open a new PowerShell window afterward** so `ffmpeg`, `ffprobe`, and `tesseract` are on `PATH`.
The tool also auto-finds Tesseract at its default install path; otherwise pass
`--tesseract-path "C:\Program Files\Tesseract-OCR\tesseract.exe"`.

### 2. Python environment
Needs **Python 3.10–3.12** (TensorFlow on Windows does **not** support 3.13/3.14).
```powershell
cd <this folder>
./setup.ps1
```
`setup.ps1` finds a compatible Python, creates `.venv`, and installs dependencies from
`requirements.txt`. TensorFlow runs **CPU-only** on Windows — fine for this workload. The first run
downloads the YAMNet laughter model (~17 MB); `--smart-rank` additionally downloads a faster-whisper
model on first use.

Manual alternative:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. (Optional) LLM judge for content-aware ranking
`--smart-rank` transcribes each candidate and has an LLM rate it 1–10 for clip-worthiness. Pick one:

- **Free, via the Cline CLI (DeepSeek V4 Flash)** — no GPU, no cost on a Cline account:
  ```powershell
  npm i -g cline      # then run `cline` once to sign in
  ```
  Use it with `--smart-rank --llm-command "python cline_judge.py"`.
- **A paid API** (DeepSeek/OpenAI-compatible) — set `$env:DEEPSEEK_API_KEY` and use `--smart-rank`.
- **Local Ollama** — `--smart-rank --llm-base-url http://localhost:11434/v1 --llm-model <model> --llm-api-key ollama`.

---

## How it works

1. **Discover** clips in the read-only source folder.
2. **Game detection** (default) — samples frames per clip and uses Tesseract OCR on HUD text to sort
   **Rust** vs **CS2** (others → `OTHER`, left alone). Skip this entirely with `--all-games`.
3. **Find candidate moments** — YAMNet laughter detection, gated by audio energy. With `--every-clip`,
   clips with *no* laughter still get a window on their loudest beat (so silent/visual gags count).
4. **Rank** — by laughter, or with `--smart-rank` by an LLM's clip-worthiness score (1–10).
5. **Select** — the top `-n`, or *everything* above `--min-score`.
6. **Trim** — snap in/out points to natural pauses (`silencedetect`), erring toward keeping the setup.
7. **Export** — `manifest.csv` shot-list, `--resolve` FCPXML timelines, `--render` MP4 compilations.

---

## Usage modes

### Sorted by game (default)
Best for libraries of mostly Rust/CS2. Sorts copies and makes per-game + combined reels.
```powershell
.\.venv\Scripts\python.exe autoclip.py --source "D:\Clips" --output "D:\Out" `
  -n 30 --smart-rank --llm-command "python cline_judge.py" --resolve --render --combine
```

### All games, one mixed reel (`--all-games`)
Skips game detection/sorting — searches every clip and compiles one video. Reads from source (no
copying). Good for mixed-game libraries.
```powershell
.\.venv\Scripts\python.exe autoclip.py --source "D:\Clips" --output "D:\Out" `
  --all-games --smart-rank --llm-command "python cline_judge.py" `
  --min-score 6 --chronological --render --combine
```

### Every clip (`--every-clip`) — for hand-curated libraries
If you *already* hand-pick funny clips, laughter detection throws most of them away. `--every-clip`
treats **every** clip as a candidate (clips with no laugh get a window on their loudest beat), judges
them all, and **keeps the silent/visual ones** (a transcript judge can't score them, so it trusts your
curation) plus spoken clips scoring ≥ `--min-score` (default **3** in this mode). Implies `--all-games`
and `--smart-rank`.
```powershell
.\.venv\Scripts\python.exe autoclip.py --source "D:\Clips" --output "D:\Out" `
  --every-clip --llm-command "python cline_judge.py" `
  --min-score 3 --chronological --render --combine
```

> **Heads-up on scale:** `--every-clip` transcribes *every* clip (slow on CPU — budget time for big
> libraries), and a thousand clips can mean a multi-hour video. Raise `--min-score` to tighten it.

### Ordering
By default the compilation is funniest-first. Add `--chronological` to order it by recording time
(oldest first), parsed from the clip filenames.

Run `python autoclip.py --help` for every option.

---

## Smart re-ranking (`--smart-rank`)

By default moments are ranked by laughter loudness. `--smart-rank` adds a content pass: transcribe each
candidate with **faster-whisper**, then an **LLM** scores it 1–10 for clip-worthiness (judging *what was
said* + the laughter). The `transcript` and `llm_score` land in `manifest.csv`. Any failure falls back
to laughter ranking — it never crashes the run.

Three ways to wire the judge:

```powershell
# A) Free via the Cline CLI (DeepSeek V4 Flash) — uses cline_judge.py in this repo
python autoclip.py --smart-rank --llm-command "python cline_judge.py"
#    override model: $env:CLINE_JUDGE_MODEL = "deepseek/deepseek-v4-flash"

# B) OpenAI-compatible HTTP endpoint (DeepSeek's own API, OpenRouter, ...)
$env:DEEPSEEK_API_KEY = "sk-..."
python autoclip.py --smart-rank --llm-model deepseek-chat   # base-url defaults to api.deepseek.com

# C) Any CLI (e.g. Ollama) — prompt piped to stdin, JSON read from stdout
python autoclip.py --smart-rank --llm-command "ollama run llama3.1:8b"
```

Knobs: `--whisper-model` (tiny/base/small/medium), `--smart-pool` (how many candidates to judge,
`0` = all), `--smart-batch` (moments per LLM call), `--llm-timeout`.

> **Limitation — visual humor.** The judge only reads the *transcript*; it can't *see* the screen. A
> silent visual gag scores low on dialogue, which is why `--every-clip` deliberately keeps un-transcribed
> clips instead of dropping them. True visual-funny detection would need a vision model (not included).

---

## DaVinci Resolve import (free version)

External scripting needs Resolve **Studio**; the free version rejects it. So this tool exports
**FCPXML**, which the free version imports cleanly and non-destructively:

1. Run with `--resolve` (optionally `--combine`) → `Rust.fcpxml`, `CS2.fcpxml`, `Combined.fcpxml`.
2. In Resolve: **File ▸ Import ▸ Timeline…**, pick the `.fcpxml`.
3. Clips are laid out with the silence-aware in/out points, referencing the source read-only.

Prefer a finished video? `--render` writes ready-to-watch MP4s directly (no Resolve needed).

---

## Output layout
```
<output>\
  Rust\  CS2\  Unsorted\         # sorted COPIES (default mode only; originals stay in source)
  audio\                         # 16 kHz mono wavs (cache; safe to delete)
  manifest.csv                   # the shot-list: filename, game, scores, transcript, in/out
  Rust.fcpxml  CS2.fcpxml  [Combined.fcpxml]
  Rust_highlights.mp4  CS2_highlights.mp4  [Combined_highlights.mp4]   # with --render
  run.log
```
Rendered MP4s use **fixed names** and are overwritten each run — rename or change `--output` to keep
old versions.

---

## Tuning notes
- **Game detection** — if a real Rust/CS2 clip lands in `OTHER`, lower `--other-floor`; too many
  `Unsorted`, lower `--conf-threshold`. Or sidestep it with `--all-games`.
- **Laughter sensitivity** — lower `--laugh-threshold` for more (quieter) hits, raise for cleaner ones;
  `--energy-percentile` sets how loud a moment must be; `--wide-laughter` adds Snicker/Belly/Baby-laugh
  classes.
- **Bigger reels** — `--all-games` (include every game) and `--every-clip` (include every clip) both
  widen the funnel; `--min-score` controls the quality bar.
- Clips with no detected moment (default mode) or that fail to process are skipped and logged — the
  batch never crashes.
