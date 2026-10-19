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

from . import context, gemini, skill
from .media import Cancelled, probe, window
from .reel import dedupe, merge_lenses, part_name, part_plan, pick, split_parts, moments_for, day
from .render import publish
from .store import atomic_json, file_hash

log = logging.getLogger(__name__)
# A rejected key or a project Google has blocked: every clip would fail the same way, so the run stops.
AUTH_HINTS = ('api key not valid', 'api_key_invalid', 'permission denied', 'permission_denied', 'denied access',
              'unauthenticated')


def blocked(exc):
    return getattr(exc, 'code', None) in (400, 401, 403) and any(h in str(exc).lower() for h in AUTH_HINTS)
MODES = ('funny', 'skill', 'both')


def scan(clips, cache, hasher=file_hash, emit=None, cancel=None, workers=4, profiles=None):
    """Hash every clip (instant when indexed) and mark which already have a cached judgment.
    cached = funny judgment saved. With profiles, also skill_triaged and skill_pending (close-ups still owed)."""
    emit = emit or (lambda e: None); cache = Path(cache); total = len(clips); done = [0]; lock = threading.Lock()

    def one(clip):
        when, hint, path = clip
        if cancel and cancel.is_set(): return None
        try:
            digest = hasher(path); cached = (cache/f'{digest}.json').exists()
            record = skill.load_record(cache, digest) if profiles else None
        except Exception as exc:
            digest, cached, record = None, False, None; log.warning('%s: cannot read (%s)', path.name, exc)
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == total: emit({'type': 'scan', 'done': done[0], 'total': total})
        row = dict(when=when, hint=hint, path=path, digest=digest, cached=cached, size=path.stat().st_size)
        if profiles:
            row['skill_triaged'] = record is not None
            row['skill_pending'] = len(skill.pending_plays(record, profiles.for_record(record))) if record else 0
        return row

    with ThreadPoolExecutor(workers) as pool:
        rows = [r for r in pool.map(one, clips) if r]
    if cancel and cancel.is_set(): raise Cancelled()
    return rows


