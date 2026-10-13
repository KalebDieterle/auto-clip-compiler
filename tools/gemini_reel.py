"""Funny-moment compilations judged by Gemini (video + audio), newest clips first. CLI over clipcompiler.job.

A 480p proxy of each clip is uploaded to the Gemini Files API and deleted after judging; judgments
are cached by content hash, so re-runs never re-upload or re-pay. Requires GEMINI_API_KEY (or a key
saved in the app's Settings).

  One reel:  python tools/gemini_reel.py --source <clips> --output <dir> --target-minutes 30
  Library:   python tools/gemini_reel.py --source <clips> --output <dir> --parts-minutes 35 --max-clips 5000 --budget-usd 9
"""
import argparse
import datetime
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcompiler import gemini, job, paths  # noqa: E402
from clipcompiler.reel import discover, pick  # noqa: E402
from clipcompiler.store import HashIndex, atomic_json, awake  # noqa: E402


def printer(event):
    kind = event['type']
    if kind == 'clip':
        if event['status'] in ('judged', 'cached'):
            have = f"; {event['minutes']:.1f} min" if event.get('minutes') is not None else ''
            print(f"  [{event['done']}] {event['name']}: {len(event['scores'])} moments "
                  f"({', '.join(map(str, event['scores'])) or 'none'}){have}; ${event['usd']:.3f}", flush=True)
        else:
            print(f"  {event['name']}: {event['status'].upper()} {event.get('error', '')}", flush=True)
    elif kind == 'stopping':
        print(f"Stopping ({event['reason']}): {event['message']}", flush=True)
    elif kind == 'part_done':
        print(f"Published {event['file']}", flush=True)
    elif kind == 'render_start' and event.get('plan'):
        for row in event['plan']:
            print(f"Part {row['part']:02}: {row['from']} to {row['to']}, {row['moments']} moments, {row['minutes']} min", flush=True)


def default_cache():
    """The cache folder chosen in the app's Settings (shared with the GUI), else the app default."""
    from clipcompiler import settings
    return settings.cache_dir()


def api_key():
    if os.environ.get('GEMINI_API_KEY'): return os.environ['GEMINI_API_KEY']
    from clipcompiler import settings
    return settings.get_key()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--source', required=True); p.add_argument('--output', required=True)
    p.add_argument('--target-minutes', type=float, default=30); p.add_argument('--min-score', type=int, default=7)
    p.add_argument('--workers', type=int, default=4); p.add_argument('--max-clips', type=int, default=400)
    p.add_argument('--since', help='Only clips recorded on/after YYYY-MM-DD[THH:MM:SS]')
    p.add_argument('--until', help='Only clips recorded before YYYY-MM-DD[THH:MM:SS]')
    p.add_argument('--cache-only', action='store_true', help='Never call Gemini; use saved (e.g. batch) judgments only')
    p.add_argument('--cache', help="Judgment cache dir (default: the app's cache folder from Settings)")
    p.add_argument('--exclude', action='append', default=[]); p.add_argument('--model', default=gemini.DEFAULT_MODEL)
    p.add_argument('--no-render', action='store_true')
    p.add_argument('--budget-usd', type=float, help='Stop judging before new judgments cost more than this')
    p.add_argument('--parts-minutes', type=float,
                   help='Judge every clip (budget permitting) and split moments into chronological videos of about this length')
    p.add_argument('--only-parts', type=int, nargs='+', help='With --parts-minutes: render just these part numbers')
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format='    %(message)s')
    target = args.target_minutes*60
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    scratch = out/'.upload-proxies'; scratch.mkdir(exist_ok=True)
    cache = Path(args.cache) if args.cache else default_cache(); cache.mkdir(parents=True, exist_ok=True)
    since = datetime.datetime.fromisoformat(args.since).timestamp() if args.since else None
    until = datetime.datetime.fromisoformat(args.until).timestamp() if args.until else None
    clips = [c for c in discover(args.source, since, until) if c[2].name not in args.exclude][:args.max_clips]
    print(f'{len(clips)} candidate clips, newest first; target {args.target_minutes:g} min at score >= {args.min_score}', flush=True)
    hasher = HashIndex(paths.hash_index_path())
    try:
        result = job.JudgeJob(clips, cache, scratch, api_key=None if args.cache_only else api_key(), model=args.model,
                              workers=args.workers, budget_usd=args.budget_usd, min_score=args.min_score,
                              target_seconds=None if args.parts_minutes else target, cache_only=args.cache_only,
                              hasher=hasher, emit=printer).run()
    finally:
        hasher.save()
    good = job.candidates(result['moments'], args.min_score)
    settings = job.render_settings(hasher=hasher)
    atomic_json(out/'report.json', result['report'])
    if args.parts_minutes:
        if args.no_render:
            from clipcompiler.reel import part_plan, split_parts
            plan = part_plan(split_parts(good, args.parts_minutes*60))
            printer({'type': 'render_start', 'plan': plan})
        else:
            _, plan = job.render_parts(good, out, args.parts_minutes*60, settings, emit=printer,
                                       only=set(args.only_parts or []))
        atomic_json(out/'summary.json', dict(result['usage'], parts=plan))
        return
    chosen, total = pick(good, target)
    borderline = [m for m in chosen if m['score'] == args.min_score]
    summary = dict(result['usage'], moments_found=len(result['moments']), moments_after_dedupe=len(good),
                   chosen=len(chosen), reel_minutes=round(total/60, 2),
                   borderline=[f"{m['clip_name']} {m['start']:.0f}-{m['end']:.0f}s: {m['reason']}" for m in borderline])
    atomic_json(out/'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'borderline'}), flush=True)
    if total < target*.9:
        print(f'NOTE: only {total/60:.1f} min of moments scored >= {args.min_score} in the clips judged.', flush=True)
    if chosen and not args.no_render:
        print(f"Published {job.render_reel(chosen, out, settings, emit=printer)}", flush=True)


if __name__ == '__main__':
    with awake():  # Hold off system sleep only while this job runs.
        main()
