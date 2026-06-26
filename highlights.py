#!/usr/bin/env python3
"""
obs-highlights — auto-assemble funny-moment highlight drafts from OBS replay clips.

Pipeline:
  A. Discover clips in the (read-only) source folder.
  B. Detect game visually (ffmpeg frame sampling + Tesseract OCR + colour heuristic).
  C. Sort COPIES into Rust / CS2 / Unsorted. Other games are left in source only.
  D. Detect laughter (YAMNet) with an audio-energy gate.
  E. Rank every laughter moment, keep the top N.
  F. Snap clip in/out points to natural conversational pauses (ffmpeg silencedetect).
  G. Write a manifest CSV shot-list.
  H. Write DaVinci-Resolve-importable FCPXML timelines (per game + optional combined).

RULE #0: NON-DESTRUCTIVE. The source folder is the master copy. We only ever COPY.

See README.md for setup. Run `python highlights.py --preflight 8` first to sanity-check
game detection before committing to a full batch.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import numpy as np

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - tqdm is a hard dep, but degrade gracefully
    def tqdm(it, **kw):
        return it

log = logging.getLogger("highlights")

# --------------------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------------------
DEFAULT_SOURCE = r"C:\Users\<user>\Videos\OBS Clips"
DEFAULT_OUTPUT = r"C:\Users\<user>\Videos\Highlights"
VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".flv", ".m4v"}

# YAMNet laughter class indices (from yamnet_class_map.csv):
#   13 Laughter | 14 Baby laughter | 15 Giggle | 16 Snicker | 17 Belly laugh | 18 Chuckle/chortle
LAUGH_CLASSES_DEFAULT = [13, 15, 18]      # Laughter, Giggle, Chuckle (spec)
LAUGH_CLASSES_WIDE = [13, 14, 15, 16, 17, 18]

YAMNET_HOP_S = 0.48                        # YAMNet frame hop
YAMNET_WIN_S = 0.96                        # YAMNet patch window
SAMPLE_RATE = 16000

# Game-detection keyword dictionaries (matched case-insensitively as substrings).
CS2_KEYWORDS = [
    "counter-terrorists", "terrorists", "bomb has been planted", "planted the bomb",
    "the bomb has been", "defusing", "ct wins", "t wins", "mvp of the round", "mvp",
    "ak-47", "awp", "m4a1", "m4a4", "deagle", "headshot", "buy menu", "saving",
    "loadout", "counter-terrorist", "terrorist win", "tactical timeout", "ct start",
    "t start", "freeze time", "match is set to pause",
    # Death / spectate / killfeed overlays (replays often open here).
    "damage taken", "killed you", "in hits", "service medal", "stattrak", "operation",
    "mac-10", "mp9", "mp7", "famas", "galil", "p90", "nova", "xm1014", "tec-9",
    "five-seven", "usp-s", "glock", "ssg 08", "sg 553", "negev", "zeus",
]
RUST_KEYWORDS = [
    "tool cupboard", "building blocked", "building privilege", "building decaying",
    "sleeping bag", "workbench", "sulfur", "metal fragments", "blueprint", "authorize",
    "you died", "was killed by", "scrap", "recycler", "low grade fuel", "research table",
    "respawn", "sheet metal", "salvaged", "too cold", "too hot", "pistol bullet",
    "rifle bullet", "wood storage", "radiation",
]

OCR_HIT_WEIGHT = 2.0       # each distinct keyword hit
COMPASS_WEIGHT = 3.0       # Rust compass strip detected (strong, CS2 has no compass)
MONEY_WEIGHT = 1.0         # each CS2 "$<digits>" money readout (capped)
# Rust shows a heading compass across the top with NE/NW/SE/SW labels; CS2 never does.
COMPASS_RE = re.compile(r"\b(NE|NW|SE|SW)\b")
MONEY_RE = re.compile(r"\$\s?\d")


# --------------------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------------------
@dataclass(eq=False)  # eq=False -> identity hash, so ClipInfo can be a dict key
class ClipInfo:
    path: Path
    duration: float = 0.0
    fps: float = 30.0
    width: int = 1920
    height: int = 1080
    game: str = "OTHER"              # Rust | CS2 | Unsorted | OTHER
    rust_score: float = 0.0
    cs2_score: float = 0.0
    confidence: float = 0.0          # 0..1 margin
    conf_label: str = "low"
    copied_path: Path | None = None
    error: str | None = None


@dataclass
class Moment:
    clip: ClipInfo
    laugh_start: float
    laugh_end: float
    funny_score: float
    in_t: float = 0.0
    out_t: float = 0.0
    transcript: str = ""
    llm_score: float | None = None      # 1-10 clip-worthiness from --smart-rank


# --------------------------------------------------------------------------------------
# Subprocess / ffmpeg helpers
# --------------------------------------------------------------------------------------
def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def ffprobe_meta(clip: Path, ffprobe: str) -> tuple[float, float, int, int]:
    """Return (duration_s, fps, width, height). Raises on failure."""
    cp = run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,width,height",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1", str(clip),
    ])
    if cp.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {cp.stderr.strip()[:300]}")
    text = cp.stdout
    dur = _grab(text, r"duration=([\d.]+)")
    rfr = re.search(r"r_frame_rate=(\d+)/(\d+)", text)
    fps = (float(rfr.group(1)) / float(rfr.group(2))) if rfr and float(rfr.group(2)) else 30.0
    w = int(_grab(text, r"width=(\d+)") or 1920)
    h = int(_grab(text, r"height=(\d+)") or 1080)
    return float(dur or 0.0), fps, w, h


def _grab(text: str, pat: str):
    m = re.search(pat, text)
    return m.group(1) if m else None


def extract_frames(clip: Path, timestamps: list[float], out_dir: Path, ffmpeg: str) -> list[Path]:
    out_paths = []
    for i, ts in enumerate(timestamps):
        out = out_dir / f"frame_{i:02d}.png"
        cp = run([
            ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", str(clip),
            "-frames:v", "1", "-q:v", "2", str(out),
        ])
        if out.exists():
            out_paths.append(out)
        else:
            log.debug("frame grab failed @%.1fs for %s: %s", ts, clip.name,
                      cp.stderr.strip()[:200])
    return out_paths


def extract_audio(clip: Path, out_wav: Path, ffmpeg: str) -> bool:
    cp = run([
        ffmpeg, "-y", "-i", str(clip), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "wav", str(out_wav),
    ])
    return out_wav.exists() and cp.returncode == 0


def detect_silences(clip: Path, noise_db: float, min_sil: float, duration: float,
                    ffmpeg: str) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) via ffmpeg silencedetect."""
    cp = run([
        ffmpeg, "-i", str(clip), "-af",
        f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-",
    ])
    text = cp.stderr  # silencedetect logs to stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\-\d.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\-\d.]+)", text)]
    sils = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        sils.append((max(0.0, s), min(duration, e)))
    return sils


