"""Gemini Batch API judging: half price, results within ~24 h (often well under an hour).

  submit          480p proxies are uploaded, a JSONL of requests is uploaded, a batch job is created.
                  One upload per clip serves every question asked of it (funny and/or skill triage).
  submit_details  skill stage 2: a close-up window per candidate play (found by triage) is uploaded and
                  judged at a high frame rate, as a second batch
  refresh         ask Google for the job's state
  collect         download results into the shared judgment cache (same shape as live), delete every upload
  cancel          cancel the job and delete every upload

Only submit (uploads) and collect need the PC on. State lives in <job_dir>/batch.json and every step
is resumable: an interrupted submit reuses uploads already made (they live 48 h), and collect is
idempotent. Judgments land in the same content-addressed cache as live judging, so a clip is never
paid for twice whichever way it was judged.

Request keys: '<digest>' = funny, '<digest>:triage' = skill stage 1, '<digest>:detail:<play id>' = stage 2.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import logging
from pathlib import Path
import threading

from . import skill
from .gemini import BATCH_DISCOUNT, PROMPT, PROMPT_VERSION, SAFETY, SCHEMA, TYPICAL_TOKENS, cost, proxy
from .media import Cancelled, probe, window
from .store import atomic_json, file_hash

log = logging.getLogger(__name__)
DONE = {'JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'}
EXPIRES_HOURS = 48
PER_CLIP = cost(TYPICAL_TOKENS) * BATCH_DISCOUNT
PER_ASK = {'funny': PER_CLIP, 'triage': cost(skill.TYPICAL_TRIAGE) * BATCH_DISCOUNT,
           'detail': cost(skill.TYPICAL_DETAIL) * BATCH_DISCOUNT}


def rest_schema(schema):
    """The Batch JSONL is raw REST, so schema types use the API's upper-case enum names."""
    if isinstance(schema, dict):
        return {k: (v.upper() if k == 'type' else rest_schema(v)) for k, v in schema.items()}
    if isinstance(schema, list):
        return [rest_schema(v) for v in schema]
    return schema


