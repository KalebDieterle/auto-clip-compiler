"""Judge many clips through the Gemini Batch API (half price, finishes within ~24 h on Google's side).

  submit   make 480p proxies, upload them, upload a JSONL of requests, create the batch job
  status   show the job's state
  collect  download results into the shared judgment cache, then delete every upload

Only `submit` (proxies + uploads) and `collect` need the PC on; Google judges in between.
State lives in <job-dir>/batch.json so any step can be re-run or resumed. Render afterwards with
gemini_reel.py --cache-only (no live API calls). Requires GEMINI_API_KEY.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcompiler.store import atomic_json, file_hash  # noqa: E402
from gemini_reel import PROMPT, PROMPT_VERSION, cost, discover, proxy  # noqa: E402
from gemini_test import SCHEMA  # noqa: E402

BATCH_DISCOUNT = .5
DONE = {'JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'}


def rest_schema(schema):
    """The Batch JSONL is raw REST, so schema types use the API's upper-case enum names."""
    if isinstance(schema, dict):
        return {k: (v.upper() if k == 'type' else rest_schema(v)) for k, v in schema.items()}
    if isinstance(schema, list):
        return [rest_schema(v) for v in schema]
    return schema


def request_line(key, file_uri):
    return {'key': key, 'request': {
        'contents': [{'role': 'user', 'parts': [{'fileData': {'fileUri': file_uri, 'mimeType': 'video/mp4'}},
                                                {'text': PROMPT}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': rest_schema(SCHEMA),
                             'temperature': 0}}}


def client():
    from google import genai
    return genai.Client(api_key=os.environ['GEMINI_API_KEY'])


def load(job_dir):
    path = Path(job_dir)/'batch.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save(job_dir, state):
    atomic_json(Path(job_dir)/'batch.json', state)


def submit(args):
    job_dir = Path(args.job_dir); job_dir.mkdir(parents=True, exist_ok=True)
    state = load(job_dir)
    if state and state.get('job'):
        sys.exit(f"Batch already submitted ({state['job']}); use status/collect, or a new --job-dir.")
    cache = Path(args.cache); cache.mkdir(parents=True, exist_ok=True)
    since = datetime.datetime.fromisoformat(args.since).timestamp() if args.since else None
    until = datetime.datetime.fromisoformat(args.until).timestamp() if args.until else None
    clips = discover(args.source, since, until)[:args.max_clips]
    state = state or {'created': datetime.datetime.now().isoformat(timespec='seconds'), 'model': args.model,
                      'prompt': PROMPT_VERSION, 'clips': {}}
    lock = threading.Lock(); scratch = job_dir/'.proxies'; scratch.mkdir(exist_ok=True)
    gc = client()
    print(f'{len(clips)} clips in range; hashing and skipping already-judged ones...', flush=True)

    def prepare(clip):
        _, _, path = clip
        try:
            digest = file_hash(path)
            if (cache/f'{digest}.json').exists() or digest in state['clips']:
                return 'skip'
            small = proxy(path, scratch)
            try:
                for attempt in range(4):
                    try:
                        uploaded = gc.files.upload(file=str(small), config={'mime_type': 'video/mp4'}); break
                    except Exception as exc:
                        if attempt == 3: raise
                        time.sleep(10*(attempt+1))
            finally:
                small.unlink(missing_ok=True)
            with lock:
                state['clips'][digest] = {'path': str(path), 'file': uploaded.name, 'uri': uploaded.uri}
                if len(state['clips']) % 25 == 0:
                    save(job_dir, state); print(f"  uploaded {len(state['clips'])}", flush=True)
            return 'uploaded'
        except Exception as exc:
            print(f'  {path.name}: FAILED {exc}', flush=True); return 'failed'

    results = list(ThreadPoolExecutor(max_workers=args.workers).map(prepare, clips))
    save(job_dir, state)
    print(f"uploaded {results.count('uploaded')}, already judged {results.count('skip')}, failed {results.count('failed')}", flush=True)
    if not state['clips']:
        print('Nothing to judge.'); return
    # Uploads expire after 48 h, so wait for every proxy to finish server-side processing now.
    pending = [c for c in state['clips'].values()]
    for c in pending:
        for _ in range(120):
            f = gc.files.get(name=c['file'])
            if f.state.name != 'PROCESSING': break
            time.sleep(2)
        if f.state.name != 'ACTIVE':
            raise SystemExit(f"Upload {c['file']} ended {f.state.name}; re-run submit after fixing.")
    lines = job_dir/'requests.jsonl'
    with lines.open('w', encoding='utf-8') as out:
        for digest, c in state['clips'].items():
            out.write(json.dumps(request_line(digest, c['uri']))+'\n')
    src = gc.files.upload(file=str(lines), config={'mime_type': 'jsonl', 'display_name': f'clip-batch-{job_dir.name}'})
    job = gc.batches.create(model=args.model, src=src.name, config={'display_name': f'clip-batch-{job_dir.name}'})
    state.update(job=job.name, requests_file=src.name, submitted=datetime.datetime.now().isoformat(timespec='seconds'))
    save(job_dir, state)
    est = len(state['clips'])*cost({'input_tokens': 2850, 'output_tokens': 200, 'thinking_tokens': 450})*BATCH_DISCOUNT
    print(f"Submitted {job.name}: {len(state['clips'])} clips, estimated ${est:.2f}. The PC can be turned off now.", flush=True)


def status(args):
    state = load(args.job_dir)
    if not state or not state.get('job'): sys.exit('No submitted batch in this job dir.')
    gc = client()  # Keep a reference: a garbage-collected Client closes its HTTP connection mid-call.
    job = gc.batches.get(name=state['job'])
    print(f"{state['job']}: {job.state.name} ({len(state['clips'])} clips, submitted {state['submitted']})")
    return job


def collect(args):
    state = load(args.job_dir)
    job = status(args)
    if job.state.name not in DONE:
        sys.exit('Not finished yet; run collect again later.')
    gc = client(); cache = Path(args.cache); cache.mkdir(parents=True, exist_ok=True)
    judged, failed, usd = 0, [], 0.
    if job.state.name == 'JOB_STATE_SUCCEEDED' and job.dest and job.dest.file_name:
        raw = gc.files.download(file=job.dest.file_name).decode('utf-8')
        for line in filter(None, raw.splitlines()):
            row = json.loads(line); digest = row.get('key')
            try:
                if 'error' in row or 'response' not in row: raise ValueError(row.get('error', 'no response'))
                response = row['response']
                text = ''.join(p.get('text', '') for p in response['candidates'][0]['content']['parts']
                               if not p.get('thought'))
                result = json.loads(text)
                usage = response.get('usageMetadata', {})
                result['_meta'] = {'model': state['model'], 'prompt': state['prompt'], 'batch': True,
                                   'input_tokens': usage.get('promptTokenCount'),
                                   'output_tokens': usage.get('candidatesTokenCount'),
                                   'thinking_tokens': usage.get('thoughtsTokenCount')}
                usd += cost(result['_meta'])*BATCH_DISCOUNT
                atomic_json(cache/f'{digest}.json', result); judged += 1
            except Exception as exc:
                failed.append({'clip': Path(state['clips'].get(digest, {}).get('path', digest)).name, 'error': str(exc)[:300]})
    else:
        failed.append({'job': job.state.name})
    # Clean up every upload regardless of outcome; failed clips simply stay un-cached for a later run.
    for c in list(state['clips'].values()) + [{'file': state.get('requests_file')}]:
        if c.get('file'):
            try: gc.files.delete(name=c['file'])
            except Exception: pass
    state.update(collected=datetime.datetime.now().isoformat(timespec='seconds'), judged=judged,
                 failed=failed, usd=round(usd, 4))
    save(args.job_dir, state)
    print(f'Collected {judged} judgments (${usd:.2f} at batch price), {len(failed)} failed; uploads deleted.')
    for f in failed[:10]: print('  FAILED', f)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('submit', 'status', 'collect'):
        s = sub.add_parser(name); s.add_argument('--job-dir', required=True)
        s.add_argument('--cache', default=r'C:\Users\<user>\Videos\Highlights\gemini-cache')
        if name == 'submit':
            s.add_argument('--source', required=True); s.add_argument('--since'); s.add_argument('--until')
            s.add_argument('--max-clips', type=int, default=5000); s.add_argument('--workers', type=int, default=6)
            s.add_argument('--model', default='gemini-flash-latest')
    args = p.parse_args()
    {'submit': submit, 'status': status, 'collect': collect}[args.command](args)


if __name__ == '__main__':
    from clipcompiler.store import awake
    with awake():
        main()