class JudgeJob:
    def __init__(self, clips, cache, scratch, *, api_key=None, model=gemini.DEFAULT_MODEL, workers=4,
                 budget_usd=None, min_score=7, target_seconds=None, cache_only=False, hasher=file_hash,
                 ffmpeg='ffmpeg', ffprobe='ffprobe', emit=None, cancel=None, judge_fn=None,
                 content_mode='funny', profiles=None, min_skill_score=7, skill_share=.4, skill_fn=None):
        """clips: (recorded_at, time_from_filename, path) newest first. target_seconds: stop once this much
        reel-worthy footage exists (None judges every clip). content_mode: funny, skill or both.
        Tests replace Gemini with judge_fn(path, stop) (funny) and skill_fn(kind, path, stop, profile, play,
        window) (kind 'triage' or 'detail')."""
        self.clips, self.cache, self.scratch = clips, Path(cache), Path(scratch)
        self.cache.mkdir(parents=True, exist_ok=True)  # Fresh installs have no cache folder yet.
        self.api_key, self.model, self.workers = api_key, model, workers
        self.budget_usd, self.min_score, self.target = budget_usd, min_score, target_seconds
        self.cache_only, self.hasher, self.ffmpeg, self.ffprobe = cache_only, hasher, ffmpeg, ffprobe
        self.emit = emit or (lambda e: None); self.judge_fn = judge_fn; self.skill_fn = skill_fn
        if content_mode not in MODES: raise ValueError(f'content_mode must be one of {MODES}')
        self.mode, self.profiles = content_mode, profiles or skill.Profiles()
        self.min_skill_score, self.skill_share = min_skill_score, skill_share
        self.funny, self.skilled = content_mode in ('funny', 'both'), content_mode in ('skill', 'both')
        self.stop = cancel or threading.Event(); self.reason = None
        self.lock = threading.Lock(); self.local = threading.local()
        self.report, self.moments, self.recordings, self.done = [], [], [], 0
        self.usage = {'clips': 0, 'cached': 0, 'failed': 0, 'input_tokens': 0, 'output_tokens': 0,
                      'thinking_tokens': 0, 'usd': 0., 'details': 0}

    def halt(self, reason, message=''):
        """Stop starting new clips; clips already being judged finish and are cached."""
        if not self.stop.is_set():
            self.reason = reason; self.stop.set()
            self.emit({'type': 'stopping', 'reason': reason, 'message': message[:300]})

    def client(self):
        if not hasattr(self.local, 'client'):  # One client (and connection pool) per worker thread.
            self.local.client = gemini.client(self.api_key)
        return self.local.client

    def judge(self, path, asks):
        """One result per ask ('funny', 'triage') about a clip, from a single upload."""
        if self.judge_fn or self.skill_fn:
            return [self.judge_fn(path, self.stop) if ask == 'funny' else self.skill_fn('triage', path, self.stop, None, None, None)
                    for ask in asks]
        if asks == ['funny']:  # Exactly the v1 call, so funny judging is unchanged.
            return [gemini.judge(self.client(), self.model, path, self.stop, self.scratch, self.ffmpeg)]
        table = {'funny': gemini.FUNNY, 'triage': {'kind': 'triage', 'prompt': skill.TRIAGE_PROMPT,
                                                   'schema': skill.TRIAGE_SCHEMA, 'version': skill.TRIAGE_VERSION}}
        return gemini.judge(self.client(), self.model, path, self.stop, self.scratch, self.ffmpeg,
                            asks=[table[a] for a in asks])

    def detail(self, path, profile, play, bounds):
        """Stage B: the close-up judgment of one play, with times mapped back to the clip."""
        if self.skill_fn:
            result = self.skill_fn('detail', path, self.stop, profile, play, bounds)
        else:
            small = window(path, bounds[0], bounds[1], profile['panel'], self.scratch, self.ffmpeg, skill.WINDOW_FPS, self.stop)
            try:
                ask = {'kind': 'detail', 'prompt': skill.detail_prompt(profile), 'schema': skill.DETAIL_SCHEMA,
                       'version': skill.DETAIL_VERSION, 'fps': skill.WINDOW_FPS}
                result = gemini.upload_and_ask(self.client(), self.model, path, small, self.stop, [ask])[0]
            finally:
                small.unlink(missing_ok=True)
        out = skill.to_clip_time(result, bounds, play)
        out['window'] = list(bounds)
        return out

    def process(self, clip):
        when, hint, path = clip
        if self.stop.is_set(): return
        fresh = []   # _meta of every judgment paid for on this clip
        try:
            digest = self.hasher(path); target = self.cache/f'{digest}.json'
            funny = json.loads(target.read_text(encoding='utf-8')) if self.funny and target.exists() else None
            record = skill.load_record(self.cache, digest) if self.skilled else None
            asks = (['funny'] if self.funny and funny is None else []) + (['triage'] if self.skilled and record is None else [])
            if asks and self.cache_only:
                if funny is None and record is None:
                    return self.finish(path, 'unjudged', error='not judged yet (cache-only run)')
                asks = []   # Both mode, one lens saved: show what exists.
            for ask, result in zip(asks, self.judge(path, asks) if asks else []):
                fresh.append(result.get('_meta') or {})
                if ask == 'funny':
                    funny = result; atomic_json(target, result)
                else:
                    record = skill.new_record(result); skill.save_record(self.cache, digest, record)
            meta = probe(path, self.ffprobe)
            profile = self.profiles.for_record(record) if record else None
            if record and not self.cache_only:
                for play in skill.pending_plays(record, profile, meta['duration']):
                    if self.stop.is_set(): break   # Left pending: a later run finishes it without re-paying triage.
                    result = self.detail(path, profile, play, skill.window_bounds(play, meta['duration']))
                    fresh.append(result.get('_meta') or {})
                    record['plays'][skill.play_id(play)] = result; skill.save_record(self.cache, digest, record)
        except Cancelled:
            return self._account(fresh)
        except gemini.DailyLimit as exc:
            self._account(fresh)
            return self.halt('spend_limit' if isinstance(exc, gemini.SpendLimit) else 'daily_limit', str(exc))
        except Exception as exc:
            text = str(exc)
            if blocked(exc):
                self.halt('auth', text)  # Every clip would fail the same way.
            log.warning('%s: FAILED %s', path.name, text)
            self._account(fresh)
            return self.finish(path, 'failed', error=text[:500])
        found = list(moments_for(path, when, hint, funny, meta, digest)) if funny else []
        skilled = list(skill.moments_for(path, when, hint, record, meta, digest, profile)) if record else []
        with self.lock:
            self.recordings.append(context.recording(when, hint, path, meta['duration'], digest))
            self.usage['clips'] += 1; self.usage['cached'] += not fresh
            spent = self._spend(fresh)
            # Clips already in flight on other workers still bill: stop with room for them.
            if spent and self.budget_usd and self.usage['usd'] + self.workers*spent >= self.budget_usd:
                self.halt('budget', f"${self.usage['usd']:.2f} spent")
            entry = {'clip': path.name, **(funny or {})}
            if record:
                entry['skill'] = {'game': record['triage'].get('game'), 'summary': record['triage'].get('summary'),
                                  'stale': skill.is_stale(record), 'features': record.get('plays', {}),
                                  'plays': [{k: m[k] for k in ('start', 'end', 'score', 'skill_tags', 'skill_drawbacks', 'reason')}
                                            for m in skilled]}
            self.report.append(entry)
            self.moments.extend(found + skilled)
            minutes = self._footage() if self.target else None  # Reel mode: stop a little past the target.
            good = [m for m in found if m['score'] >= self.min_score] + [m for m in skilled if m['score'] >= self.min_skill_score]
            details = {'lenses': ['funny']*len(found) + ['skill']*len(skilled)} if self.skilled else {}
            self.finish(path, 'judged' if fresh else 'cached', scores=[m['score'] for m in found + skilled], minutes=minutes,
                        reasons=[m['reason'] for m in good][:2], locked=True, **details)

    def _spend(self, fresh):
        """Add paid judgments to usage (lock held). Returns their dollars."""
        spent = 0.
        for meta in fresh:
            spent += gemini.cost(meta)
            for key in ('input_tokens', 'output_tokens', 'thinking_tokens'):
                self.usage[key] += meta.get(key) or 0
            self.usage['details'] += meta.get('prompt') == skill.DETAIL_VERSION
        self.usage['usd'] += spent
        return spent

    def _account(self, fresh):
        """A clip that stopped part-way still bills for the judgments that finished."""
        if fresh:
            with self.lock: self._spend(fresh)

    def _footage(self):
        """Minutes of reel-worthy footage so far; halts once the target (in Both mode, each share) is met."""
        if self.mode == 'funny':  # Exactly the v1 rule.
            have = sum(m['end']-m['start'] for m in dedupe([dict(m) for m in self.moments if m['score'] >= self.min_score]))
            if have >= self.target*1.1: self.halt('target')
            return have/60
        cands = candidates(self.moments, self.min_score, self.min_skill_score)
        have = sum(m['end']-m['start'] for m in cands)
        if self.mode == 'skill':
            if have >= self.target*1.1: self.halt('target')
        else:
            by = {lens: sum(m['end']-m['start'] for m in cands if lens in m['lenses']) for lens in ('skill', 'funny')}
            if by['skill'] >= self.target*self.skill_share*1.1 and by['funny'] >= self.target*(1-self.skill_share)*1.1:
                self.halt('target')
        return have/60

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
        return {'moments': self.moments, 'report': self.report, 'usage': self.usage, 'stopped': reason,
                'recordings': self.recordings}