# --------------------------------------------------------------------------------------
# Phase B — game detection
# --------------------------------------------------------------------------------------
def _count_keywords(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def _frame_signals(img_path: Path) -> tuple[float, float]:
    """Return (rust_score, cs2_score) for one frame from OCR + compass + money cues.

    Colour heuristics are deliberately NOT used: Rust deserts have blue skies (false CS2)
    and CS2 maps are earthy (false Rust). HUD text + the Rust compass separate them cleanly.
    """
    import cv2  # lazy
    import pytesseract
    img = cv2.imread(str(img_path))
    if img is None:
        return 0.0, 0.0
    h = img.shape[0]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    try:
        text = pytesseract.image_to_string(gray).lower()
    except Exception as e:
        log.debug("OCR error on %s: %s", img_path.name, e)
        text = ""

    rust = OCR_HIT_WEIGHT * _count_keywords(text, RUST_KEYWORDS)
    cs2 = OCR_HIT_WEIGHT * _count_keywords(text, CS2_KEYWORDS)
    cs2 += MONEY_WEIGHT * min(3, len(MONEY_RE.findall(text)))

    # Compass strip across the very top (Rust only; CS2 has no compass). The text is small
    # and light-on-dark, so binarize + upscale and try two page-segmentation modes.
    strip = img[0:max(1, int(h * 0.07)), :]
    try:
        sgray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        sgray = cv2.resize(sgray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
        _, sbin = cv2.threshold(sgray, 150, 255, cv2.THRESH_BINARY)
        # Single-line (psm 7) pass on the binarized strip — clean enough that a standalone
        # NE/NW/SE/SW token is Rust-specific (CS2 has no compass). Optional heading-number
        # corroboration bumps confidence but isn't required, to preserve recall.
        stext = pytesseract.image_to_string(sbin, config="--psm 7").upper()
        if COMPASS_RE.search(stext):
            headings = {int(x) for x in re.findall(r"\b\d{1,3}\b", stext)
                        if int(x) % 10 == 0 and 0 <= int(x) <= 350}
            rust += COMPASS_WEIGHT + (1.0 if len(headings) >= 3 else 0.0)
    except Exception as e:
        log.debug("compass OCR error on %s: %s", img_path.name, e)
    return rust, cs2


def detect_clip(clip: Path, args, tmp_root: Path) -> ClipInfo:
    info = ClipInfo(path=clip)
    try:
        info.duration, info.fps, info.width, info.height = ffprobe_meta(clip, args.ffprobe)
    except Exception as e:
        info.error = f"probe: {e}"
        log.warning("Skipping %s (probe failed): %s", clip.name, e)
        return info

    if info.duration <= 0:
        info.error = "zero duration"
        return info

    fracs = np.linspace(0.1, 0.9, max(1, args.frames))
    timestamps = [float(f) * info.duration for f in fracs]

    with tempfile.TemporaryDirectory(dir=tmp_root) as td:
        frames = extract_frames(clip, timestamps, Path(td), args.ffmpeg)
        if not frames:
            info.error = "no frames extracted"
            log.warning("Skipping %s (no frames).", clip.name)
            return info

        rust_total = cs2_total = 0.0
        for fp in frames:
            rc, cc = _frame_signals(fp)
            rust_total += rc
            cs2_total += cc

    info.rust_score = round(rust_total, 3)
    info.cs2_score = round(cs2_total, 3)

    winner_score = max(rust_total, cs2_total)
    margin = (abs(rust_total - cs2_total) / (rust_total + cs2_total)) if (rust_total + cs2_total) > 0 else 0.0
    info.confidence = round(margin, 3)

    if winner_score < args.other_floor:
        info.game = "OTHER"
        info.conf_label = "n/a"
    elif margin >= args.conf_threshold:
        info.game = "Rust" if rust_total > cs2_total else "CS2"
        info.conf_label = "high" if margin >= 0.6 else "medium"
    else:
        info.game = "Unsorted"
        info.conf_label = "low"
    return info


# --------------------------------------------------------------------------------------
# Phase C — sort copies
# --------------------------------------------------------------------------------------
def unique_dest(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suf = Path(name).stem, Path(name).suffix
    i = 1
    while (dest_dir / f"{stem}_{i}{suf}").exists():
        i += 1
    return dest_dir / f"{stem}_{i}{suf}"


def sort_copies(infos: list[ClipInfo], out: Path) -> None:
    for g in ("Rust", "CS2", "Unsorted"):
        (out / g).mkdir(parents=True, exist_ok=True)
    for info in infos:
        if info.error or info.game == "OTHER":
            if info.game == "OTHER":
                log.info("LEFT IN SOURCE (other/no-match): %s", info.path.name)
            continue
        if info.copied_path and info.copied_path.exists():
            continue  # already placed (cached from a previous run)
        existing = out / info.game / info.path.name
        if existing.exists() and existing.stat().st_size == info.path.stat().st_size:
            info.copied_path = existing  # idempotent: identical copy already present
            continue
        dest = unique_dest(out / info.game, info.path.name)
        shutil.copy2(info.path, dest)        # copy2 = never touches source
        info.copied_path = dest
        log.info("COPIED -> %s\\%s", info.game, dest.name)


# --------------------------------------------------------------------------------------
# Phase D — laughter detection (YAMNet)
# --------------------------------------------------------------------------------------
_YAMNET = None


def load_yamnet():
    global _YAMNET
    if _YAMNET is None:
        log.info("Loading YAMNet model (first run downloads it)...")
        import tensorflow_hub as hub
        _YAMNET = hub.load("https://tfhub.dev/google/yamnet/1")
    return _YAMNET


def read_wav(path: Path) -> np.ndarray:
    from scipy.io import wavfile
    sr, data = wavfile.read(str(path))
    if data.ndim > 1:
        data = data[:, 0]
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    else:
        data = data.astype(np.float32)
    return data


def find_moments(wav: np.ndarray, laugh_idx: list[int], laugh_thr: float,
                 energy_pct: float) -> list[tuple[float, float, float]]:
    """Return list of (start_s, end_s, funny_score)."""
    model = load_yamnet()
    scores, _, _ = model(wav)
    scores = scores.numpy()                         # [num_frames, 521]
    n = scores.shape[0]
    if n == 0:
        return []
    laugh_prob = scores[:, laugh_idx].max(axis=1)   # per frame

    # Per-frame RMS energy aligned to YAMNet hop.
    hop = int(YAMNET_HOP_S * SAMPLE_RATE)
    win = int(YAMNET_WIN_S * SAMPLE_RATE)
    rms = np.zeros(n, dtype=np.float32)
    for i in range(n):
        seg = wav[i * hop: i * hop + win]
        if seg.size:
            rms[i] = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
    energy_gate = np.percentile(rms, energy_pct) if rms.size else 0.0
    rms_max = float(rms.max()) or 1.0

    qualifies = (laugh_prob >= laugh_thr) & (rms >= energy_gate)

    moments = []
    i = 0
    while i < n:
        if not qualifies[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j + 1 < n and (qualifies[j + 1] or gap < 1):
            if not qualifies[j + 1]:
                gap += 1
            else:
                gap = 0
            j += 1
        # Trim trailing non-qualifying frame absorbed by the gap allowance.
        while j > i and not qualifies[j]:
            j -= 1
        start_s = i * YAMNET_HOP_S
        end_s = j * YAMNET_HOP_S + YAMNET_WIN_S
        peak_laugh = float(laugh_prob[i:j + 1].max())
        peak_rms = float(rms[i:j + 1].max()) / rms_max
        funny = peak_laugh * (0.5 + 0.5 * peak_rms)
        moments.append((start_s, end_s, round(funny, 4)))
        i = j + 1
    return moments


# --------------------------------------------------------------------------------------
# Phase F — silence-aware boundaries
# --------------------------------------------------------------------------------------
def compute_boundaries(m: Moment, silences: list[tuple[float, float]],
                       duration: float, min_dur: float, max_dur: float) -> tuple[float, float]:
    ls, le = m.laugh_start, m.laugh_end
    search = max_dur * 1.5

    # In-point: start of the nearest pause before the laughter (include the pause + setup).
    in_t = None
    for s_start, _ in sorted([s for s in silences if s[0] <= ls], key=lambda x: -x[0]):
        if ls - s_start <= search:
            in_t = s_start
            break
    if in_t is None:
        in_t = max(0.0, le - max_dur * 0.7)

    # Out-point: end of the nearest pause after the laughter settles.
    out_t = None
    for _, s_end in sorted([s for s in silences if s[1] >= le], key=lambda x: x[1]):
        if s_end - le <= search:
            out_t = s_end
            break
    if out_t is None:
        out_t = min(duration, le + max_dur * 0.4)

    in_t, out_t = min(in_t, ls), max(out_t, le)     # always keep the laughter inside

    # Clamp to min/max, erring toward MORE (keep laughter + as much setup as fits).
    if out_t - in_t < min_dur:
        deficit = min_dur - (out_t - in_t)
        in_t = max(0.0, in_t - deficit * 0.6)
        out_t = min(duration, in_t + min_dur)
        in_t = max(0.0, out_t - min_dur)
    if out_t - in_t > max_dur:
        tail = min(out_t - le, max_dur * 0.3)
        out_t = le + tail
        in_t = out_t - max_dur
        if in_t < 0:
            in_t, out_t = 0.0, min(duration, max_dur)

    return max(0.0, round(in_t, 3)), min(duration, round(out_t, 3))


# --------------------------------------------------------------------------------------
# Phase H — FCPXML
# --------------------------------------------------------------------------------------
def _file_url(p: Path) -> str:
    return "file://localhost/" + quote(str(p).replace("\\", "/"), safe="/:")


def _t(seconds: float, fps: int) -> str:
    return f"{round(seconds * fps)}/{fps}s"


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def write_fcpxml(path: Path, project_name: str, moments: list[Moment], fps: int,
                 width: int, height: int) -> None:
    """Write an FCPXML 1.8 timeline. Clips ordered as given (best first)."""
    frame_dur = _t(1.0 / fps, fps)  # "1/{fps}s"

    # One asset per unique source clip.
    assets: dict[str, tuple[str, Moment]] = {}
    order = []
    for m in moments:
        key = str(m.clip.copied_path or m.clip.path)
        if key not in assets:
            aid = f"a{len(assets) + 1}"
            assets[key] = (aid, m)
            order.append(key)

    res = [f'    <format id="r1" name="FFVideoFormat{fps}p" frameDuration="{frame_dur}" '
           f'width="{width}" height="{height}"/>']
    for key in order:
        aid, m = assets[key]
        src = m.clip.copied_path or m.clip.path
        res.append(
            f'    <asset id="{aid}" name="{_xml_escape(src.stem)}" '
            f'src="{_file_url(src)}" start="0s" '
            f'duration="{_t(m.clip.duration, fps)}" hasVideo="1" hasAudio="1" '
            f'format="r1" audioSources="1" audioChannels="2"/>'
        )

    spine = []
    offset = 0.0
    for m in moments:
        key = str(m.clip.copied_path or m.clip.path)
        aid, _ = assets[key]
        dur = max(1.0 / fps, m.out_t - m.in_t)
        spine.append(
            f'          <asset-clip ref="{aid}" '
            f'name="{_xml_escape((m.clip.copied_path or m.clip.path).stem)}" '
            f'offset="{_t(offset, fps)}" start="{_t(m.in_t, fps)}" '
            f'duration="{_t(dur, fps)}" format="r1" tcFormat="NDF"/>'
        )
        offset += dur

    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
  <resources>
{chr(10).join(res)}
  </resources>
  <library>
    <event name="OBS Highlights">
      <project name="{_xml_escape(project_name)}">
        <sequence format="r1" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(spine)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""
    path.write_text(doc, encoding="utf-8")
    log.info("Wrote timeline: %s (%d clips)", path.name, len(moments))


# --------------------------------------------------------------------------------------
# Phase G — manifest
# --------------------------------------------------------------------------------------
def write_manifest(path: Path, moments: list[Moment]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "filename", "game", "classification_confidence", "funny_score",
                    "llm_score", "transcript", "in_timestamp", "out_timestamp",
                    "clip_duration", "source_path", "copied_path"])
        for i, m in enumerate(moments, 1):
            c = m.clip
            w.writerow([i, c.path.name, c.game, c.confidence, m.funny_score,
                        "" if m.llm_score is None else m.llm_score,
                        (m.transcript or "")[:300],
                        _fmt_ts(m.in_t), _fmt_ts(m.out_t), round(c.duration, 2),
                        str(c.path), str(c.copied_path or "")])
    log.info("Wrote manifest: %s (%d rows)", path.name, len(moments))


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = seconds % 60
    return f"{h:02d}:{mm:02d}:{ss:06.3f}"


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def discover_clips(source: Path) -> list[Path]:
    return sorted([p for p in source.iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXTS])


def _existing_sort(out: Path) -> dict[str, tuple[str, Path]]:
    """Map filename -> (game, copied_path) for clips sorted on a previous run."""
    seen: dict[str, tuple[str, Path]] = {}
    for g in ("Rust", "CS2", "Unsorted"):
        d = out / g
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    seen.setdefault(f.name, (g, f))
    return seen


def detect_all(clips: list[Path], args, tmp_root: Path) -> list[ClipInfo]:
    results: list[ClipInfo] = []
    # Reuse a previous run's classification (deterministic for the same clip) so re-runs
    # with a different -n / --render don't re-OCR the whole folder. --redetect forces fresh.
    cached = {} if getattr(args, "redetect", False) else _existing_sort(Path(args.output))
    todo: list[Path] = []
    for c in clips:
        if c.name in cached:
            game, cp = cached[c.name]
            info = ClipInfo(path=c, game=game, copied_path=cp, confidence=1.0,
                            conf_label="cached")
            try:
                info.duration, info.fps, info.width, info.height = ffprobe_meta(c, args.ffprobe)
            except Exception as e:
                info.error = f"probe: {e}"
            results.append(info)
        else:
            todo.append(c)
    if cached:
        log.info("Reusing prior classification for %d clips; detecting %d new.",
                 len(results), len(todo))
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(detect_clip, c, args, tmp_root): c for c in todo}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="Detecting game", unit="clip"):
                results.append(fut.result())
    # Preserve original discovery order for stable output.
    order = {c: i for i, c in enumerate(clips)}
    results.sort(key=lambda r: order.get(r.path, 0))
    return results


def print_detection_table(infos: list[ClipInfo]) -> None:
    print()
    print(f"{'CLIP':<42} {'RUST':>7} {'CS2':>7} {'CONF':>6} {'DECISION':>10}")
    print("-" * 76)
    for info in infos:
        name = info.path.name
        if len(name) > 41:
            name = name[:38] + "..."
        dec = info.game if not info.error else "ERROR"
        print(f"{name:<42} {info.rust_score:>7.2f} {info.cs2_score:>7.2f} "
              f"{info.confidence:>6.2f} {dec:>10}")
    print("-" * 76)
    counts = {}
    for info in infos:
        counts[info.game if not info.error else "ERROR"] = counts.get(
            info.game if not info.error else "ERROR", 0) + 1
    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print()


def process_laughter(infos: list[ClipInfo], args, tmp_root: Path) -> list[Moment]:
    """Extract audio in parallel, then run YAMNet serially (single model in memory)."""
    targets = [i for i in infos if i.copied_path and not i.error]
    if not targets:
        return []

    audio_dir = Path(args.output) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    # Parallel audio extraction (ffmpeg, I/O bound). Reuse wavs from a previous run.
    wavs: dict[ClipInfo, Path] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for info in targets:
            wav = audio_dir / (info.copied_path.stem + ".wav")
            if wav.exists() and wav.stat().st_size > 1024:
                wavs[info] = wav
                continue
            futs[ex.submit(extract_audio, info.copied_path, wav, args.ffmpeg)] = (info, wav)
        for fut in tqdm(as_completed(futs), total=len(futs), desc="Extracting audio", unit="clip"):
            info, wav = futs[fut]
            try:
                if fut.result():
                    wavs[info] = wav
                else:
                    log.warning("Audio extraction failed: %s", info.copied_path.name)
            except Exception as e:
                log.warning("Audio extraction error %s: %s", info.copied_path.name, e)

    laugh_idx = LAUGH_CLASSES_WIDE if args.wide_laughter else LAUGH_CLASSES_DEFAULT

    # Serial YAMNet inference (keep one model loaded).
    all_moments: list[Moment] = []
    for info, wav in tqdm(wavs.items(), total=len(wavs), desc="Detecting laughter", unit="clip"):
        try:
            data = read_wav(wav)
            for (s, e, score) in find_moments(data, laugh_idx, args.laugh_threshold,
                                              args.energy_percentile):
                all_moments.append(Moment(clip=info, laugh_start=s, laugh_end=e, funny_score=score))
        except Exception as ex_:
            log.warning("Laughter detection failed for %s: %s", info.copied_path.name, ex_)
    log.info("Found %d laughter moments across %d clips.", len(all_moments), len(wavs))
    return all_moments


def assign_boundaries(moments: list[Moment], args) -> None:
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for m in moments:
            clip = m.clip.copied_path or m.clip.path
            futs[ex.submit(detect_silences, clip, args.silence_db, args.silence_dur,
                           m.clip.duration, args.ffmpeg)] = m
        for fut in tqdm(as_completed(futs), total=len(futs), desc="Snapping boundaries", unit="clip"):
            m = futs[fut]
            try:
                sils = fut.result()
            except Exception as e:
                log.warning("silencedetect failed for %s: %s", m.clip.path.name, e)
                sils = []
            m.in_t, m.out_t = compute_boundaries(m, sils, m.clip.duration,
                                                 args.min_dur, args.max_dur)


# --------------------------------------------------------------------------------------
# Phase E2 — smart re-ranking: transcript (faster-whisper) + LLM clip-worthiness judge.
# Optional (--smart-rank). Falls back to laughter ranking on any failure.
# --------------------------------------------------------------------------------------
_WHISPER = None


def load_whisper(args):
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        log.info("Loading faster-whisper '%s' (first run downloads it)...", args.whisper_model)
        _WHISPER = WhisperModel(args.whisper_model, device="cpu", compute_type="int8")
    return _WHISPER


def transcribe_moments(moments: list[Moment], args) -> None:
    """Transcribe each moment's audio window from its already-extracted 16k wav."""
    model = load_whisper(args)
    audio_dir = Path(args.output) / "audio"
    cache: dict[Path, np.ndarray] = {}
    for m in tqdm(moments, desc="Transcribing", unit="clip"):
        try:
            src = m.clip.copied_path or m.clip.path
            wavp = audio_dir / (src.stem + ".wav")
            if not wavp.exists():
                continue
            if wavp not in cache:
                cache[wavp] = read_wav(wavp)
            data = cache[wavp]
            seg = data[max(0, int(m.in_t * SAMPLE_RATE)): int(m.out_t * SAMPLE_RATE)]
            if seg.size < SAMPLE_RATE // 2:
                continue
            segments, _ = model.transcribe(seg, language="en", vad_filter=True)
            m.transcript = " ".join(s.text.strip() for s in segments).strip()
        except Exception as e:
            log.warning("Transcription failed for %s: %s", m.clip.path.name, e)


def _judge_prompt(batch: list[Moment]) -> str:
    head = (
        "You rate moments from a gaming session (Rust / Counter-Strike 2) for a FUNNY "
        "highlights compilation. For each moment, output an integer 1-10 for how clip-worthy "
        "it is: judge what was SAID (the transcript) and the laughter intensity together. "
        "10=must-include banger, 1=boring/nothing happens. Reward genuine reactions, jokes, "
        "clutch/fail callouts, banter. Penalize silence or filler.\n"
        'Reply with ONLY a JSON array, one object per moment, e.g. '
        '[{"i":0,"score":7},{"i":1,"score":3}] — no other text.\n'
    )
    rows = []
    for idx, m in enumerate(batch):
        t = (m.transcript or "(no speech detected)").replace("\n", " ")[:500]
        rows.append(f'Moment {idx}: game={m.clip.game}, laughter={m.funny_score:.2f}, '
                    f'len={m.out_t - m.in_t:.0f}s\nTranscript: "{t}"')
    return head + "\n" + "\n\n".join(rows)


def llm_complete(prompt: str, args) -> str:
    """Either pipe the prompt to --llm-command's stdin, or POST to an OpenAI-compatible API."""
    if args.llm_command:
        # Run via the shell so any command line works (incl. Windows paths/args).
        cp = subprocess.run(args.llm_command, shell=True, input=prompt,
                            capture_output=True, text=True, timeout=args.llm_timeout)
        if cp.returncode != 0:
            raise RuntimeError(f"llm-command exit {cp.returncode}: {cp.stderr.strip()[:200]}")
        return cp.stdout
    key = (args.llm_api_key or os.environ.get("OPENAI_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY") or "")
    url = args.llm_base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": args.llm_model, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=args.llm_timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


def _parse_scores(text: str) -> dict[int, float]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return {}
    out: dict[int, float] = {}
    for o in arr:
        try:
            out[int(o["i"])] = float(o["score"])
        except Exception:
            continue
    return out


def judge_moments(moments: list[Moment], args) -> None:
    b = max(1, args.smart_batch)
    for start in tqdm(range(0, len(moments), b), desc="LLM judging", unit="batch"):
        batch = moments[start:start + b]
        try:
            scores = _parse_scores(llm_complete(_judge_prompt(batch), args))
            for idx, m in enumerate(batch):
                if idx in scores:
                    m.llm_score = max(1.0, min(10.0, scores[idx]))
        except Exception as e:
            log.warning("LLM judge batch @%d failed (%s); those keep laughter ranking.", start, e)


def smart_rank(moments: list[Moment], args) -> None:
    transcribe_moments(moments, args)
    judge_moments(moments, args)
    scored = sum(1 for m in moments if m.llm_score is not None)
    log.info("smart-rank: %d/%d candidates scored by the LLM judge.", scored, len(moments))


def build_timelines(moments: list[Moment], args) -> None:
    out = Path(args.output)
    fps, w, h = args.fps, 1920, 1080
    if moments:
        w, h = moments[0].clip.width, moments[0].clip.height

    for game in ("Rust", "CS2"):
        g_moments = [m for m in moments if m.clip.game == game]
        if g_moments:
            write_fcpxml(out / f"{game}.fcpxml", f"{game} Highlights", g_moments, fps, w, h)

    if args.combine and moments:
        write_fcpxml(out / "Combined.fcpxml", "Combined Highlights", moments, fps, w, h)


def dedup_moments(ranked: list[Moment], overlap: float = 0.5) -> list[Moment]:
    """Drop lower-ranked moments whose in/out window overlaps an already-kept moment
    from the SAME clip by more than `overlap` (so one clip-region can't appear twice)."""
    kept: list[Moment] = []
    for m in ranked:
        dup = False
        for k in kept:
            if k.clip is m.clip:
                lo, hi = max(k.in_t, m.in_t), min(k.out_t, m.out_t)
                ov = max(0.0, hi - lo)
                shorter = min(k.out_t - k.in_t, m.out_t - m.in_t) or 1.0
                if ov > overlap * shorter:
                    dup = True
                    break
        if not dup:
            kept.append(m)
    return kept


# --------------------------------------------------------------------------------------
# Phase I — render finished MP4s (cut + concat with ffmpeg, no Resolve needed)
# --------------------------------------------------------------------------------------
def render_videos(moments: list[Moment], args) -> list[Path]:
    """Cut each selected moment at its in/out and concatenate into finished MP4s.

    Segments are normalised to a common resolution/fps/codec so the concat demuxer can
    stream-copy them together. Writes per-game files plus Combined (with --combine).
    """
    out = Path(args.output)
    tmp = out / ".render_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    h = args.render_height
    w = int(round(h * 16 / 9 / 2) * 2)   # 16:9, even width
    fps = args.fps

    def encode(idx: int, m: Moment) -> Path | None:
        src = m.clip.copied_path or m.clip.path
        seg = tmp / f"seg_{idx:03d}.mp4"
        dur = max(0.1, m.out_t - m.in_t)
        cp = run([
            args.ffmpeg, "-y", "-i", str(src), "-ss", f"{m.in_t:.3f}", "-t", f"{dur:.3f}",
            "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(seg),
        ])
        if seg.exists() and seg.stat().st_size > 0:
            return seg
        log.warning("Segment encode failed (#%d %s): %s", idx, src.name, cp.stderr.strip()[-200:])
        return None

    segs: dict[int, Path | None] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(encode, i, m): i for i, m in enumerate(moments)}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="Rendering segments", unit="clip"):
            i = futs[fut]
            try:
                segs[i] = fut.result()
            except Exception as e:
                log.warning("Segment encode error #%d: %s", i, e)
                segs[i] = None

    def concat(indices: list[int], outfile: Path) -> None:
        ordered = [segs[i] for i in indices if segs.get(i)]
        if not ordered:
            return
        listfile = tmp / (outfile.stem + "_list.txt")
        listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in ordered), encoding="utf-8")
        cp = run([args.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
                  "-c", "copy", "-movflags", "+faststart", str(outfile)])
        if outfile.exists():
            log.info("Rendered %s (%d clips)", outfile.name, len(ordered))
        else:
            log.warning("Concat failed for %s: %s", outfile.name, cp.stderr.strip()[-300:])

    written: list[Path] = []
    for game in ("Rust", "CS2"):
        idxs = [i for i, m in enumerate(moments) if m.clip.game == game]
        if idxs:
            f = out / f"{game}_highlights.mp4"
            concat(idxs, f)
            written.append(f)
    if args.combine:
        f = out / "Combined_highlights.mp4"
        concat(list(range(len(moments))), f)
        written.append(f)

    shutil.rmtree(tmp, ignore_errors=True)
    return [f for f in written if f.exists()]


