"""Judging and rendering as cancellable jobs that report progress through events instead of printing.

Every event is a dict with a 'type'. A job never raises for one bad clip: failures are reported and
the clip stays un-cached, so a later run retries it. Judgments are cached by content hash, so a
cancelled or interrupted run never pays twice.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import threading
import time

from . import gemini
from .media import Cancelled, probe
from .reel import dedupe, part_name, part_plan, pick, split_parts, moments_for, day
from .render import publish
from .store import atomic_json, file_hash

log = logging.getLogger(__name__)
AUTH_HINTS = ('api key not valid', 'api_key_invalid', 'permission denied', 'unauthenticated')


def scan(clips, cache, hasher=file_hash, emit=None, cancel=None, workers=4):
    """Hash every clip (instant when indexed) and mark which already have a cached judgment."""
    emit = emit or (lambda e: None); cache = Path(cache); total = len(clips); done = [0]; lock = threading.Lock()

    def one(clip):
        when, hint, path = clip
        if cancel and cancel.is_set(): return None
        try:
            digest = hasher(path); cached = (cache/f'{digest}.json').exists()
        except Exception as exc:
            digest, cached = None, False; log.warning('%s: cannot read (%s)', path.name, exc)
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == total: emit({'type': 'scan', 'done': done[0], 'total': total})
        return dict(when=when, hint=hint, path=path, digest=digest, cached=cached, size=path.stat().st_size)

    with ThreadPoolExecutor(workers) as pool:
        rows = [r for r in pool.map(one, clips) if r]
    if cancel and cancel.is_set(): raise Cancelled()
    return rows


class JudgeJob:
    def __init__(self, clips, cache, scratch, *, api_key=None, model=gemini.DEFAULT_MODEL, workers=4,
                 budget_usd=None, min_score=7, target_seconds=None, cache_only=False, hasher=file_hash,
                 ffmpeg='ffmpeg', ffprobe='ffprobe', emit=None, cancel=None, judge_fn=None):
        """clips: (recorded_at, time_from_filename, path) newest first. target_seconds: stop once this much
        reel-worthy footage exists (None judges every clip). judge_fn(path, stop) replaces Gemini in tests."""
        self.clips, self.cache, self.scratch = clips, Path(cache), Path(scratch)
        self.cache.mkdir(parents=True, exist_ok=True)  # Fresh installs have no cache folder yet.
        self.api_key, self.model, self.workers = api_key, model, workers
        self.budget_usd, self.min_score, self.target = budget_usd, min_score, target_seconds
        self.cache_only, self.hasher, self.ffmpeg, self.ffprobe = cache_only, hasher, ffmpeg, ffprobe
        self.emit = emit or (lambda e: None); self.judge_fn = judge_fn
        self.stop = cancel or threading.Event(); self.reason = None
        self.lock = threading.Lock(); self.local = threading.local()
        self.report, self.moments, self.done = [], [], 0
        self.usage = {'clips': 0, 'cached': 0, 'failed': 0, 'input_tokens': 0, 'output_tokens': 0,
                      'thinking_tokens': 0, 'usd': 0.}

    def halt(self, reason, message=''):
        """Stop starting new clips; clips already being judged finish and are cached."""
        if not self.stop.is_set():
            self.reason = reason; self.stop.set()
            self.emit({'type': 'stopping', 'reason': reason, 'message': message[:300]})

    def client(self):
        if not hasattr(self.local, 'client'):  # One client (and connection pool) per worker thread.
            self.local.client = gemini.client(self.api_key)
        return self.local.client

    def judge(self, path):
        if self.judge_fn: return self.judge_fn(path, self.stop)
        return gemini.judge(self.client(), self.model, path, self.stop, self.scratch, self.ffmpeg)

    def process(self, clip):
        when, hint, path = clip
        if self.stop.is_set(): return
        fresh = False
        try:
            digest = self.hasher(path); target = self.cache/f'{digest}.json'
            if target.exists():
                result = json.loads(target.read_text(encoding='utf-8'))
            elif self.cache_only:
                return self.finish(path, 'unjudged', error='not judged yet (cache-only run)')
            else:
                result = self.judge(path); fresh = True
                atomic_json(target, result)
            meta = probe(path, self.ffprobe)
        except Cancelled:
            return
        except gemini.DailyLimit as exc:
            return self.halt('daily_limit', str(exc))
        except Exception as exc:
            text = str(exc)
            if getattr(exc, 'code', None) in (400, 401, 403) and any(h in text.lower() for h in AUTH_HINTS):
                self.halt('auth', text)  # Every clip would fail the same way.
            log.warning('%s: FAILED %s', path.name, text)
            return self.finish(path, 'failed', error=text[:500])
        found = list(moments_for(path, when, hint, result, meta, digest))
        with self.lock:
            self.usage['clips'] += 1; self.usage['cached'] += not fresh
            if fresh:
                spent = gemini.cost(result['_meta'])
                for key in ('input_tokens', 'output_tokens', 'thinking_tokens'):
                    self.usage[key] += result['_meta'].get(key) or 0
                self.usage['usd'] += spent
                # Clips already in flight on other workers still bill: stop with room for them.
                if self.budget_usd and self.usage['usd'] + self.workers*spent >= self.budget_usd:
                    self.halt('budget', f"${self.usage['usd']:.2f} spent")
            self.report.append({'clip': path.name, **result})
            self.moments.extend(found)
            minutes = None
            if self.target:  # Reel mode: stop a little past the target so ranking has choices.
                have = sum(m['end']-m['start'] for m in dedupe([dict(m) for m in self.moments if m['score'] >= self.min_score]))
                minutes = have/60
                if have >= self.target*1.1: self.halt('target')
            self.finish(path, 'judged' if fresh else 'cached', scores=[m['score'] for m in found], minutes=minutes,
                        reasons=[m['reason'] for m in found if m['score'] >= self.min_score][:2], locked=True)

    def finish(self, path, status, locked=False, **details):
        def record():
            self.done += 1
            if status in ('failed', 'unjudged'):
                self.usage['failed'] += status == 'failed'
                self.report.append({'clip': path.name, 'error': details.get('error')})
            self.emit({'type': 'clip', 'name': path.name, 'status': status, 'done': self.done,
                       'total': len(self.clips), 'usd': round(self.usage['usd'], 4), **details})
        if locked: record()
        else:
            with self.lock: record()

    def run(self):
        started = time.monotonic()
        self.emit({'type': 'judge_start', 'total': len(self.clips)})
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(self.process, self.clips))
        reason = self.reason or ('cancelled' if self.stop.is_set() else None)
        self.usage['judged_seconds'] = round(time.monotonic()-started)
        self.emit({'type': 'judge_done', 'stopped': reason, 'usage': dict(self.usage)})
        return {'moments': self.moments, 'report': self.report, 'usage': self.usage, 'stopped': reason}


def candidates(moments, min_score):
    """Reel-worthy moments with replays merged, in recording order."""
    good = dedupe([dict(m) for m in moments if m['score'] >= min_score])
    return sorted(good, key=lambda m: (m['recorded_at'], m['start']))


def default_keep(cands, target_seconds=None):
    """Ids kept before any review: the best `target_seconds` of footage, or everything."""
    if not target_seconds: return {m['id'] for m in cands}
    return {m['id'] for m in pick(cands, target_seconds)[0]}


def render_settings(ffmpeg='ffmpeg', ffprobe='ffprobe', height=1080, fps=60, loudnorm=True,
                    timestamp_overlay=False, workers=4, hasher=None):
    return {'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'fps': fps, 'render_height': height, 'workers': workers,
            'resolve': True, 'render': True, 'min_free_gb': 5, 'loudnorm': loudnorm,
            'timestamp_overlay': timestamp_overlay, 'hasher': hasher}


def unique(path):
    path = Path(path); n = 2
    while path.exists():
        path = path.with_name(f'{path.stem.rsplit(" (#", 1)[0]} (#{n}){path.suffix}'); n += 1
    return path


def render_reel(chosen, out, settings, emit=None, cancel=None, name=None):
    """One chronological video; returns its path. Never overwrites an earlier reel."""
    emit = emit or (lambda e: None); out = Path(out)
    chosen = sorted(chosen, key=lambda m: (m['recorded_at'], m['start']))
    name = name or f"Highlights ({day(chosen[0]['recorded_at'])} to {day(chosen[-1]['recorded_at'])}).mp4"
    emit({'type': 'render_start', 'parts': 1})
    final = publish(chosen, out, settings, cancel=cancel,
                    on_progress=lambda d, n: emit({'type': 'render', 'part': 1, 'parts': 1, 'done': d, 'total': n}))
    target = unique(out/name); os.replace(final/'Combined_highlights.mp4', target)  # FCPXML/manifest stay behind.
    emit({'type': 'render_done', 'files': [str(target)], 'folder': str(final)})
    return target


def render_parts(moments, out, part_seconds, settings, emit=None, cancel=None, only=None):
    """Chronological parts; an existing part file is kept, so a re-run renders only what's missing."""
    emit = emit or (lambda e: None); out = Path(out)
    parts = split_parts(moments, part_seconds); plan = part_plan(parts)
    emit({'type': 'render_start', 'parts': len(parts), 'plan': plan}); files = []
    for row, part in zip(plan, parts):
        if only and row['part'] not in only: continue
        target = out/part_name(row)
        if not target.exists():
            final = publish(part, out/f"part-{row['part']:02}", settings, cancel=cancel,
                            on_progress=lambda d, n, i=row['part']: emit({'type': 'render', 'part': i, 'parts': len(parts),
                                                                         'done': d, 'total': n}))
            os.replace(final/'Combined_highlights.mp4', target)
        files.append(str(target))
        emit({'type': 'part_done', 'part': row['part'], 'file': str(target)})
    emit({'type': 'render_done', 'files': files})
    return files, plan
