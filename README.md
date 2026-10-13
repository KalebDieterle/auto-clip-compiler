# Auto Clip Compiler

Turns a folder of game clips into compilations of the funny moments. Google's Gemini watches and
listens to each clip, scores what happens, and the app cuts the best moments from your original
full-quality clips into one video or a set of parts. You review every moment before anything is
rendered.

Windows 10/11 (64-bit). Your clips are never modified or moved.

## Install

1. Download `AutoClipCompiler-Setup-<version>.exe` and run it.
   Windows may say "Windows protected your PC" because the app isn't code-signed: click
   **More info**, then **Run anyway**.
2. No admin rights needed. It installs for your user only and adds a Start menu entry.

## Get a Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key.
2. Paste it into the app on first launch. It's stored in Windows Credential Manager, not in a file.

**Costs.** Judging costs about **$0.0045 per 30-second clip** (about $4.50 per 1,000 clips) at
Gemini 3.8 Flash's paid price (until 2026-12-31, when Google doubles it). The free tier only allows
about 20 clips a day, so turn on billing in AI Studio for a real library. Every clip is judged once:
results are saved, so re-running, changing settings or rendering again never costs anything. The app
shows an estimate before you start and stops at your budget cap.

## Use

1. **New**: pick your clips folder and an output folder, optionally a date range, and choose
   *One video* (a set length of the best moments, newest clips first) or *Split into parts*
   (everything good, in order, cut into parts of about N minutes).
2. **Judging**: watch progress, moments found and money spent. *Stop* keeps everything judged so far.
3. **Review**: every moment with its score and Gemini's reason. Click to watch it.
   - <kbd>K</kbd> keep, <kbd>X</kbd> cut, <kbd>&larr;</kbd>/<kbd>&rarr;</kbd> move, <kbd>Space</kbd> play
   - Drag the purple handles, or press <kbd>[</kbd> / <kbd>]</kbd> at the playhead, to trim.
4. **Render**: the final videos land in a `Compilation <date>` folder inside your output folder,
   with a DaVinci Resolve timeline (`Combined.fcpxml`, File > Import > Timeline) next to each.

Jobs run at below-normal priority so you can keep gaming. Closing the window stops the job; nothing
already judged is lost.

## Privacy

Each clip is shrunk to a 480p copy, uploaded to Google's Gemini Files API, judged, and deleted from
Google right after. Nothing else leaves your PC.

## Uninstall

Settings > Apps > Auto Clip Compiler > Uninstall. You'll be asked whether to also delete your
settings, saved judgments and stored API key (keep them if you might reinstall).

---

Developers: see [docs/DEVELOPING.md](docs/DEVELOPING.md).