def setup_logging(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(out / "run.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)


def check_binaries(args) -> None:
    for tool, flag in ((args.ffmpeg, "--ffmpeg"), (args.ffprobe, "--ffprobe")):
        if shutil.which(tool) is None and not Path(tool).exists():
            sys.exit(f"ERROR: '{tool}' not found. Install ffmpeg (winget install Gyan.FFmpeg) "
                     f"or pass {flag} <path>.")
    if args.tesseract_path:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = args.tesseract_path


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Auto-assemble funny-moment highlight drafts from OBS clips (non-destructive).")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="OBS clips folder (read-only).")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Working/output folder.")
    p.add_argument("-n", "--top", type=int, default=20, help="Number of top moments (default 20).")
    p.add_argument("--min-dur", type=float, default=5.0, help="Min clip length, s (default 5).")
    p.add_argument("--max-dur", type=float, default=30.0, help="Max clip length, s (default 30).")
    p.add_argument("--frames", type=int, default=6, help="Frames sampled per clip for game detection.")
    p.add_argument("--silence-db", type=float, default=-30.0, help="silencedetect noise floor, dB.")
    p.add_argument("--silence-dur", type=float, default=0.3, help="Min silence duration, s.")
    p.add_argument("--conf-threshold", type=float, default=0.35,
                   help="Min score margin (0..1) to confidently pick a game; else Unsorted.")
    p.add_argument("--other-floor", type=float, default=2.0,
                   help="Min winning score to be considered Rust/CS2 at all; below = OTHER (left in source).")
    p.add_argument("--laugh-threshold", type=float, default=0.30, help="YAMNet laughter prob threshold.")
    p.add_argument("--energy-percentile", type=float, default=70.0,
                   help="Audio-energy gate percentile (cuts quiet false positives).")
    p.add_argument("--wide-laughter", action="store_true",
                   help="Also count Snicker/Belly laugh/Baby laughter classes.")
    p.add_argument("--fps", type=int, default=60, help="Timeline frame rate for FCPXML (default 60).")
    p.add_argument("--resolve", action="store_true", help="Write FCPXML timelines for DaVinci Resolve.")
    p.add_argument("--render", action="store_true",
                   help="Export finished MP4 compilation(s) with ffmpeg (no Resolve needed).")
    p.add_argument("--render-height", type=int, default=1080,
                   help="Output height for rendered MP4s (16:9, default 1080).")
    p.add_argument("--combine", action="store_true",
                   help="Also write a single combined timeline/video across both games.")
    p.add_argument("--redetect", action="store_true",
                   help="Force fresh game detection even if clips were sorted on a prior run.")
    # --- smart re-ranking (optional): transcript + LLM clip-worthiness judge ---
    p.add_argument("--smart-rank", action="store_true",
                   help="Transcribe candidates (faster-whisper) and re-rank by an LLM "
                        "clip-worthiness score instead of raw laughter.")
    p.add_argument("--whisper-model", default="base",
                   help="faster-whisper model: tiny/base/small/medium (default base).")
    p.add_argument("--smart-pool", type=int, default=150,
                   help="Max candidate moments to transcribe+judge (<=0 = all candidates).")
    p.add_argument("--smart-batch", type=int, default=12, help="Moments per LLM judging request.")
    p.add_argument("--llm-base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
                   help="OpenAI-compatible chat endpoint (DeepSeek, OpenRouter, Ollama, ...).")
    p.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
                   help="LLM model id used for judging.")
    p.add_argument("--llm-api-key", default=None,
                   help="API key (else OPENAI_API_KEY / DEEPSEEK_API_KEY env var).")
    p.add_argument("--llm-command", default=os.environ.get("LLM_COMMAND"),
                   help="Instead of HTTP: pipe the prompt to this CLI's stdin and read JSON "
                        "from stdout (e.g. your Cline/Ollama wrapper).")
    p.add_argument("--llm-timeout", type=float, default=120.0, help="LLM call timeout, seconds.")
    p.add_argument("--preflight", nargs="?", type=int, const=8, default=None, metavar="K",
                   help="Game-detect the first K clips (default 8), print results, and exit.")
    p.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1),
                   help="Parallel workers for ffmpeg/OCR/audio.")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg path/command.")
    p.add_argument("--ffprobe", default="ffprobe", help="ffprobe path/command.")
    p.add_argument("--tesseract-path", default=None, help="Full path to tesseract.exe if not on PATH.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    source = Path(args.source)
    out = Path(args.output)

    if not source.is_dir():
        sys.exit(f"ERROR: source folder not found: {source}")

    setup_logging(out)
    check_binaries(args)

    tmp_root = out / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    clips = discover_clips(source)
    if not clips:
        sys.exit(f"No video clips found in {source}")
    log.info("Discovered %d clips in %s", len(clips), source)

    # ---- Pre-flight: detect a sample, print, exit. No copying, no audio. ----
    if args.preflight is not None:
        k = max(1, args.preflight)
        if k >= len(clips):
            sample = clips
        else:  # evenly spaced across the whole folder so both games are represented
            idx = np.linspace(0, len(clips) - 1, k).round().astype(int)
            sample = [clips[i] for i in sorted(set(idx))]
        log.info("PRE-FLIGHT: classifying %d clips spread across the folder "
                 "(no files will be copied).", len(sample))
        infos = detect_all(sample, args, tmp_root)
        print_detection_table(infos)
        print("Pre-flight only. Re-run without --preflight to process the full batch.")
        return

    # ---- Phase B: detect all ----
    infos = detect_all(clips, args, tmp_root)
    print_detection_table(infos)

    # ---- Phase C: sort copies (source never touched) ----
    sort_copies(infos, out)

    # ---- Phase D: laughter ----
    moments = process_laughter(infos, args, tmp_root)
    if not moments:
        log.warning("No laughter moments detected. Manifest/timelines will be empty.")
        write_manifest(out / "manifest.csv", [])
        return

    # ---- Phase E: rank. Take an oversized candidate window, snap boundaries, de-dup
    #      overlapping moments from the same clip, THEN trim to top N. ----
    moments.sort(key=lambda m: m.funny_score, reverse=True)
    candidates = moments[: max(args.top * 3, args.top)]

    # ---- Phase F: silence-aware boundaries (on candidates) ----
    assign_boundaries(candidates, args)

    # ---- Phase E2 (optional): transcript + LLM clip-worthiness re-ranking ----
    if args.smart_rank:
        pool = candidates if args.smart_pool <= 0 else candidates[:args.smart_pool]
        smart_rank(pool, args)
        # LLM-scored moments rank first (by score), unscored fall back to laughter order.
        candidates.sort(key=lambda m: (m.llm_score is not None, m.llm_score or 0.0,
                                       m.funny_score), reverse=True)

    top = dedup_moments(candidates)[:args.top]
    log.info("Selected top %d of %d moments (after de-dup).", len(top), len(moments))

    # ---- Phase G: manifest ----
    write_manifest(out / "manifest.csv", top)

    # ---- Phase H: FCPXML timelines ----
    if args.resolve:
        build_timelines(top, args)
    else:
        log.info("Skipping FCPXML (pass --resolve to write DaVinci Resolve timelines).")

    # ---- Phase I: rendered MP4 compilation ----
    rendered = []
    if args.render:
        rendered = render_videos(top, args)
    else:
        log.info("Skipping render (pass --render to export finished MP4s).")

    # Cleanup temp frame dirs (leave audio + outputs).
    try:
        shutil.rmtree(tmp_root, ignore_errors=True)
    except Exception:
        pass

    log.info("Done. Outputs in %s", out)
    outs = ["manifest.csv"]
    if args.resolve:
        outs.append("*.fcpxml")
    if rendered:
        outs.append(", ".join(f.name for f in rendered))
    print(f"\nDone. Outputs in {out}: " + "; ".join(outs) + ".")


if __name__ == "__main__":
    main()
