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

from .. import APP_NAME, __version__, batch, gemini, job, paths, settings as config, skill
from ..media import Cancelled, audio_args, audio_plan, command, find_ffmpeg
from ..reel import discover, split_parts
from ..store import HashIndex, atomic_json, awake
import json

log = logging.getLogger(__name__)
PREVIEW_PAD = 3.         # Seconds of context either side of a moment, so trims can grow it.
MIN_MOMENT = 1.
ALLOWED_URLS = ('https://aistudio.google.com/', 'https://ai.google.dev/')
BELOW_NORMAL, NORMAL = 0x4000, 0x20
PROFILES_README = '''Skill profiles: how Auto Clip Compiler scores skill plays for each game.

The *.default.json files are the built-in profiles, for reference. Editing them does nothing.
To change a game, create cs2.json, rust.json or generic.json here holding ONLY the values you want to change,
for example {"kill": {"headshot": 0.4}, "penalties": {"enemy_unaware": 0.5}}.
Scores are worked out from saved judgments, so a change applies to the next review at no cost. Re-open the
compilation to see it. A file that is not valid JSON is ignored.
'''


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


def recycle(folder):
    """Send a folder to the Recycle Bin, so a discarded draft can still be restored."""
    if os.name != 'nt':
        import shutil
        shutil.rmtree(folder); return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
                    ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]
    # FO_DELETE with ALLOWUNDO (Recycle Bin), NOCONFIRMATION, SILENT, NOERRORUI. pFrom must end in two NULs.
    op = SHFILEOPSTRUCTW(wFunc=3, pFrom=str(Path(folder).resolve())+'\0', fFlags=0x40 | 0x10 | 0x4 | 0x400)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or Path(folder).exists():
        raise OSError(f"Couldn't move {Path(folder).name} to the Recycle Bin (is a file in it open?).")


