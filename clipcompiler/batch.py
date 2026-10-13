"""Gemini Batch API judging: half price, results within ~24 h (often well under an hour).

  submit   480p proxies are uploaded, a JSONL of requests is uploaded, a batch job is created
  refresh  ask Google for the job's state
  collect  download results into the shared judgment cache (same shape as live), delete every upload
  cancel   cancel the job and delete every upload

Only submit (uploads) and collect need the PC on. State lives in <job_dir>/batch.json and every step
is resumable: an interrupted submit reuses uploads already made (they live 48 h), and collect is
idempotent. Judgments land in the same content-addressed cache as live judging, so a clip is never
paid for twice whichever way it was judged.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import logging
from pathlib import Path
import threading

from .gemini import BATCH_DISCOUNT, PROMPT, PROMPT_VERSION, SCHEMA, TYPICAL_TOKENS, cost, proxy
from .media import Cancelled
from .store import atomic_json, file_hash

log = logging.getLogger(__name__)
DONE = {'JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'}
EXPIRES_HOURS = 48
PER_CLIP = cost(TYPICAL_TOKENS) * BATCH_DISCOUNT


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


def load(job_dir):
    path = Path(job_dir)/'batch.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save(job_dir, state):
    Path(job_dir).mkdir(parents=True, exist_ok=True)
    atomic_json(Path(job_dir)/'batch.json', state)


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def submit(clips, job_dir, cache, gc, *, model, hasher=file_hash, ffmpeg='ffmpeg', workers=6, max_usd=None,
           emit=None, cancel=None):
    """Upload proxies for every un-judged clip (newest first, within max_usd) and create the batch job.

    Returns the saved state; state['job'] is None when there was nothing to judge."""
    emit = emit or (lambda e: None); cancel = cancel or threading.Event()
    job_dir = Path(job_dir); cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    state = load(job_dir) or {'created': now(), 'model': model, 'prompt': PROMPT_VERSION, 'clips': {}, 'failed': []}
    if state.get('job'):
        raise RuntimeError(f"Batch already submitted ({state['job']}).")
    todo = []
    for _, _, path in clips:
        digest = hasher(path)
        if not (cache/f'{digest}.json').exists() and digest not in state['clips'] and all(d != digest for d, _ in todo):
            todo.append((digest, path))
    capped = 0
    if max_usd:
        room = max(0, int(max_usd / PER_CLIP) - len(state['clips']))
        capped, todo = max(0, len(todo)-room), todo[:room]
    total = len(todo) + len(state['clips'])
    emit({'type': 'batch_upload', 'done': len(state['clips']), 'total': total, 'capped': capped})
    lock = threading.Lock(); scratch = job_dir/'.proxies'; scratch.mkdir(parents=True, exist_ok=True)

    def prepare(item):
        digest, path = item
        if cancel.is_set(): return
        try:
            small = proxy(path, scratch, ffmpeg, cancel)
            try:
                for attempt in range(4):
                    if cancel.is_set(): raise Cancelled()
                    try:
                        uploaded = gc.files.upload(file=str(small), config={'mime_type': 'video/mp4'}); break
                    except Exception as exc:
                        if attempt == 3: raise
                        log.warning('%s: upload error (%s); retrying', path.name, exc); cancel.wait(10*(attempt+1))
            finally:
                small.unlink(missing_ok=True)
            with lock:
                state['clips'][digest] = {'path': str(path), 'file': uploaded.name, 'uri': uploaded.uri}
                done = len(state['clips'])
                if done % 10 == 0: save(job_dir, state)
            emit({'type': 'batch_upload', 'done': done, 'total': total})
        except Cancelled:
            pass
        except Exception as exc:
            log.warning('%s: upload FAILED %s', path.name, exc)
            with lock: state['failed'].append({'clip': path.name, 'error': str(exc)[:300]})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(prepare, todo))
    save(job_dir, state)
    if cancel.is_set():
        delete_uploads(state, gc); state['status'] = 'cancelled'; save(job_dir, state)
        raise Cancelled()
    if not state['clips']:
        state.update(job=None, status='nothing'); save(job_dir, state); return state
    # Uploads expire after 48 h: wait for each to finish server-side processing; drop any that broke.
    for digest, c in list(state['clips'].items()):
        f = gc.files.get(name=c['file'])
        for _ in range(150):
            if f.state.name != 'PROCESSING': break
            if cancel.wait(2): break
            f = gc.files.get(name=c['file'])
        if f.state.name != 'ACTIVE':
            state['failed'].append({'clip': Path(c['path']).name, 'error': f'upload ended {f.state.name}'})
            try: gc.files.delete(name=c['file'])
            except Exception: pass
            del state['clips'][digest]
    if cancel.is_set():
        delete_uploads(state, gc); state['status'] = 'cancelled'; save(job_dir, state)
        raise Cancelled()
    lines = job_dir/'requests.jsonl'
    with lines.open('w', encoding='utf-8') as out:
        for digest, c in state['clips'].items():
            out.write(json.dumps(request_line(digest, c['uri']))+'\n')
    src = gc.files.upload(file=str(lines), config={'mime_type': 'jsonl', 'display_name': f'clip-batch-{job_dir.name}'})
    state['requests_file'] = src.name; save(job_dir, state)
    job = gc.batches.create(model=state['model'], src=src.name, config={'display_name': f'clip-batch-{job_dir.name}'})
    state.update(job=job.name, submitted=now(), status='JOB_STATE_PENDING',
                 estimate_usd=round(len(state['clips'])*PER_CLIP, 4))
    save(job_dir, state)
    emit({'type': 'batch_submitted', 'job': job.name, 'clips': len(state['clips']), 'estimate_usd': state['estimate_usd'],
          'failed': len(state['failed']), 'capped': capped})
    return state


def refresh(state, job_dir, gc):
    """Ask Google for the job's state; remembers it in batch.json. Returns the state name."""
    job = gc.batches.get(name=state['job'])
    state.update(status=job.state.name, checked=now()); save(job_dir, state)
    return job


