# Auto Clip Compiler

Turns a folder of game clips into a compilation of memorable moments with friends: original audio,
plain cuts, complete setup / payoff / reaction, and no footage played twice when the same moment was
saved in more than one replay.

**Status: work in progress, not a validated release.** Every component has automated tests, but
no full-library run or quality evaluation has been done yet. See `HANDOFF.md`.

**Rule #0: non-destructive.** Source clips and earlier compilations are never modified. Every render
goes to a new, uniquely named folder.

## Fully local

Analysis runs on your machine: no paid APIs, no uploads, no hosted fallback. The internet is used only
to install software and download models.

- **Speech:** faster-whisper (small, CPU int8), timestamped.
- **Laughter / loudness:** YAMNet + audio energy, as supporting evidence only (optional).
- **Judge:** Ollama `qwen3-vl:8b` on loopback, temperature 0, 32K context, one request at a time.
  It sees 1 frame per second of footage (4 per second around candidate payoffs) plus the transcript.
  These are still images and text, not raw audio.

## Setup (Windows / PowerShell)

```powershell
winget install Gyan.FFmpeg
winget install Ollama.Ollama
ollama pull qwen3-vl:8b
./setup.ps1
.\.venv\Scripts\python.exe setup_models.py --yamnet   # whisper + optional YAMNet into models\
```

## Use

```powershell
# Analyze 5 clips without rendering (measure timing before committing the GPU to a library)
.\.venv\Scripts\python.exe autoclip.py --preflight 5 --source "D:\Clips" --output "D:\Out"

# Full run: analyze, then render chronologically
.\.venv\Scripts\python.exe autoclip.py --source "D:\Clips" --output "D:\Out" --resolve

# Interrupted or partly failed? Completed stages are cached; resume the same run
.\.venv\Scripts\python.exe autoclip.py --resume "D:\Out\run-..."

# Review candidates in the browser (keep / maybe / reject, trim), then render from saved decisions
.\.venv\Scripts\python.exe autoclip.py --review "D:\Out\run-..."
.\.venv\Scripts\python.exe autoclip.py --render-from "D:\Out\run-..." --min-score 5
```

Useful options: `-n/--top N`, `--min-score 1..10`, `--ranked` (best first; default is chronological),
`--resolve` (FCPXML for DaVinci Resolve free: File > Import > Timeline), `--timestamp-overlay`,
`--audio-track K` (zero-based; needed when a file has several audio tracks and none is marked
default or mixed), `--allow-partial`, `--cpu`, `--llm-timeout` (default 300 s).

## Guarantees and their limits

- **Failures are never "maybe".** A clip that fails transcription or judging marks the run
  `incomplete`, and no normal compilation is published. Fix it and `--resume`, or pass
  `--allow-partial` to get a `partial-` render that lists every omission.
- **Review decisions stick.** A keep/reject/trim owns its interval. If a re-analysis describes the
  same event differently, the new candidate yields to your decision.
- **Duplicates.** Byte-identical files count as one recording. Overlapping replays count as shared
  only where 1 fps frames visibly matched and audio agreed, and only inside the matched span.
  Unverified overlap is kept, so it may repeat briefly rather than lose footage. Slices of one event
  play in timeline order, and joins between recordings never repeat a frame.
- **Not yet verified:** quality targets (90% recall on held-out favorites, no clipped essential
  setup or payoff), multitrack audio as imported into Resolve, and the throughput of the vision model.

## Output layout

```
<output>\
  .cache\                         # frames/audio by content hash (safe to delete)
  run-YYYYMMDD-HHMMSS-xxxxxxxx\
    run.sqlite                    # resumable ledger: sources, stages, candidates, reviews
    summary.json  candidate_report.csv  manifest.csv  selections.json
    model-responses\              # raw judge responses, for auditing
    render-\<token>\              # Combined_highlights.mp4, manifest.csv, Combined.fcpxml, render.json
    partial-\<token>\             # only with --allow-partial
```

## Evaluation

`evaluate.py prepare` builds a 40-clip, 20/20 grouped split (duplicates stay in one half, known
favorites go in both). After you label event intervals in `labels.csv`, `evaluate.py score` measures
the **published** cut list: favorite recall, unwanted inclusions and repeated seconds, all in shared
event coordinates.

## Migration from the laughter/LLM-command version

Retired: game sorting / Tesseract, laughter-gated selection, `--smart-rank` / `--every-clip` /
`--all-games` behavior (accepted as no-op aliases), and hosted or command judges (`--llm-command`,
`--llm-base-url`, ...). Every clip now gets full-context judging. Retired flags fail with an
explanation.

## Tests

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
These need FFmpeg on PATH. The models are faked, so the tests use no GPU.
