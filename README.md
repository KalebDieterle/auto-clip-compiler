# obs-highlights

Auto-assemble funny-moment highlight drafts from a folder of OBS replay-buffer clips.

**Rule #0 — non-destructive.** Your source OBS folder is the master copy and is *never* modified,
moved, or deleted. The tool only ever **copies**. All output goes to a separate working folder.

## What it does
1. **Detect the game visually** — clips have no game info in the filename. Samples several frames per
   clip (spread across the whole video, not just the middle) and identifies **Rust** vs **CS2** with
   Tesseract OCR on HUD/UI text plus a colour-palette heuristic. Other games are left in the source
   folder, untouched and unprocessed.
2. **Sort copies** into `Rust/`, `CS2/`, `Unsorted/` (anything that looks like Rust/CS2 but can't be
   confidently called goes to `Unsorted/` for you to sort by hand — it never guesses).
3. **Find funny moments by laughter** — YAMNet (TensorFlow Hub) laughter detection, gated by audio
   energy so quiet false positives are dropped. Each moment gets a *funny score*.
4. **Rank** every moment and keep the **top N** (default 20).
5. **Silence-aware boundaries** — in/out points snap to natural conversational pauses
   (ffmpeg `silencedetect`), clamped to min/max, erring toward *including more* so setups aren't chopped.
6. **DaVinci Resolve timelines** — with `--resolve`, writes FCPXML files you import into Resolve.
7. **Manifest CSV** — your editing shot-list: filename, game, confidence, funny score, in/out times.

## Setup (Windows / PowerShell)

### 1. External tools (one-time)
```powershell
winget install Gyan.FFmpeg
winget install UB-Mannheim.TesseractOCR
```
Open a **new** PowerShell afterwards so `ffmpeg`, `ffprobe`, and `tesseract` are on `PATH`.
(If they aren't, pass `--ffmpeg`, `--ffprobe`, `--tesseract-path` explicitly.)

### 2. Python environment
```powershell
cd C:\Users\<user>\Documents\coding\obs-highlights
./setup.ps1
```
This creates `.venv` (Python 3.11 recommended) and installs the Python deps. TensorFlow on Windows is
**CPU-only** — that's fine for YAMNet at this scale. The first laughter run downloads the YAMNet model.

## Usage

Always start by sanity-checking game detection on a sample (nothing is copied):
```powershell
.\.venv\Scripts\Activate.ps1
python highlights.py --preflight 8
```
Confirm the Rust/CS2 column looks right. If detection is weak, tune `--conf-threshold`,
`--other-floor`, or `--frames` and re-run the pre-flight.

Full run (top 20, write Resolve timelines + a combined one):
```powershell
python highlights.py --resolve --combine
```

Common options:
```powershell
python highlights.py `
  --source "C:\Users\<user>\Videos\OBS Clips" `
  --output "C:\Users\<user>\Videos\Highlights" `
  -n 20 --min-dur 5 --max-dur 30 `
  --frames 4 --silence-db -30 --silence-dur 0.3 `
  --resolve --combine
```
Run `python highlights.py --help` for the full list.

## DaVinci Resolve import (free version)
External Python scripting requires Resolve **Studio**; the free version rejects external connections.
So this tool exports **FCPXML** instead, which the free version imports cleanly and non-destructively:

1. Run with `--resolve` (optionally `--combine`). You'll get `Rust.fcpxml`, `CS2.fcpxml`,
   and optionally `Combined.fcpxml` in the output folder.
2. In Resolve: **File ▸ Import ▸ Timeline…**, pick the `.fcpxml`.
3. Clips are laid out best-first by funny score with the silence-aware in/out points.
   They reference the **copied** clips read-only — refine the edit however you like.

## Output layout
```
<output>\
  Rust\  CS2\  Unsorted\     # sorted COPIES (originals stay in source)
  audio\                     # 16 kHz mono wavs (safe to delete)
  manifest.csv               # the shot-list
  Rust.fcpxml  CS2.fcpxml  [Combined.fcpxml]
  run.log
```

## Smart re-ranking (optional `--smart-rank`)
By default moments are ranked purely by laughter loudness/intensity. `--smart-rank` adds a content
pass: it transcribes each candidate moment with **faster-whisper** and asks an **LLM** to score it 1–10
for clip-worthiness (judging *what was said* + the laughter), then ranks by that. Laughter becomes one
signal among several. The `transcript` and `llm_score` land in `manifest.csv`. Any failure falls back to
laughter ranking — it never crashes the run.

The judge is provider-agnostic — two ways to wire it:

**A) OpenAI-compatible HTTP endpoint** (DeepSeek, OpenRouter, Ollama, …):
```powershell
# DeepSeek's own API (cheap):
$env:DEEPSEEK_API_KEY = "sk-..."
python highlights.py --smart-rank --llm-model deepseek-chat   # base-url defaults to api.deepseek.com

# A FREE model via OpenRouter:
$env:OPENAI_API_KEY = "sk-or-..."
python highlights.py --smart-rank `
  --llm-base-url https://openrouter.ai/api/v1 `
  --llm-model "deepseek/deepseek-chat-v3.1:free"
```

**B) Any CLI** (your Cline/Ollama/etc.) — pipe the prompt to its stdin, read the reply from stdout:
```powershell
python highlights.py --smart-rank --llm-command "ollama run deepseek-r1"
```
The command must accept the prompt on **stdin** and print the model's reply (containing the JSON array)
on **stdout**. Note: an interactive agent like Cline is a clumsy fit here — prefer mode A with a free
OpenRouter model if you can.

Knobs: `--whisper-model` (tiny/base/small/medium), `--smart-pool` (how many candidates to transcribe+judge,
default 150), `--smart-batch` (moments per LLM call), `--llm-timeout`.

## Tuning notes
- **Game detection** leans on OCR keywords (`RUST_KEYWORDS` / `CS2_KEYWORDS` in `highlights.py`) plus a
  colour heuristic. If a real Rust/CS2 clip lands in `OTHER`, lower `--other-floor`; if too many clips
  are `Unsorted`, lower `--conf-threshold`.
- **Laughter sensitivity** — raise `--laugh-threshold` for fewer/cleaner hits, lower for more.
  `--energy-percentile` controls how loud a moment must be to count. `--wide-laughter` also counts
  Snicker/Belly-laugh/Baby-laughter classes.
- Clips with no detected laughter or no confident game are skipped and logged — the batch never crashes.