def collect(state, job_dir, gc, cache, job=None):
    """Write finished results into the cache and delete every upload. Safe to call twice."""
    if state.get('collected'): return state
    job = job or refresh(state, job_dir, gc)
    if job.state.name not in DONE: raise RuntimeError('The batch has not finished yet.')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    judged, failed, usd = 0, [], 0.
    names = {d: Path(c['path']).name for d, c in state['clips'].items()}
    dest = getattr(job, 'dest', None)
    if job.state.name == 'JOB_STATE_SUCCEEDED' and dest and getattr(dest, 'file_name', None):
        raw = gc.files.download(file=dest.file_name)
        for line in filter(None, (raw.decode('utf-8') if isinstance(raw, bytes) else raw).splitlines()):
            row = json.loads(line); digest = row.get('key')
            try:
                if 'error' in row or 'response' not in row: raise ValueError(row.get('error', 'no response'))
                response = row['response']
                text = ''.join(p.get('text', '') for p in response['candidates'][0]['content']['parts'] if not p.get('thought'))
                result = json.loads(text)
                usage = response.get('usageMetadata', {})
                result['_meta'] = {'model': state['model'], 'prompt': state['prompt'], 'batch': True,
                                   'input_tokens': usage.get('promptTokenCount'),
                                   'output_tokens': usage.get('candidatesTokenCount'),
                                   'thinking_tokens': usage.get('thoughtsTokenCount')}
                usd += cost(result['_meta'])*BATCH_DISCOUNT
                atomic_json(cache/f'{digest}.json', result); judged += 1
            except Exception as exc:
                failed.append({'clip': names.get(digest, digest), 'error': str(exc)[:300]})
    else:
        failed.append({'job': job.state.name})
    delete_uploads(state, gc)  # Failed clips simply stay un-judged for a later run.
    state.update(collected=now(), judged=judged, failed=state.get('failed', []) + failed, usd=round(usd, 4),
                 status=job.state.name)
    save(job_dir, state)
    return state


def cancel(state, job_dir, gc):
    if state.get('job') and state.get('status') not in DONE:
        try: gc.batches.cancel(name=state['job'])
        except Exception as exc: log.warning('batch cancel: %s', exc)
    delete_uploads(state, gc)
    state.update(status='JOB_STATE_CANCELLED', collected=state.get('collected') or now())
    save(job_dir, state)
    return state


def delete_uploads(state, gc):
    """Never leave clips on Google's side, whatever happened."""
    for c in list(state['clips'].values()) + [{'file': state.get('requests_file')}]:
        if c.get('file'):
            try: gc.files.delete(name=c['file'])
            except Exception: pass


def expires_at(state):
    if not state.get('submitted'): return None
    return (datetime.datetime.fromisoformat(state['submitted']) + datetime.timedelta(hours=EXPIRES_HOURS)).isoformat(timespec='seconds')