def request_line(key, file_uri, prompt=PROMPT, schema=SCHEMA, fps=None):
    video = {'fileData': {'fileUri': file_uri, 'mimeType': 'video/mp4'}}
    if fps: video['videoMetadata'] = {'fps': fps}
    return {'key': key, 'request': {
        'contents': [{'role': 'user', 'parts': [video, {'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': rest_schema(schema),
                             'temperature': 0},
        'safetySettings': SAFETY}}


def ask_lines(key, entry, profiles=None):
    """The request lines for one upload, from the questions recorded with it."""
    digest = key.split(':')[0]; lines = []
    for ask in entry.get('asks', ['funny']):   # States from v1 have no 'asks': funny only.
        if ask == 'funny':
            lines.append(request_line(digest, entry['uri']))
        elif ask == 'triage':
            lines.append(request_line(f'{digest}:triage', entry['uri'], skill.TRIAGE_PROMPT, skill.TRIAGE_SCHEMA))
        else:
            profile = (profiles or skill.Profiles()).for_game(entry['game'])
            lines.append(request_line(f"{digest}:detail:{entry['play_id']}", entry['uri'], skill.detail_prompt(profile),
                                      skill.DETAIL_SCHEMA, skill.WINDOW_FPS))
    return lines


def load(job_dir):
    path = Path(job_dir)/'batch.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save(job_dir, state):
    Path(job_dir).mkdir(parents=True, exist_ok=True)
    atomic_json(Path(job_dir)/'batch.json', state)


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def new_state(model, stage=1):
    return {'created': now(), 'model': model, 'prompt': PROMPT_VERSION, 'stage': stage, 'clips': {}, 'failed': []}


def submit(clips, job_dir, cache, gc, *, model, hasher=file_hash, ffmpeg='ffmpeg', workers=6, max_usd=None,
           emit=None, cancel=None, kinds=('funny',)):
    """Upload proxies for every clip still missing a judgment of `kinds` (newest first, within max_usd) and
    create the batch job. kinds: 'funny' and/or 'triage'; one upload serves both.

    Returns the saved state; state['job'] is None when there was nothing to judge."""
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    state = load(job_dir) or new_state(model)
    if state.get('job'):
        raise RuntimeError(f"Batch already submitted ({state['job']}).")
    todo, seen = [], set(state['clips'])
    for _, _, path in clips:
        digest = hasher(path)
        if digest in seen: continue
        asks = [k for k in kinds if (k == 'funny' and not (cache/f'{digest}.json').exists())
                or (k == 'triage' and skill.load_record(cache, digest) is None)]
        if asks:
            todo.append((digest, path, {'asks': asks})); seen.add(digest)

    def make(item, scratch, cancel):
        return proxy(item[1], scratch, ffmpeg, cancel)
    return _submit(state, todo, make, job_dir, gc, workers=workers, max_usd=max_usd, emit=emit, cancel=cancel)


def detail_todo(clips, cache, profiles, hasher=file_hash, ffprobe='ffprobe'):
    """(key, path, entry) for every triaged play still owed a close-up."""
    todo = []
    for _, _, path in clips:
        digest = hasher(path); record = skill.load_record(cache, digest)
        if not record: continue
        profile = profiles.for_record(record)
        if not skill.pending_plays(record, profile): continue
        duration = probe(path, ffprobe)['duration']
        for play in skill.pending_plays(record, profile, duration):
            todo.append((f'{digest}:{skill.play_id(play)}', path,
                         {'asks': ['detail'], 'play_id': skill.play_id(play), 'play': play, 'game': record['triage'].get('game'),
                          'window': list(skill.window_bounds(play, duration))}))
    return todo


def submit_details(clips, job_dir, cache, gc, *, model, profiles, hasher=file_hash, ffmpeg='ffmpeg', ffprobe='ffprobe',
                   workers=6, max_usd=None, emit=None, cancel=None):
    """Skill stage 2 as a batch: one close-up window per pending play. Same state shape and guarantees as submit."""
    state = load(job_dir) or new_state(model, stage=2)
    if state.get('job'):
        raise RuntimeError(f"Batch already submitted ({state['job']}).")
    todo = [t for t in detail_todo(clips, cache, profiles, hasher, ffprobe) if t[0] not in state['clips']]

    def make(item, scratch, cancel):
        entry = item[2]; profile = profiles.for_game(entry['game'])
        return window(item[1], entry['window'][0], entry['window'][1], profile['panel'], scratch, ffmpeg, skill.WINDOW_FPS, cancel)
    return _submit(state, todo, make, job_dir, gc, workers=workers, max_usd=max_usd, emit=emit, cancel=cancel,
                   profiles=profiles)


def entry_cost(entry):
    return sum(PER_ASK[a] for a in entry.get('asks', ['funny']))


def _submit(state, todo, make, job_dir, gc, *, workers, max_usd, emit, cancel, profiles=None):
    """Shared by both stages: resumable uploads, budget cap, wait for processing, request file, job."""
    emit = emit or (lambda e: None); cancel = cancel or threading.Event(); job_dir = Path(job_dir)
    capped = 0
    if max_usd:
        room = max_usd - sum(entry_cost(c) for c in state['clips'].values()); kept = []
        for item in todo:
            if entry_cost(item[2]) > room: break
            kept.append(item); room -= entry_cost(item[2])
        capped, todo = len(todo)-len(kept), kept
    total = len(todo) + len(state['clips'])
    emit({'type': 'batch_upload', 'done': len(state['clips']), 'total': total, 'capped': capped, 'stage': state.get('stage', 1)})
    lock = threading.Lock(); scratch = job_dir/'.proxies'; scratch.mkdir(parents=True, exist_ok=True)

    def prepare(item):
        key, path, entry = item
        if cancel.is_set(): return
        try:
            small = make(item, scratch, cancel)
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
                state['clips'][key] = dict(entry, path=str(path), file=uploaded.name, uri=uploaded.uri)
                done = len(state['clips'])
                if done % 10 == 0: save(job_dir, state)
            emit({'type': 'batch_upload', 'done': done, 'total': total, 'stage': state.get('stage', 1)})
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
    for key, c in list(state['clips'].items()):
        f = gc.files.get(name=c['file'])
        for _ in range(150):
            if f.state.name != 'PROCESSING': break
            if cancel.wait(2): break
            f = gc.files.get(name=c['file'])
        if f.state.name != 'ACTIVE':
            state['failed'].append({'clip': Path(c['path']).name, 'error': f'upload ended {f.state.name}'})
            try: gc.files.delete(name=c['file'])
            except Exception: pass
            del state['clips'][key]
    if cancel.is_set():
        delete_uploads(state, gc); state['status'] = 'cancelled'; save(job_dir, state)
        raise Cancelled()
    lines = job_dir/'requests.jsonl'
    with lines.open('w', encoding='utf-8') as out:
        for key, c in state['clips'].items():
            for line in ask_lines(key, c, profiles): out.write(json.dumps(line)+'\n')
    src = gc.files.upload(file=str(lines), config={'mime_type': 'jsonl', 'display_name': f'clip-batch-{job_dir.name}'})
    state['requests_file'] = src.name; save(job_dir, state)
    job = gc.batches.create(model=state['model'], src=src.name, config={'display_name': f'clip-batch-{job_dir.name}'})
    state.update(job=job.name, submitted=now(), status='JOB_STATE_PENDING',
                 estimate_usd=round(sum(entry_cost(c) for c in state['clips'].values()), 4))
    save(job_dir, state)
    emit({'type': 'batch_submitted', 'job': job.name, 'clips': len(state['clips']), 'estimate_usd': state['estimate_usd'],
          'failed': len(state['failed']), 'capped': capped, 'stage': state.get('stage', 1)})
    return state


def refresh(state, job_dir, gc):
    """Ask Google for the job's state; remembers it in batch.json. Returns the state name."""
    job = gc.batches.get(name=state['job'])
    state.update(status=job.state.name, checked=now()); save(job_dir, state)
    return job


def store_result(key, result, state, cache):
    """Write one batch result where live judging would have put it."""
    parts = key.split(':'); digest = parts[0]
    if len(parts) == 1:
        atomic_json(cache/f'{digest}.json', result)
    elif parts[1] == 'triage':
        if skill.load_record(cache, digest) is None:   # Never overwrite a record that already has close-ups.
            skill.save_record(cache, digest, skill.new_record(result))
    else:
        entry = state['clips'][f'{digest}:{parts[2]}']
        record = skill.load_record(cache, digest)
        if record is None: raise ValueError('close-up without a triage record')
        meta = result.pop('_meta', None)
        out = skill.to_clip_time(result, entry['window'], entry['play']); out['window'] = entry['window']
        if meta: out['_meta'] = meta
        record['plays'][parts[2]] = out
        skill.save_record(cache, digest, record)


VERSIONS = {'triage': skill.TRIAGE_VERSION, 'detail': skill.DETAIL_VERSION}


def collect(state, job_dir, gc, cache, job=None):
    """Write finished results into the cache and delete every upload. Safe to call twice."""
    if state.get('collected'): return state
    job = job or refresh(state, job_dir, gc)
    if job.state.name not in DONE: raise RuntimeError('The batch has not finished yet.')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    judged, failed, usd = 0, [], 0.
    names = {k.split(':')[0]: Path(c['path']).name for k, c in state['clips'].items()}
    dest = getattr(job, 'dest', None)
    if job.state.name == 'JOB_STATE_SUCCEEDED' and dest and getattr(dest, 'file_name', None):
        raw = gc.files.download(file=dest.file_name)
        for line in filter(None, (raw.decode('utf-8') if isinstance(raw, bytes) else raw).splitlines()):
            row = json.loads(line); key = row.get('key') or ''
            try:
                if 'error' in row or 'response' not in row: raise ValueError(row.get('error', 'no response'))
                response = row['response']
                text = ''.join(p.get('text', '') for p in response['candidates'][0]['content']['parts'] if not p.get('thought'))
                result = json.loads(text)
                usage = response.get('usageMetadata', {})
                kind = key.split(':')[1] if ':' in key else 'funny'
                result['_meta'] = {'model': state['model'], 'prompt': VERSIONS.get(kind, state['prompt']), 'batch': True,
                                   'input_tokens': usage.get('promptTokenCount'),
                                   'output_tokens': usage.get('candidatesTokenCount'),
                                   'thinking_tokens': usage.get('thoughtsTokenCount')}
                usd += cost(result['_meta'])*BATCH_DISCOUNT
                store_result(key, result, state, cache); judged += 1
            except Exception as exc:
                failed.append({'clip': names.get(key.split(':')[0], key), 'error': str(exc)[:300]})
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
