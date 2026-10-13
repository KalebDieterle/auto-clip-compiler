"""The bridge the UI calls (window.pywebview.api.*). Plain Python, so it is tested without a window.

pywebview exposes every public attribute to JavaScript, so all state lives in `_private` fields.
Each public method returns JSON-safe data; failures come back as {'error': message} rather than
raising into the page. Long work (scan, judge, render) runs on one background thread at a time and
reports through the event queue the page polls with events(since).
"""
from dataclasses import asdict
import datetime
import logging
import os
from pathlib import Path
import threading
import time
import webbrowser

from .. import APP_NAME, __version__, batch, gemini, job, paths, settings as config
from ..media import Cancelled, command, find_ffmpeg
from ..reel import discover, split_parts
from ..store import HashIndex, atomic_json, awake
import json

log = logging.getLogger(__name__)
PREVIEW_PAD = 3.         # Seconds of context either side of a moment, so trims can grow it.
MIN_MOMENT = 1.
ALLOWED_URLS = ('https://aistudio.google.com/', 'https://ai.google.dev/')
BELOW_NORMAL, NORMAL = 0x4000, 0x20


def safe(method):
    """Never let an exception escape into the page: log it and return {'error': ...}."""
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            log.exception('%s failed', method.__name__)
            return {'error': config.redact(str(exc)) or type(exc).__name__}
    wrapper.__name__ = method.__name__; wrapper.__doc__ = method.__doc__
    return wrapper


def low_priority(on):
    """Background jobs yield to games: child ffmpeg processes inherit the priority class."""
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), BELOW_NORMAL if on else NORMAL)


