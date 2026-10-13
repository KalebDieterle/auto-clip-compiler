"""Judge many clips through the Gemini Batch API (half price, finishes within ~24 h). CLI over clipcompiler.batch.

  submit   make 480p proxies, upload them, upload a JSONL of requests, create the batch job
  status   show the job's state
  collect  download results into the shared judgment cache, then delete every upload
  cancel   cancel the job and delete every upload

Only `submit` (proxies + uploads) and `collect` need the PC on; Google judges in between.
State lives in <job-dir>/batch.json so any step can be re-run or resumed. Render afterwards with
gemini_reel.py --cache-only (no live API calls). Requires GEMINI_API_KEY or a key saved in the app.
"""
import argparse
import datetime
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcompiler import batch, gemini, paths  # noqa: E402
from clipcompiler.reel import discover  # noqa: E402
from clipcompiler.store import HashIndex  # noqa: E402
from gemini_reel import api_key, default_cache  # noqa: E402


def printer(event):
    if event['type'] == 'batch_upload' and (event['done'] % 25 == 0 or event['done'] == event['total']):
        print(f"  uploaded {event['done']} of {event['total']}", flush=True)
        if event.get('capped'): print(f"  {event['capped']} clips left out to stay within --budget-usd", flush=True)
    elif event['type'] == 'batch_submitted':
        print(f"Submitted {event['job']}: {event['clips']} clips, estimated ${event['estimate_usd']:.2f}. "
              f"The PC can be turned off now.", flush=True)


def state_or_exit(job_dir):
    state = batch.load(job_dir)
    if not state or not state.get('job'): sys.exit('No submitted batch in this job dir.')
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('submit', 'status', 'collect', 'cancel'):
        s = sub.add_parser(name); s.add_argument('--job-dir', required=True)
        s.add_argument('--cache', default=None, help="Judgment cache dir (default: the app's cache folder from Settings)")
        if name == 'submit':
            s.add_argument('--source', required=True); s.add_argument('--since'); s.add_argument('--until')
            s.add_argument('--max-clips', type=int, default=5000); s.add_argument('--workers', type=int, default=6)
            s.add_argument('--budget-usd', type=float, help='Submit only as many (newest) clips as fit this estimate')
            s.add_argument('--model', default=gemini.DEFAULT_MODEL)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format='    %(message)s')
    cache = Path(args.cache or default_cache())
    gc = gemini.client(api_key())  # Keep a reference: a garbage-collected Client closes its connection mid-call.
    if args.command == 'submit':
        since = datetime.datetime.fromisoformat(args.since).timestamp() if args.since else None
        until = datetime.datetime.fromisoformat(args.until).timestamp() if args.until else None
        clips = discover(args.source, since, until)[:args.max_clips]
        print(f'{len(clips)} clips in range; hashing and skipping already-judged ones...', flush=True)
        hasher = HashIndex(paths.hash_index_path())
        try:
            state = batch.submit(clips, args.job_dir, cache, gc, model=args.model, hasher=hasher, workers=args.workers,
                                 max_usd=args.budget_usd, emit=printer)
        finally:
            hasher.save()
        if not state.get('job'): print('Nothing to judge.')
        for f in state['failed'][:10]: print('  FAILED', f)
        return
    state = state_or_exit(args.job_dir)
    if args.command == 'status':
        job = batch.refresh(state, args.job_dir, gc)
        print(f"{state['job']}: {job.state.name} ({len(state['clips'])} clips, submitted {state['submitted']}, "
              f"expires {batch.expires_at(state)})")
    elif args.command == 'collect':
        job = batch.refresh(state, args.job_dir, gc)
        if job.state.name not in batch.DONE: sys.exit(f'{job.state.name}: not finished yet; run collect again later.')
        state = batch.collect(state, args.job_dir, gc, cache, job)
        print(f"Collected {state['judged']} judgments (${state['usd']:.2f} at batch price), "
              f"{len(state['failed'])} failed; uploads deleted.")
        for f in state['failed'][:10]: print('  FAILED', f)
    else:
        batch.cancel(state, args.job_dir, gc); print('Cancelled; uploads deleted.')


if __name__ == '__main__':
    from clipcompiler.store import awake
    with awake():
        main()