def candidates(moments, min_score, min_skill_score=None):
    """Reel-worthy moments with replays merged, in recording order. With min_skill_score, skill moments are
    thresholded on their own scale and merged with funny ones (one moment can carry both tags). Each then
    gets its primary `lens` and `margin` (how far it clears that lens's threshold); scores of the two
    lenses are never compared directly."""
    if min_skill_score is None:
        good = dedupe([dict(m) for m in moments if m['score'] >= min_score])
        return sorted(good, key=lambda m: (m['recorded_at'], m['start']))
    funny = dedupe([dict(m) for m in moments if m.get('lenses', ['funny']) == ['funny'] and m['score'] >= min_score])
    skilled = dedupe([dict(m) for m in moments if m.get('lenses') == ['skill'] and m['score'] >= min_skill_score])
    merged = merge_lenses(funny, skilled)
    for m in merged:
        margins = {}
        if 'funny' in m['lenses']: margins['funny'] = m.get('funny_score', m['score']) - min_score
        if 'skill' in m['lenses']: margins['skill'] = m['skill_score'] - min_skill_score
        m['lens'] = max(margins, key=lambda k: (margins[k], k == 'skill'))
        m['margin'] = margins[m['lens']]
        m['score'] = m['skill_score'] if m['lens'] == 'skill' else m.get('funny_score', m['score'])
    return sorted(merged, key=lambda m: (m['recorded_at'], m['start']))


def default_keep(cands, target_seconds=None, skill_share=None):
    """Ids kept before any review: the best `target_seconds` of footage, or everything."""
    if not target_seconds: return {m['id'] for m in cands}
    return {m['id'] for m in pick(cands, target_seconds, skill_share)[0]}


LOOKBACK_USD = .0075   # Measured: one look-back ask (~4,000 tokens in, ~1,300 out) on Flash, live.
# Held back per ask in flight, so concurrent asks cannot overshoot a cap unless one costs over twice the usual.
LOOKBACK_RESERVE = 2*LOOKBACK_USD