class Api:
    def __init__(self, server=None, judge_fn=None, client_factory=None):
        self._settings = config.load()
        self._server = server; self._window = None; self._judge_fn = judge_fn
        self._client_factory = client_factory or gemini.client
        self._lock = threading.RLock(); self._events = []; self._seq = 0
        self._job = None; self._cancel = None; self._scan = None
        self._run_dir = None; self._run = None
        self._hasher = HashIndex(paths.hash_index_path())
        self._ffmpeg = find_ffmpeg()
        self._media_gate = threading.Semaphore(3)

    # --- events ----------------------------------------------------------------------------------

    def _emit(self, event):
        with self._lock:
            self._seq += 1; self._events.append(dict(event, seq=self._seq, at=time.time()))
            del self._events[:-3000]

    def events(self, since=0):
        with self._lock:
            return {'events': [e for e in self._events if e['seq'] > since], 'busy': self._job}

    # --- state & settings ------------------------------------------------------------------------

    @safe
    def get_state(self):
        key = config.get_key()
        return {'app': APP_NAME, 'version': __version__, 'settings': asdict(self._settings),
                'key': {'present': bool(key), 'masked': config.masked(key)}, 'ffmpeg': bool(self._ffmpeg),
                'busy': self._job, 'run': self._run_summary(), 'recent': self._recent_runs(),
                'cache': {'folder': str(config.cache_dir(self._settings))}}

    @safe
    def save_settings(self, changes):
        merged = config.Settings(**{**asdict(self._settings), **{k: v for k, v in changes.items()
                                                                  if k in asdict(self._settings) and k != 'version'}})
        config.save(merged); self._settings = merged
        return {'settings': asdict(merged)}

    @safe
    def pick_folder(self, initial=''):
        import webview
        if not self._window: return {'error': 'No window'}
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=initial or '')
        return {'path': chosen[0] if chosen else None}

    @safe
    def set_key(self, key, check=True):
        key = (key or '').strip()
        if check:
            result = config.validate_key(key, self._settings.model)
            if result['state'] == 'invalid': return result
            if not result['ok']: return dict(result, saved=False)
        config.set_key(key)
        return {'ok': True, 'state': 'ok', 'saved': True, 'masked': config.masked(key)}

    @safe
    def test_key(self):
        key = config.get_key()
        return config.validate_key(key, self._settings.model) if key else {'ok': False, 'state': 'missing',
                                                                           'message': 'No key saved yet.'}

    @safe
    def clear_key(self):
        config.clear_key(); return {'ok': True}

    @safe
    def open_url(self, url):
        if not url.startswith(ALLOWED_URLS): return {'error': 'Link not allowed'}
        webbrowser.open(url); return {'ok': True}

    @safe
    def open_path(self, path):
        """Open a file or folder the app produced (output folder, run folder, logs)."""
        target = Path(path).resolve()
        roots = [Path(self._settings.output).resolve()] if self._settings.output else []
        roots.append(paths.logs_dir().resolve())
        if not any(target == r or r in target.parents for r in roots) or not target.exists():
            return {'error': 'That location is not one the app created.'}
        os.startfile(target); return {'ok': True}

    @safe
    def storage(self):
        def size(folder):
            return sum(p.stat().st_size for p in Path(folder).rglob('*') if p.is_file()) if Path(folder).exists() else 0
        cache = config.cache_dir(self._settings)
        return {'cache_files': len(list(cache.glob('*.json'))) if cache.exists() else 0,
                'previews_mb': round(size(paths.previews_dir())/1e6, 1), 'logs': str(paths.logs_dir())}

    @safe
    def clear_previews(self):
        if self._job: return {'error': 'Wait for the current job to finish.'}
        for p in paths.previews_dir().glob('*'):
            if p.is_file(): p.unlink(missing_ok=True)
        return {'ok': True}

    # --- background jobs ---------------------------------------------------------------------------

    def _start(self, kind, target):
        with self._lock:
            if self._job: raise RuntimeError(f'Already {self._job}; cancel it first.')
            if not self._ffmpeg: raise RuntimeError('FFmpeg is missing. Reinstall the app.')
            self._job = kind; self._cancel = threading.Event()

        def body():
            low_priority(True)
            try:
                with awake(): target(self._cancel)
            except Cancelled:
                self._emit({'type': f'{kind}_cancelled'})
            except Exception as exc:
                log.exception('%s failed', kind)
                self._emit({'type': 'error', 'job': kind, 'message': config.redact(str(exc))[:500]})
            finally:
                low_priority(False); self._hasher.save()
                with self._lock: self._job = None
                self._emit({'type': 'idle'})
        threading.Thread(target=body, daemon=True, name=kind).start()
        return {'ok': True, 'job': kind}

    @safe
    def cancel(self):
        if self._cancel: self._cancel.set()
        return {'ok': True}

    @safe
    def scan(self, source=None, since=None, until=None):
        """Find clips in the source folder (optionally a date range) and estimate the cost."""
        source = Path(source or self._settings.source)
        if not source.is_dir(): return {'error': 'Pick the folder where your clips are saved.'}
        lo = datetime.datetime.fromisoformat(since).timestamp() if since else None
        # An end date includes that whole day.
        hi = (datetime.datetime.fromisoformat(until) + datetime.timedelta(days=1)).timestamp() if until else None

        def work(cancel):
            clips = discover(source, lo, hi)
            self._emit({'type': 'scan', 'done': 0, 'total': len(clips)})
            rows = job.scan(clips, config.cache_dir(self._settings), self._hasher, emit=self._emit, cancel=cancel)
            pending = self._pending_batch_digests()
            for r in rows: r['pending'] = not r['cached'] and r['digest'] in pending
            todo = [r for r in rows if not r['cached'] and not r['pending']]
            self._scan = {'source': str(source), 'since': since, 'until': until, 'rows': rows}
            self._emit({'type': 'scan_done', 'clips': len(rows), 'cached': sum(r['cached'] for r in rows), 'to_judge': len(todo),
                        'in_batch': sum(r['pending'] for r in rows),
                        'unreadable': sum(r['digest'] is None for r in rows),
                        'gb': round(sum(r['size'] for r in rows)/1e9, 1),
                        'first': rows[-1]['when'] if rows else None, 'last': rows[0]['when'] if rows else None,
                        'estimate_usd': round(gemini.estimate(len(todo)), 2),
                        'estimate_batch_usd': round(gemini.estimate(len(todo), batch=True), 2),
                        'batch_per_clip': batch.PER_CLIP})
        return self._start('scan', work)

    @safe
    def start_judge(self):
        """Judge the scanned clips with the current settings: live (then review), or submit a batch."""
        if not self._scan: return {'error': 'Scan your clips first.'}
        s = self._settings
        if not s.output: return {'error': 'Pick an output folder first.'}
        # Clips already submitted in a batch that is still at Google are never sent again (no double paying).
        pending = self._pending_batch_digests()
        rows = [r for r in self._scan['rows'] if r['digest'] and (r['cached'] or r['digest'] not in pending)]
        if not rows: return {'error': 'Every clip in this range is already in a batch at Google.'}
        key = config.get_key()
        if any(not r['cached'] for r in rows) and not key: return {'error': 'Add your Gemini API key in Settings first.'}
        run_dir = Path(s.output)/datetime.datetime.now().strftime('Compilation %Y-%m-%d %H-%M-%S')
        run_dir.mkdir(parents=True)
        batch_mode = s.judge_mode == 'batch' and any(not r['cached'] for r in rows)
        run = {'created': datetime.datetime.now().isoformat(timespec='seconds'),
               'status': 'batch_uploading' if batch_mode else 'judging', 'judge_mode': 'batch' if batch_mode else 'live',
               'source': self._scan['source'], 'since': self._scan['since'], 'until': self._scan['until'],
               'mode': s.mode, 'min_score': s.min_score, 'target_minutes': s.target_minutes,
               'part_minutes': s.part_minutes, 'moments': [], 'decisions': {}, 'files': [],
               'clips': [[r['when'], r['hint'], str(r['path'])] for r in rows]}
        with self._lock: self._run_dir, self._run = run_dir, run
        self._save_run()
        if batch_mode: return self._submit_batch(run, run_dir, key)
        target = s.target_minutes*60 if s.mode == 'reel' else None

        def work(cancel):
            scratch = run_dir/'.upload-proxies'; scratch.mkdir(exist_ok=True)
            result = job.JudgeJob([(r['when'], r['hint'], r['path']) for r in rows], config.cache_dir(s), scratch,
                                  api_key=key, model=s.model, workers=s.workers, budget_usd=s.budget_usd or None,
                                  min_score=s.min_score, target_seconds=target, hasher=self._hasher,
                                  ffmpeg=self._ffmpeg[0], ffprobe=self._ffmpeg[1], emit=self._emit,
                                  cancel=cancel, judge_fn=self._judge_fn).run()
            scratch.rmdir() if scratch.exists() and not any(scratch.iterdir()) else None
            self._finish_judging(run, run_dir, result, target)
        return self._start('judge', work)

    def _finish_judging(self, run, run_dir, result, target):
        """Shared by live and batch: merge replays, preselect, save, and open the review."""
        cands = job.candidates(result['moments'], run['min_score'])
        keep = job.default_keep(cands, target)
        for m in cands:
            m['orig_start'], m['orig_end'] = m['start'], m['end']
        with self._lock:
            run.update(status='review', moments=cands, usage=result['usage'], stopped=result['stopped'],
                       decisions={m['id']: {'keep': m['id'] in keep} for m in cands})
            self._run_dir, self._run = run_dir, run
        atomic_json(run_dir/'report.json', result['report']); self._save_run()
        self._emit({'type': 'review_ready', 'moments': len(cands), 'stopped': result['stopped'], 'folder': str(run_dir)})

    # --- batch judging (half price; Google returns results within 24 h) ------------------------------

    def _client(self, key=None):
        return self._client_factory(key or config.get_key())

    @staticmethod
    def _run_clips(run):
        return [(w, h, Path(p)) for w, h, p in run['clips']]

    def _submit_batch(self, run, run_dir, key):
        s = self._settings

        def work(cancel):
            gc = self._client(key)
            try:
                state = batch.submit(self._run_clips(run), run_dir/'batch', config.cache_dir(s), gc, model=s.model,
                                     hasher=self._hasher, ffmpeg=self._ffmpeg[0], workers=max(4, s.workers),
                                     max_usd=s.budget_usd or None, emit=self._emit, cancel=cancel)
            except Cancelled:
                with self._lock: run['status'] = 'cancelled'
                self._save_run(); raise
            if not state.get('job'):  # Everything was already judged: straight to review.
                return self._collect_work(run, run_dir)(cancel)
            with self._lock: run.update(status='batch_waiting', batch=self._batch_summary(state))
            self._save_run()
            self._emit({'type': 'batch_waiting', 'folder': str(run_dir), 'batch': run['batch']})
        return self._start('batch', work)

    def _pending_batch_digests(self):
        """Content hashes of clips uploaded in batches that have not been collected yet."""
        found = set()
        if not self._settings.output or not Path(self._settings.output).is_dir(): return found
        for f in Path(self._settings.output).glob('Compilation */batch/batch.json'):
            try:
                state = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not state.get('collected') and state.get('status') not in ('cancelled', 'nothing'):
                found.update(state.get('clips', {}))
        return found

    @staticmethod
    def _batch_summary(state):
        return {'job': state.get('job'), 'clips': len(state['clips']), 'estimate_usd': state.get('estimate_usd'),
                'submitted': state.get('submitted'), 'expires': batch.expires_at(state), 'status': state.get('status'),
                'checked': state.get('checked'), 'failed': len(state.get('failed', [])),
                'judged': state.get('judged'), 'usd': state.get('usd')}

    def _target_run(self, folder):
        """(run_dir, run) for a folder, or the open compilation. Reads from disk without opening it."""
        if not folder or (self._run_dir and Path(folder) == self._run_dir):
            return self._run_dir, self._run
        folder = Path(folder)
        return folder, json.loads((folder/'run.json').read_text(encoding='utf-8'))

    @safe
    def check_batch(self, folder=None):
        """Ask Google how a waiting batch is doing; once it has finished, collect it and open the review."""
        run_dir, run = self._target_run(folder)
        if not run or run.get('status') != 'batch_waiting':
            return {'status': run.get('status') if run else None, 'done': False}
        state = batch.load(run_dir/'batch')
        job_ = batch.refresh(state, run_dir/'batch', self._client())
        run['batch'] = self._batch_summary(state)
        atomic_json(run_dir/'run.json', run)
        if run_dir == self._run_dir: self._run = run
        done = job_.state.name in batch.DONE
        collecting = False
        if done and not self._job:
            with self._lock: self._run_dir, self._run = run_dir, run
            self._start('collect', self._collect_work(run, run_dir, job_)); collecting = True
        return {'status': job_.state.name, 'done': done, 'collecting': collecting, 'batch': run['batch'],
                'folder': str(run_dir)}

    def _collect_work(self, run, run_dir, job_=None):
        def work(cancel):
            self._emit({'type': 'batch_collecting', 'folder': str(run_dir)})
            state = batch.load(run_dir/'batch'); usd = 0.
            if state and state.get('job'):
                state = batch.collect(state, run_dir/'batch', self._client(), config.cache_dir(self._settings), job_)
                usd = state['usd']
                with self._lock: run['batch'] = self._batch_summary(state)
            # Rebuild moments from the cache (no API calls); only failures are worth showing.
            quiet = lambda e: self._emit(e) if e['type'] == 'clip' and e['status'] == 'failed' else None
            result = job.JudgeJob(self._run_clips(run), config.cache_dir(self._settings), run_dir, cache_only=True,
                                  hasher=self._hasher, ffprobe=self._ffmpeg[1], emit=quiet, cancel=cancel).run()
            result['usage']['usd'] = usd
            if state and state.get('job') and state.get('status') != 'JOB_STATE_SUCCEEDED':
                result['stopped'] = 'batch_' + state['status'].replace('JOB_STATE_', '').lower()
            target = run['target_minutes']*60 if run['mode'] == 'reel' else None
            self._finish_judging(run, run_dir, result, target)
        return work

    @safe
    def cancel_batch(self, folder=None):
        """Cancel a batch at Google and delete its uploads. Nothing finished is charged."""
        if self._job == 'batch': self.cancel(); return {'ok': True}  # Still uploading: the job cleans up.
        run_dir, run = self._target_run(folder)
        state = batch.load(run_dir/'batch')
        if state: batch.cancel(state, run_dir/'batch', self._client())
        run.update(status='cancelled', batch=self._batch_summary(state) if state else None)
        atomic_json(run_dir/'run.json', run)
        if run_dir == self._run_dir: self._run = run
        return {'ok': True}

    @safe
    def resume_batch(self, folder=None):
        """Finish an upload that was interrupted (app closed, PC slept); uploads already made are reused."""
        run_dir, run = self._target_run(folder)
        if run.get('status') not in ('batch_uploading', 'cancelled') or run.get('judge_mode') != 'batch':
            return {'error': 'Nothing to resume.'}
        key = config.get_key()
        if not key: return {'error': 'Add your Gemini API key in Settings first.'}
        state = batch.load(run_dir/'batch')
        if state and state.get('job'): return {'error': 'This batch was already submitted.'}
        if state and state.get('status') == 'cancelled':
            state.pop('status'); batch.save(run_dir/'batch', state)
        with self._lock:
            run['status'] = 'batch_uploading'; self._run_dir, self._run = run_dir, run
        self._save_run()
        return self._submit_batch(run, run_dir, key)

    # --- review ------------------------------------------------------------------------------------

    def _save_run(self):
        with self._lock:
            if self._run_dir: atomic_json(self._run_dir/'run.json', self._run)

    def _view(self, m):
        d = self._run['decisions'].get(m['id'], {})
        start, end = d.get('start', m['start']), d.get('end', m['end'])
        return {'id': m['id'], 'clip': m['clip_name'], 'recorded_at': m['recorded_at'], 'score': m['score'],
                'reason': m['reason'], 'keep': d.get('keep', True), 'start': start, 'end': end,
                'orig_start': m['orig_start'], 'orig_end': m['orig_end'], 'duration': m['duration'],
                'window_start': max(0., m['orig_start']-PREVIEW_PAD), 'window_end': min(m['duration'], m['orig_end']+PREVIEW_PAD)}

    def _kept(self):
        out = []
        for m in self._run['moments']:
            v = self._view(m)
            if v['keep']: out.append(dict(m, start=v['start'], end=v['end']))
        return out

    def _totals(self):
        kept = self._kept(); seconds = sum(m['end']-m['start'] for m in kept)
        parts = len(split_parts(kept, self._run['part_minutes']*60)) if self._run['mode'] == 'parts' and kept else (1 if kept else 0)
        return {'kept': len(kept), 'total': len(self._run['moments']), 'minutes': round(seconds/60, 1), 'parts': parts}

    def _run_summary(self):
        if not self._run: return None
        return {k: self._run.get(k) for k in ('created', 'status', 'mode', 'min_score', 'target_minutes', 'part_minutes',
                                               'usage', 'stopped', 'files', 'judge_mode', 'batch')} | {'folder': str(self._run_dir),
                                                                                'moments': len(self._run['moments'])}

    def _recent_runs(self):
        if not self._settings.output or not Path(self._settings.output).is_dir(): return []
        runs = []
        for f in sorted(Path(self._settings.output).glob('Compilation */run.json'), reverse=True)[:6]:
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                runs.append({'folder': str(f.parent), 'created': data['created'], 'status': data['status'],
                             'moments': len(data['moments']), 'mode': data['mode'], 'files': data.get('files', []),
                             'judge_mode': data.get('judge_mode', 'live'), 'batch': data.get('batch')})
            except (OSError, ValueError, KeyError):
                continue
        return runs

    @safe
    def open_run(self, folder):
        path = Path(folder)
        data = json.loads((path/'run.json').read_text(encoding='utf-8'))
        if data['status'] == 'judging': data['status'] = 'review'  # Interrupted: review what was saved.
        with self._lock: self._run_dir, self._run = path, data
        return self.get_review()

    @safe
    def get_review(self):
        if not self._run: return {'error': 'No compilation open.'}
        return {'moments': [self._view(m) for m in self._run['moments']], 'totals': self._totals(),
                'run': self._run_summary()}

    @safe
    def set_decision(self, moment_id, keep=None, start=None, end=None):
        with self._lock:
            m = next((m for m in self._run['moments'] if m['id'] == moment_id), None)
            if not m: return {'error': 'Unknown moment'}
            d = self._run['decisions'].setdefault(moment_id, {'keep': True})
            if keep is not None: d['keep'] = bool(keep)
            lo, hi = max(0., m['orig_start']-PREVIEW_PAD), min(m['duration'], m['orig_end']+PREVIEW_PAD)
            if start is not None: d['start'] = round(min(max(lo, float(start)), hi-MIN_MOMENT), 2)
            if end is not None: d['end'] = round(max(min(hi, float(end)), lo+MIN_MOMENT), 2)
            s0, e0 = d.get('start', m['start']), d.get('end', m['end'])
            if e0-s0 < MIN_MOMENT:  # Keep the edited edge; move the other.
                if start is not None: d['end'] = round(min(hi, s0+MIN_MOMENT), 2)
                else: d['start'] = round(max(lo, e0-MIN_MOMENT), 2)
        self._save_run()
        return {'moment': self._view(m), 'totals': self._totals()}

    @safe
    def set_all(self, keep, min_score=None):
        with self._lock:
            for m in self._run['moments']:
                if min_score is None or m['score'] >= min_score:
                    self._run['decisions'].setdefault(m['id'], {})['keep'] = bool(keep)
        self._save_run()
        return self.get_review()

    def _media(self, moment_id, kind):
        m = next((m for m in self._run['moments'] if m['id'] == moment_id), None)
        if not m: raise ValueError('Unknown moment')
        folder = paths.previews_dir(); folder.mkdir(parents=True, exist_ok=True)
        name = f"{moment_id[:32]}.{kind}"; target = folder/name
        if not target.exists():
            ffmpeg = self._ffmpeg[0]; v = self._view(m); tmp = folder/f'.{name}.tmp.{kind}'
            with self._media_gate:
                if kind == 'jpg':
                    at = (m['orig_start']+m['orig_end'])/2
                    command([ffmpeg, '-v', 'error', '-y', '-ss', at, '-i', m['source_path'], '-frames:v', '1',
                             '-vf', 'scale=480:-2', '-q:v', '4', tmp], 60)
                else:
                    command([ffmpeg, '-v', 'error', '-y', '-ss', v['window_start'], '-i', m['source_path'],
                             '-t', v['window_end']-v['window_start'], '-map', '0:v:0', '-map', '0:a:0?',
                             '-vf', 'scale=-2:540,fps=30', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26',
                             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', tmp], 300)
            os.replace(tmp, target)
        return self._server.media_url(name) if self._server else str(target)

    @safe
    def thumbnail(self, moment_id):
        return {'url': self._media(moment_id, 'jpg')}

    @safe
    def preview(self, moment_id):
        return {'url': self._media(moment_id, 'mp4')}

    # --- render ------------------------------------------------------------------------------------

    @safe
    def start_render(self):
        if not self._run: return {'error': 'No compilation open.'}
        kept = self._kept()
        if not kept: return {'error': 'Keep at least one moment.'}
        s, run, run_dir = self._settings, self._run, self._run_dir
        settings = job.render_settings(self._ffmpeg[0], self._ffmpeg[1], height=s.height, fps=s.fps, loudnorm=s.loudnorm,
                                       timestamp_overlay=s.timestamp_overlay, workers=s.workers, hasher=self._hasher)

        def work(cancel):
            if run['mode'] == 'parts':
                files, _ = job.render_parts(kept, run_dir, run['part_minutes']*60, settings, emit=self._emit, cancel=cancel)
            else:
                files = [str(job.render_reel(kept, run_dir, settings, emit=self._emit, cancel=cancel))]
            with self._lock: run.update(status='rendered', files=files)
            self._save_run()
        return self._start('render', work)
