"""Re-render Combined_highlights.mp4 from an existing manifest.csv, reusing the exact
clip selection already chosen by a prior autoclip run. Calls autoclip.render_videos so
all render logic (incl. --timestamp-overlay) is shared, never duplicated.

Usage:
  python render_from_manifest.py --manifest "<dir>\manifest.csv" --output "<dir>" [--timestamp-overlay]
"""
import argparse
import csv
from pathlib import Path
from types import SimpleNamespace

import autoclip


def parse_ts(s: str) -> float:
    """'HH:MM:SS.mmm' -> seconds."""
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--timestamp-overlay", action="store_true")
    p.add_argument("--render-height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--combine", action="store_true", default=True)
    a = p.parse_args()

    rows = list(csv.DictReader(Path(a.manifest).open(encoding="utf-8")))
    moments = []
    for r in rows:
        src = Path(r["source_path"])
        clip = autoclip.ClipInfo(path=src, game=r.get("game") or "OTHER", copied_path=None)
        moments.append(autoclip.Moment(
            clip=clip, laugh_start=0.0, laugh_end=0.0,
            funny_score=float(r.get("funny_score") or 0.0),
            in_t=parse_ts(r["in_timestamp"]), out_t=parse_ts(r["out_timestamp"]),
            transcript=r.get("transcript") or "",
            llm_score=(float(r["llm_score"]) if r.get("llm_score") else None),
        ))

    args = SimpleNamespace(
        output=a.output, render_height=a.render_height, fps=a.fps,
        workers=a.workers, ffmpeg=a.ffmpeg, combine=True,
        timestamp_overlay=a.timestamp_overlay,
    )
    print(f"Rendering {len(moments)} clips from {a.manifest} (overlay={a.timestamp_overlay})")
    out = autoclip.render_videos(moments, args)
    print("Wrote:", [str(x) for x in out])


if __name__ == "__main__":
    main()