def lookback(cands, keep, recordings, cache, scratch, *, api_key=None, model=gemini.DEFAULT_MODEL, workers=4,
             ffmpeg='ffmpeg', emit=None, cancel=None, cache_only=False, budget_usd=None, ask_fn=None):
    """Look back before each kept funny moment for the setup it builds on (context.py) and attach what was
    found as `lead` slices. Answers are cached by the exact footage and question, so re-opening or
    re-rendering never pays twice; moments asking the identical question share one answer. budget_usd:
    dollars this pass may spend (None = no cap). Like judging's cap it is approximate: an ask starts only while
    twice the usual ask's cost still fits, so it is overshot only by answers costing more than that.
    Returns usage {'asked', 'cached', 'failed', 'usd', token counts}.
    Tests replace Gemini with ask_fn(window, moment, stop) -> result dict."""
    emit = emit or (lambda e: None); stop = cancel or threading.Event(); halted = threading.Event()
    folder = Path(cache)/'context'; folder.mkdir(parents=True, exist_ok=True)
    usage = {'asked': 0, 'cached': 0, 'failed': 0, 'usd': 0., 'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0}
    kept = sorted({m['id']: m for m in cands if m['id'] in keep}.values(), key=lambda m: (m['recorded_at'], m['start']))
    groups = {}   # cache file -> [(moment, window)], one ask each
    for i, m in enumerate(kept):
        m['lead'], m['lead_status'] = [], 'none'
        if 'funny' not in m.get('lenses', ['funny']): continue
        p = kept[i-1] if i else None
        w = context.window(m, recordings, (p['clip_wall']+p['start'], p['clip_wall']+p['end'])
                           if p and p.get('clip_wall') is not None else None)
        if w: groups.setdefault(folder/f"{context.key(w, m, model)}.json", []).append((m, w))
    local, lock, done, reserved = threading.local(), threading.Lock(), [0], [0.]

    def ask(m, w):
        if ask_fn: return ask_fn(w, m, stop)
        if not hasattr(local, 'client'): local.client = gemini.client(api_key)
        small = context.proxy(w, scratch, ffmpeg, stop)
        try:
            return gemini.upload_and_ask(local.client, model, Path(m['source_path']), small, stop, [context.ask(w, m)])[0]
        finally:
            small.unlink(missing_ok=True)

    def one(item):
        target, members = item; m, w = members[0]
        result, status = None, 'unanalyzed'
        try:
            if target.exists():
                result = json.loads(target.read_text(encoding='utf-8'))
                with lock: usage['cached'] += 1
            elif not (cache_only or stop.is_set() or halted.is_set()):
                with lock:
                    room = budget_usd is None or usage['usd']+reserved[0]+LOOKBACK_RESERVE <= budget_usd
                    if room: reserved[0] += LOOKBACK_RESERVE
                if not room: halted.set()   # The rest stay unanalyzed: nothing is asked past the cap.
                else:
                    try:
                        result = ask(m, w)
                        meta = result.get('_meta') or {}
                        with lock:   # Paid: count it before anything below can fail.
                            usage['asked'] += 1; usage['usd'] += gemini.cost(meta)
                            for k in ('input_tokens', 'output_tokens', 'thinking_tokens'): usage[k] += meta.get(k) or 0
                    finally:
                        with lock: reserved[0] -= LOOKBACK_RESERVE
                    atomic_json(target, result)
        except Cancelled:
            pass
        except gemini.DailyLimit:   # Quota or money ran out: no point asking about the rest.
            halted.set()
        except Exception as exc:  # One failed look-back only costs that moment its setup.
            log.warning('%s: look-back failed (%s)', m['clip_name'], exc)
            status = 'failed'
            if blocked(exc): halted.set()   # A rejected key or blocked project fails every ask: stop asking.
            with lock: usage['failed'] += 1
        for m, w in members:
            if result is not None:
                m['lead'] = context.slices(context.plan(result, w), w)
                m['lead_status'] = 'found' if m['lead'] else 'none'
            else: m['lead_status'] = status
        with lock:
            done[0] += 1
            emit({'type': 'context', 'done': done[0], 'total': len(groups), 'usd': round(usage['usd'], 4)})

    emit({'type': 'context_start', 'total': len(groups)})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, groups.items()))
    return usage


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
    chosen = context.resolve(chosen)   # Chronological, with each setup trimmed against what plays around it.
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
    parts = split_parts(context.resolve(moments), part_seconds); plan = part_plan(parts)
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