class Api:
    def __init__(self, server=None, judge_fn=None, client_factory=None, skill_fn=None):
        self._settings = config.load()
        self._server = server; self._window = None; self._judge_fn = judge_fn; self._skill_fn = skill_fn
        self._client_factory = client_factory or gemini.client
        self._lock = threading.RLock(); self._events = []; self._seq = 0
        self._job = None; self._cancel = None; self._scan = None
        self._run_dir = None; self._run = None
        self._hasher = HashIndex(paths.hash_index_path())
        self._ffmpeg = find_ffmpeg()
        self._media_gate = threading.Semaphore(3)    # Thumbnails.
        self._preview_gate = threading.Semaphore(2)  # Previews get their own slots, never queued behind thumbnails.

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
                'busy': self._job, 'run': self._run_summary(), 'recent': self._recent_runs(), 'drafts': self._drafts(),
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
        roots += [paths.logs_dir().resolve(), *(Path(f).resolve() for f in self._remembered())]
        if not any(target == r or r in target.parents for r in roots) or not target.exists():
            return {'error': 'That location is not one the app created.'}
        os.startfile(target); return {'ok': True}

    @safe
    def storage(self):
        def size(folder):
            return sum(p.stat().st_size for p in Path(folder).rglob('*') if p.is_file()) if Path(folder).exists() else 0
        cache = config.cache_dir(self._settings)
        return {'cache_files': len(list(cache.glob('*.json'))) if cache.exists() else 0,
                'skill_files': len(list((cache/'skill').glob('*.json'))) if (cache/'skill').exists() else 0,
                'previews_mb': round(size(paths.previews_dir())/1e6, 1), 'logs': str(paths.logs_dir())}

    @safe
    def edit_profiles(self):
        """Open the folder for skill-profile tweaks, seeded with the defaults (as examples) and a how-to."""
        folder = config.profiles_dir(); folder.mkdir(parents=True, exist_ok=True)
        for f in skill.PROFILE_DIR.glob('*.json'):
            example = folder/f'{f.stem}.default.json'
            if not example.exists(): example.write_text(f.read_text(encoding='utf-8'), encoding='utf-8')
        readme = folder/'README.txt'
        if not readme.exists():
            readme.write_text(PROFILES_README, encoding='utf-8')
        os.startfile(folder); return {'ok': True, 'folder': str(folder)}

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
            rows = job.scan(clips, config.cache_dir(self._settings), self._hasher, emit=self._emit, cancel=cancel,
                            profiles=config.profiles(self._settings))
            pending = self._pending_batch_digests()
            for r in rows: r['pending'] = r['digest'] in pending
            self._scan = {'source': str(source), 'since': since, 'until': until, 'rows': rows, 'selected': None}
            self._emit(dict(self._scan_summary(), type='scan_done'))
        return self._start('scan', work)

    def _chosen_rows(self):
        """The scanned clips the user picked (all of them unless a selection was made)."""
        selected = self._scan.get('selected')
        return [r for r in self._scan['rows'] if selected is None or str(r['path']) in selected]

    def _scan_summary(self):
        """Counts and cost estimates (per compilation type) for the chosen clips. Local only: no API calls."""
        rows = self._chosen_rows(); everything = self._scan['rows']
        estimates = {mode: self._estimate(rows, mode) for mode in job.MODES}
        now = estimates[self._settings.content_mode]
        return {'clips': len(rows), 'total': len(everything), 'picked': self._scan.get('selected') is not None,
                'cached': now['ready'], 'to_judge': now['clips'], 'in_batch': now['in_batch'],
                'unreadable': sum(r['digest'] is None for r in rows),
                'gb': round(sum(r['size'] for r in rows)/1e9, 1),
                'first': everything[-1]['when'] if everything else None, 'last': everything[0]['when'] if everything else None,
                'estimate_usd': now['live'], 'estimate_batch_usd': now['batch'], 'estimates': estimates,
                'batch_per_clip': batch.PER_CLIP}

    @safe
    def scan_clips(self):
        """Every clip from the last scan, newest first, for the clip picker: what it is, whether judging it
        would cost anything for the current compilation type, and whether it is chosen."""
        if not self._scan: return {'error': 'Scan your clips first.'}
        mode = self._settings.content_mode; selected = self._scan.get('selected')
        return {'clips': [{'path': str(r['path']), 'name': r['path'].name, 'when': r['when'], 'mb': round(r['size']/1e6, 1),
                           'ready': bool(r['digest']) and bool(self._ready(r, mode)), 'pending': bool(r['pending']),
                           'unreadable': r['digest'] is None, 'chosen': selected is None or str(r['path']) in selected}
                          for r in self._scan['rows']]}

    @safe
    def select_clips(self, paths=None):
        """Choose which scanned clips to use (None = all of them). Returns the updated counts and estimates."""
        if not self._scan: return {'error': 'Scan your clips first.'}
        if paths is None:
            self._scan['selected'] = None
        else:
            known = {str(r['path']) for r in self._scan['rows']}
            chosen = {p for p in paths if p in known}
            self._scan['selected'] = None if chosen == known else chosen
        return self._scan_summary()

    @safe
    def open_clip(self, path):
        """Play a scanned clip in the default video player (only clips from the last scan)."""
        if not self._scan or path not in {str(r['path']) for r in self._scan['rows']}:
            return {'error': 'That clip is not in the current scan.'}
        os.startfile(path); return {'ok': True}

    @staticmethod
    def _ready(row, mode):
        """Has this clip everything the compilation type needs, so judging it costs nothing?"""
        funny = row['cached']; skilled = row.get('skill_triaged') and not row.get('skill_pending')
        return {'funny': funny, 'skill': skilled, 'both': funny and skilled}[mode]

    def _estimate(self, rows, mode):
        """Clips still to judge for a compilation type and what that costs (skill is approximate until measured)."""
        todo = [r for r in rows if r['digest'] and not self._ready(r, mode) and not r['pending']]
        live = 0.
        if mode in ('funny', 'both'):
            live += gemini.estimate(sum(not r['cached'] for r in todo))
        if mode in ('skill', 'both'):
            fresh = sum(not r.get('skill_triaged') for r in todo)
            owed = sum(r.get('skill_pending', 0) for r in todo)
            live += fresh*gemini.cost(skill.TYPICAL_TRIAGE) + (fresh*skill.PLAYS_PER_CLIP + owed)*gemini.cost(skill.TYPICAL_DETAIL)
        return {'clips': len(todo), 'live': round(live, 2), 'batch': round(live*gemini.BATCH_DISCOUNT, 2),
                'approx': mode != 'funny', 'ready': sum(1 for r in rows if r['digest'] and self._ready(r, mode)),
                'in_batch': sum(1 for r in rows if r['digest'] and not self._ready(r, mode) and r['pending'])}

    @safe
    def start_judge(self):
        """Judge the scanned clips with the current settings: live (then review), or submit a batch."""
        if not self._scan: return {'error': 'Scan your clips first.'}
        s = self._settings
        if not s.output: return {'error': 'Pick an output folder first.'}
        # Clips already submitted in a batch that is still at Google are never sent again (no double paying).
        pending = self._pending_batch_digests()
        mode = s.content_mode
        chosen = self._chosen_rows()
        if not chosen: return {'error': 'Choose at least one clip.'}
        rows = [r for r in chosen if r['digest'] and (self._ready(r, mode) or r['digest'] not in pending)]
        if not rows: return {'error': 'Every chosen clip is already in a batch at Google.'}
        key = config.get_key()
        unready = any(not self._ready(r, mode) for r in rows)
        if unready and not key: return {'error': 'Add your Gemini API key in Settings first.'}
        run_dir = Path(s.output)/datetime.datetime.now().strftime('Compilation %Y-%m-%d %H-%M-%S')
        run_dir.mkdir(parents=True); self._remember(run_dir)
        batch_mode = s.judge_mode == 'batch' and unready
        run = {'created': datetime.datetime.now().isoformat(timespec='seconds'),
               'status': 'batch_uploading' if batch_mode else 'judging', 'judge_mode': 'batch' if batch_mode else 'live',
               'source': self._scan['source'], 'since': self._scan['since'], 'until': self._scan['until'],
               'mode': s.mode, 'min_score': s.min_score, 'target_minutes': s.target_minutes,
               'part_minutes': s.part_minutes, 'moments': [], 'decisions': {}, 'files': [],
               'content_mode': mode, 'min_skill_score': s.min_skill_score, 'skill_share': s.skill_share,
               'skill_preset': s.skill_preset, 'game_override': s.game_override,
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
                                  cancel=cancel, judge_fn=self._judge_fn, skill_fn=self._skill_fn,
                                  **self._skill_options(run)).run()
            scratch.rmdir() if scratch.exists() and not any(scratch.iterdir()) else None
            self._finish_judging(run, run_dir, result, target)
        return self._start('judge', work)

    @staticmethod
    def _skill_options(run):
        """JudgeJob options for a run's compilation type (runs from v1 have none: funny)."""
        mode = run.get('content_mode', 'funny')
        if mode == 'funny': return {}
        return {'content_mode': mode, 'min_skill_score': run['min_skill_score'], 'skill_share': run['skill_share'],
                'profiles': skill.Profiles(run['skill_preset'], run['game_override'], config.profiles_dir())}

    def _finish_judging(self, run, run_dir, result, target):
        """Shared by live and batch: merge replays, preselect, save, and open the review."""
        mode = run.get('content_mode', 'funny')
        cands = job.candidates(result['moments'], run['min_score'], None if mode == 'funny' else run['min_skill_score'])
        keep = job.default_keep(cands, target, run['skill_share'] if mode == 'both' else None)
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

    @staticmethod
    def _batch_dir(run_dir, run):
        """Stage 1 (funny and/or skill triage) lives in batch/, the skill close-ups in batch-2/."""
        return run_dir/('batch-2' if run.get('stage') == 2 else 'batch')

    def _submit_batch(self, run, run_dir, key):
        s = self._settings
        kinds = {'funny': ('funny',), 'skill': ('triage',), 'both': ('funny', 'triage')}[run.get('content_mode', 'funny')]

        def work(cancel):
            gc = self._client(key)
            try:
                if run.get('stage') == 2:
                    state = self._submit_details(run, run_dir, gc, cancel)
                else:
                    state = batch.submit(self._run_clips(run), run_dir/'batch', config.cache_dir(s), gc, model=s.model,
                                         hasher=self._hasher, ffmpeg=self._ffmpeg[0], workers=max(4, s.workers),
                                         max_usd=s.budget_usd or None, emit=self._emit, cancel=cancel, kinds=kinds)
            except Cancelled:
                with self._lock: run['status'] = 'cancelled'
                self._save_run(); raise
            if not state.get('job'):  # Everything was already judged: straight on.
                return self._collect_work(run, run_dir)(cancel)
            self._waiting(run, run_dir, state)
        return self._start('batch', work)

    def _submit_details(self, run, run_dir, gc, cancel):
        s = self._settings; spent = run.get('usd_paid', 0.)
        room = max(0., s.budget_usd - spent) if s.budget_usd else None
        return batch.submit_details(self._run_clips(run), run_dir/'batch-2', config.cache_dir(s), gc, model=s.model,
                                    profiles=self._skill_options(run)['profiles'], hasher=self._hasher,
                                    ffmpeg=self._ffmpeg[0], ffprobe=self._ffmpeg[1], workers=max(4, s.workers),
                                    max_usd=room, emit=self._emit, cancel=cancel)

    def _waiting(self, run, run_dir, state):
        with self._lock: run.update(status='batch_waiting', batch=self._batch_summary(state))
        self._save_run()
        self._emit({'type': 'batch_waiting', 'folder': str(run_dir), 'batch': run['batch'], 'stage': run.get('stage', 1)})

    def _pending_batch_digests(self):
        """Content hashes of clips uploaded in batches that have not been collected yet (either stage)."""
        found = set()
        if not self._settings.output or not Path(self._settings.output).is_dir(): return found
        for f in Path(self._settings.output).glob('Compilation */batch*/batch.json'):
            try:
                state = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not state.get('collected') and state.get('status') not in ('cancelled', 'nothing'):
                found.update(k.split(':')[0] for k in state.get('clips', {}))
        return found

    @staticmethod
    def _batch_summary(state):
        return {'job': state.get('job'), 'clips': len(state['clips']), 'estimate_usd': state.get('estimate_usd'),
                'submitted': state.get('submitted'), 'expires': batch.expires_at(state), 'status': state.get('status'),
                'checked': state.get('checked'), 'failed': len(state.get('failed', [])),
                'judged': state.get('judged'), 'usd': state.get('usd'), 'stage': state.get('stage', 1)}

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
        bdir = self._batch_dir(run_dir, run)
        state = batch.load(bdir)
        job_ = batch.refresh(state, bdir, self._client())
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
            self._emit({'type': 'batch_collecting', 'folder': str(run_dir), 'stage': run.get('stage', 1)})
            bdir = self._batch_dir(run_dir, run)
            state = batch.load(bdir)
            if state and state.get('job'):
                if not state.get('collected'):   # Count each stage's spend once, even if collect runs twice.
                    state = batch.collect(state, bdir, self._client(), config.cache_dir(self._settings), job_)
                    with self._lock: run['usd_paid'] = round(run.get('usd_paid', 0.) + state['usd'], 4)
                with self._lock: run['batch'] = self._batch_summary(state)
            failed = state and state.get('job') and state.get('status') != 'JOB_STATE_SUCCEEDED'
            if run.get('content_mode', 'funny') != 'funny' and run.get('stage', 1) == 1 and not failed:
                # Skill stage 2: close-ups of the plays triage found, as a second batch (PC on to upload).
                with self._lock: run.update(stage=2, status='batch_uploading')
                self._save_run()
                try:
                    details = self._submit_details(run, run_dir, self._client(), cancel)
                except Cancelled:
                    with self._lock: run['status'] = 'cancelled'
                    self._save_run(); raise
                if details.get('job'):
                    return self._waiting(run, run_dir, details)
                state = None   # Nothing needed a close-up: straight to review.
            # Rebuild moments from the cache (no API calls); only failures are worth showing.
            quiet = lambda e: self._emit(e) if e['type'] == 'clip' and e['status'] == 'failed' else None
            result = job.JudgeJob(self._run_clips(run), config.cache_dir(self._settings), run_dir, cache_only=True,
                                  hasher=self._hasher, ffprobe=self._ffmpeg[1], emit=quiet, cancel=cancel,
                                  **self._skill_options(run)).run()
            result['usage']['usd'] = run.get('usd_paid', 0.)
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
        bdir = self._batch_dir(run_dir, run)
        state = batch.load(bdir)
        if state: batch.cancel(state, bdir, self._client())
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
        bdir = self._batch_dir(run_dir, run)
        state = batch.load(bdir)
        if state and state.get('job'): return {'error': 'This batch was already submitted.'}
        if state and state.get('status') == 'cancelled':
            state.pop('status'); batch.save(bdir, state)
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
                'window_start': max(0., m['orig_start']-PREVIEW_PAD), 'window_end': min(m['duration'], m['orig_end']+PREVIEW_PAD),
                'lenses': m.get('lenses', ['funny']), 'lens': m.get('lens', m.get('lenses', ['funny'])[0]),
                'funny_score': m.get('funny_score'), 'skill_score': m.get('skill_score'),
                'skill_tags': m.get('skill_tags', []), 'skill_drawbacks': m.get('skill_drawbacks', []),
                'skill_reason': m.get('skill_reason'), 'game': m.get('game'), 'kills': m.get('kills', [])}

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
                                               'usage', 'stopped', 'files', 'judge_mode', 'batch', 'min_skill_score',
                                               'skill_share', 'stage')} | {
            'folder': str(self._run_dir), 'moments': len(self._run['moments']),
            'content_mode': self._run.get('content_mode', 'funny')}

    @staticmethod
    def _run_row(f):
        """One compilation's list entry from its run.json, or None if it is missing or unreadable."""
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            return {'folder': str(f.parent), 'created': data['created'], 'status': data['status'],
                    'moments': len(data['moments']), 'mode': data['mode'], 'files': data.get('files', []),
                    'judge_mode': data.get('judge_mode', 'live'), 'batch': data.get('batch'),
                    'content_mode': data.get('content_mode', 'funny'), 'stage': data.get('stage', 1),
                    'usd': (data.get('usage') or {}).get('usd', data.get('usd_paid'))}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _recent_runs(self, limit=6):
        if not self._settings.output or not Path(self._settings.output).is_dir(): return []
        rows = (self._run_row(f) for f in sorted(Path(self._settings.output).glob('Compilation */run.json'), reverse=True))
        return [r for r in rows if r][:limit]

    @staticmethod
    def _remembered():
        try:
            data = json.loads(paths.runs_path().read_text(encoding='utf-8'))
            return [str(f) for f in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _remember(self, *folders):
        with self._lock:
            known = self._remembered(); seen = {os.path.normcase(f) for f in known}
            new = [str(Path(f).resolve()) for f in folders]
            new = [f for f in dict.fromkeys(new) if os.path.normcase(f) not in seen]
            if new:
                paths.runs_path().parent.mkdir(parents=True, exist_ok=True)
                atomic_json(paths.runs_path(), known+new)

    @safe
    def history(self):
        """Every compilation, newest first: the remembered ones plus any found in, next to or one level
        under the output folder (so runs made before 1.1.4, or in an earlier output folder, show up too)."""
        found = [Path(f) for f in self._remembered()]
        out = Path(self._settings.output) if self._settings.output else None
        if out and out.is_dir():
            for pattern, base in (('Compilation */run.json', out), ('Compilation */run.json', out.parent),
                                  ('*/Compilation */run.json', out)):
                found += [f.parent for f in base.glob(pattern)]
        rows, seen = [], set()
        for folder in found:
            key = os.path.normcase(str(folder.resolve()))
            if key in seen: continue
            seen.add(key)
            row = self._run_row(folder/'run.json')
            if row: rows.append(row)
        self._remember(*(r['folder'] for r in rows))
        return {'runs': sorted(rows, key=lambda r: r['created'], reverse=True)}

    def _drafts(self):
        """Every compilation not rendered yet: stopped, cancelled, in review or waiting on a batch."""
        return [r for r in self._recent_runs(limit=None) if r['status'] != 'rendered']

    @safe
    def discard_run(self, folder):
        """Move an unrendered compilation's folder to the Recycle Bin. A batch still at Google is cancelled
        first so nothing is charged; saved judgments stay in the cache, so re-judging those clips is free."""
        if self._job: return {'error': 'Wait for the current job to finish.'}
        path = Path(folder)
        out = Path(self._settings.output) if self._settings.output else None
        if not (out and path.parent.resolve() == out.resolve() and path.name.startswith('Compilation ')
                and (path/'run.json').is_file()):
            return {'error': 'That is not a compilation in your output folder.'}
        run = json.loads((path/'run.json').read_text(encoding='utf-8'))
        if run['status'] == 'rendered': return {'error': 'Only drafts can be discarded here.'}
        if run['status'] == 'batch_waiting':
            done = self.cancel_batch(str(path))
            if 'error' in done: return done
        recycle(path)
        with self._lock:
            if self._run_dir and self._run_dir.resolve() == path.resolve(): self._run_dir = self._run = None
        return {'ok': True}

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
        # 'a2' (hex, so the media server serves it): previews made before 1.1.2 carried only the first
        # audio track, and 1.1.2's played a [mix, mic] clip's mic twice; those get remade.
        name = f"{moment_id[:32]}{'a2' if kind == 'mp4' else ''}.{kind}"; target = folder/name
        if not target.exists():
            ffmpeg = self._ffmpeg[0]; v = self._view(m); tmp = folder/f'.{name}.tmp.{kind}'
            with (self._media_gate if kind == 'jpg' else self._preview_gate):
                if kind == 'jpg':
                    at = (m['orig_start']+m['orig_end'])/2
                    command([ffmpeg, '-v', 'error', '-y', '-ss', at, '-i', m['source_path'], '-frames:v', '1',
                             '-vf', 'scale=480:-2', '-q:v', '4', tmp], 60)
                else:
                    _, plan = audio_plan(m['source_path'], ffmpeg, self._ffmpeg[1])
                    command([ffmpeg, '-v', 'error', '-y', '-ss', v['window_start'], '-i', m['source_path'],
                             '-t', v['window_end']-v['window_start'], '-map', '0:v:0', *audio_args(plan),
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
