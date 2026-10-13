"""Funny-moment compilations judged by Gemini (video + audio), newest clips first.

A 480p proxy of each clip is uploaded to the Gemini Files API and deleted after judging; judgments
are cached by content hash, so re-runs never re-upload or re-pay. Requires GEMINI_API_KEY.

  One reel:  python tools/gemini_reel.py --source <clips> --output <dir> --target-minutes 30
  Library:   python tools/gemini_reel.py --source <clips> --output <dir> --parts-minutes 35 --max-clips 5000 --budget-usd 9
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
from clipcompiler.media import VIDEO_EXTS, probe, recording_time  # noqa: E402
from clipcompiler.render import publish  # noqa: E402
from clipcompiler.store import atomic_json, awake, file_hash, identity  # noqa: E402
from clipcompiler.media import command  # noqa: E402
from gemini_test import PROMPT as BASE_PROMPT, SCHEMA  # noqa: E402

PROMPT_VERSION = 'reel-v2'
PROMPT = BASE_PROMPT + '''
Only humor counts. Skill highlights (good plays, clutch fights, nice shots, big kills) are NOT wanted
unless something genuinely funny happens in them: score pure skill moments 4 or lower.'''
# Gemini 3.8 Flash paid tier, USD per 1M tokens, through 2026-12-31 (doubles 2027-01-01).
PRICE_IN, PRICE_OUT = 0.75, 3.75


def cost(meta):
    """Billed dollars for one judgment; thinking tokens bill at the output rate."""
    billed_out = (meta.get('output_tokens') or 0) + (meta.get('thinking_tokens') or 0)
    return ((meta.get('input_tokens') or 0)*PRICE_IN + billed_out*PRICE_OUT) / 1e6


class DailyLimit(Exception):
    """Quota exhausted for the day: stop judging, render what exists."""


def proxy(path, directory):
    """Small 480p/10fps copy for upload; same timeline, so moment times map 1:1 to the original."""
    target = Path(directory)/(identity([str(path)])[:16]+'.mp4')
    # OBS records AV1; hardware decode is ~3x faster than software (ffmpeg falls back if unavailable).
    command(['ffmpeg', '-v', 'error', '-y', '-hwaccel', 'd3d11va', '-i', path, '-map', '0:v:0', '-map', '0:a:0?',
             '-vf', 'scale=-2:480,fps=10', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30',
             '-c:a', 'aac', '-b:a', '96k', '-ac', '2', target])
    return target


def judge(client, model, path, stop, scratch):
    from google.genai import errors
    small = proxy(path, scratch)
    try:
        return upload_and_judge(client, model, path, small, stop, errors)
    finally:
        small.unlink(missing_ok=True)


def upload_and_judge(client, model, path, small, stop, errors):
    for attempt in range(4):
        try:
            uploaded = client.files.upload(file=str(small)); break
        except Exception as exc:  # SSL resets, timeouts, 5xx during upload (httpx wraps some of them).
            if attempt == 3 or isinstance(exc, errors.ClientError): raise
            print(f'    {path.name}: upload error ({exc}); retrying', flush=True); time.sleep(10*(attempt+1))
    try:
        while uploaded.state.name == 'PROCESSING':
            time.sleep(2); uploaded = client.files.get(name=uploaded.name)
        if uploaded.state.name != 'ACTIVE':
            raise RuntimeError(f'Upload failed: {uploaded.state}')
        started = time.monotonic()
        for attempt in range(7):
            if stop.is_set(): raise DailyLimit('stopped')
            try:
                response = client.models.generate_content(model=model, contents=[uploaded, PROMPT],
                    config={'response_mime_type': 'application/json', 'response_schema': SCHEMA, 'temperature': 0})
                break
            except (errors.ServerError, errors.ClientError) as exc:
                text = str(exc)
                if exc.code == 429 and ('per day' in text.lower() or 'PerDay' in text):
                    raise DailyLimit(text) from exc
                if exc.code not in (429, 500, 503) or attempt == 6: raise
                wait = min(240, 15 * 2**attempt)
                print(f'    {path.name}: Gemini {exc.code}; retrying in {wait}s', flush=True); time.sleep(wait)
        result = json.loads(response.text)
        usage = response.usage_metadata
        result['_meta'] = {'model': model, 'prompt': PROMPT_VERSION, 'seconds': round(time.monotonic()-started, 1),
                           'input_tokens': usage.prompt_token_count, 'output_tokens': usage.candidates_token_count,
                           'thinking_tokens': usage.thoughts_token_count}
        return result
    finally:
        try: client.files.delete(name=uploaded.name)  # Never leave clips on Google's side.
        except Exception as exc: print(f'    WARNING could not delete upload {uploaded.name}: {exc}', flush=True)


def discover(source, since=None, until=None):
    clips = []
    for path in Path(source).rglob('*'):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
            when, hint = recording_time(path)
            if (since and when < since) or (until and when >= until): continue
            clips.append((when, hint, path))
    return sorted(clips, key=lambda c: c[0], reverse=True)


def moments_for(path, when, hint, result, meta, digest):
    # OBS names a replay when it is SAVED, so the clip spans [save - duration, save] in wall time.
    clip_start = when - meta['duration'] if hint else None
    for m in result.get('moments', []):
        start, end = max(0., float(m['start'])), min(meta['duration'], float(m['end']))
        if end - start < .5: continue
        yield dict(id=identity([digest, start, end]), source=digest, source_hash=digest, source_path=str(path),
                   start=start, end=end, duration=meta['duration'], audio_stream=meta['audio_stream'],
                   recorded_at=when, score=int(m['score']), reason=m['why_funny'], transcript='',
                   recommendation='keep', clip_wall=clip_start, clip_name=path.name)


def wall(m):
    return (m['clip_wall']+m['start'], m['clip_wall']+m['end']) if m['clip_wall'] is not None else None


def dedupe(moments):
    """Best first; a moment overlapping an accepted one in wall time is merged, trimmed or dropped."""
    accepted = []
    for m in sorted(moments, key=lambda m: (-m['score'], m['id'])):
        span = wall(m); keep = True
        for a in accepted:
            other = wall(a)
            if span is None or other is None: continue
            overlap = min(span[1], other[1]) - max(span[0], other[0])
            if overlap <= .5: continue
            union = (min(span[0], other[0]), max(span[1], other[1]))
            clip_a = (a['clip_wall'], a['clip_wall']+a['duration'])
            if clip_a[0] <= union[0] and union[1] <= clip_a[1]:
                a['start'], a['end'] = union[0]-a['clip_wall'], union[1]-a['clip_wall']  # Same event, one recording.
                keep = False; break
            if m['clip_wall'] <= union[0] and union[1] <= m['clip_wall']+m['duration']:
                # Only m's recording holds the whole event: play it from there, keep the better score.
                score = a['score']; a.clear(); a.update(m, score=score)
                a['start'], a['end'] = union[0]-m['clip_wall'], union[1]-m['clip_wall']
                keep = False; break
            if overlap >= .5*(span[1]-span[0]):
                keep = False; break
            # Keep only the part of m the accepted moment does not already show.
            if span[0] < other[0]: m['end'] = other[0]-m['clip_wall']
            else: m['start'] = other[1]-m['clip_wall']
            span = wall(m)
        if keep and m['end']-m['start'] >= 2: accepted.append(m)
    return accepted


def pick(moments, target):
    chosen, total = [], 0.
    for m in sorted(moments, key=lambda m: (-m['score'], m['recorded_at'])):
        if total >= target: break
        chosen.append(m); total += m['end']-m['start']
    return sorted(chosen, key=lambda m: (m['recorded_at'], m['start'])), total


def split_parts(moments, part_seconds, session_gap=1800):
    """Chronological parts of about part_seconds; once a part is 80% full, prefer to end it between sessions."""
    parts, total, previous = [[]], 0., None
    for m in sorted(moments, key=lambda m: (m['recorded_at'], m['start'])):
        gap = m['recorded_at']-previous['recorded_at'] if previous else 0
        if parts[-1] and (total >= part_seconds or (total >= .8*part_seconds and gap > session_gap)):
            parts.append([]); total = 0.
        parts[-1].append(m); total += m['end']-m['start']; previous = m
    # A short leftover tail joins the previous part rather than standing alone.
    if len(parts) > 1 and sum(m['end']-m['start'] for m in parts[-1]) < .3*part_seconds:
        parts[-2].extend(parts.pop())
    return [p for p in parts if p]


def render_parts(moments, out, part_seconds, report, usage, no_render, only=None):
    parts = split_parts(moments, part_seconds)
    day = lambda t: datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d')
    plan = [{'part': i, 'from': day(p[0]['recorded_at']), 'to': day(p[-1]['recorded_at']), 'moments': len(p),
             'minutes': round(sum(m['end']-m['start'] for m in p)/60, 1)} for i, p in enumerate(parts, 1)]
    atomic_json(out/'report.json', report); atomic_json(out/'summary.json', dict(usage, parts=plan))
    print(json.dumps(dict(usage, parts=len(parts))), flush=True)
    for row in plan: print(f"Part {row['part']:02}: {row['from']} to {row['to']}, {row['moments']} moments, {row['minutes']} min", flush=True)
    if no_render: return
    for row, part in zip(plan, parts):
        if only and row['part'] not in only: continue
        name = f"Part {row['part']:02} ({row['from']} to {row['to']}).mp4"
        if (out/name).exists():
            print(f'Skip existing {name}', flush=True); continue
        final = publish(part, out/f"part-{row['part']:02}", {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe', 'fps': 60,
            'render_height': 1080, 'workers': 4, 'resolve': True, 'render': True, 'min_free_gb': 5, 'loudnorm': True})
        os.replace(final/'Combined_highlights.mp4', out/name)  # FCPXML/manifest stay in the render folder.
        print(f'Published {out/name}', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True); p.add_argument('--output', required=True)
    p.add_argument('--target-minutes', type=float, default=30); p.add_argument('--min-score', type=int, default=7)
    p.add_argument('--workers', type=int, default=4); p.add_argument('--max-clips', type=int, default=400)
    p.add_argument('--since', help='Only clips recorded on/after YYYY-MM-DD[THH:MM:SS]')
    p.add_argument('--until', help='Only clips recorded before YYYY-MM-DD[THH:MM:SS]')
    p.add_argument('--cache-only', action='store_true', help='Never call Gemini; use saved (e.g. batch) judgments only')
    p.add_argument('--cache', help='Judgment cache dir (default <output>/../gemini-cache)')
    p.add_argument('--exclude', action='append', default=[]); p.add_argument('--model', default='gemini-flash-latest')
    p.add_argument('--no-render', action='store_true')
    p.add_argument('--budget-usd', type=float, help='Stop judging before new judgments cost more than this')
    p.add_argument('--parts-minutes', type=float,
                   help='Judge every clip (budget permitting) and split moments into chronological videos of about this length')
    p.add_argument('--only-parts', type=int, nargs='+', help='With --parts-minutes: render just these part numbers')
    args = p.parse_args()
    target = args.target_minutes*60
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    scratch = out/'.upload-proxies'; scratch.mkdir(exist_ok=True)
    cache = Path(args.cache) if args.cache else out.parent/'gemini-cache'; cache.mkdir(parents=True, exist_ok=True)
    since = datetime.datetime.fromisoformat(args.since).timestamp() if args.since else None
    until = datetime.datetime.fromisoformat(args.until).timestamp() if args.until else None
    clips = [c for c in discover(args.source, since, until) if c[2].name not in args.exclude][:args.max_clips]
    print(f'{len(clips)} candidate clips, newest first; target {args.target_minutes:g} min at score >= {args.min_score}', flush=True)

    from google import genai
    local = threading.local()  # One client (and connection pool) per worker thread.
    def client():
        if not hasattr(local, 'client'): local.client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
        return local.client
    stop = threading.Event(); lock = threading.Lock()
    report, moments, usage = [], [], {'clips': 0, 'cached': 0, 'input_tokens': 0, 'output_tokens': 0,
                                      'thinking_tokens': 0, 'usd': 0.}

    def process(clip):
        when, hint, path = clip
        if stop.is_set(): return
        try:
            digest = file_hash(path); target_file = cache/f'{digest}.json'
            if target_file.exists():
                result = json.loads(target_file.read_text(encoding='utf-8')); fresh = False
            elif args.cache_only:
                with lock: report.append({'clip': path.name, 'error': 'not judged (cache-only run)'})
                return
            else:
                result = judge(client(), args.model, path, stop, scratch); atomic_json(target_file, result); fresh = True
            meta = probe(path)
        except DailyLimit as exc:
            if not stop.is_set(): print(f'Daily Gemini quota reached; rendering what exists. ({str(exc)[:160]})', flush=True)
            stop.set(); return
        except Exception as exc:
            print(f'  {path.name}: FAILED {exc}', flush=True)
            with lock: report.append({'clip': path.name, 'error': str(exc)})
            return
        found = list(moments_for(path, when, hint, result, meta, digest))
        with lock:
            usage['clips'] += 1; usage['cached'] += not fresh
            if fresh:
                usage['input_tokens'] += result['_meta'].get('input_tokens') or 0
                usage['output_tokens'] += result['_meta'].get('output_tokens') or 0
                usage['thinking_tokens'] += result['_meta'].get('thinking_tokens') or 0
                usage['usd'] += cost(result['_meta'])
                # Clips already in flight on other workers still bill: stop with room for them.
                if args.budget_usd and usage['usd'] + args.workers*cost(result['_meta']) >= args.budget_usd:
                    if not stop.is_set(): print(f'Budget reached (${usage["usd"]:.2f} spent); rendering what exists.', flush=True)
                    stop.set()
            report.append({'clip': path.name, **result})
            moments.extend(found)
            good = dedupe([dict(m) for m in moments if m['score'] >= args.min_score])
            have = sum(m['end']-m['start'] for m in good)
            print(f'  [{usage["clips"]}] {path.name}: {len(found)} moments '
                  f'({", ".join(str(m["score"]) for m in found) or "none"}); {have/60:.1f}/{args.target_minutes:g} min; ${usage["usd"]:.3f}', flush=True)
            if not args.parts_minutes and have >= target*1.1: stop.set()  # A little extra so ranking has choices.

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(process, clips))
    good = dedupe([dict(m) for m in moments if m['score'] >= args.min_score])
    if args.parts_minutes:
        return render_parts(good, out, args.parts_minutes*60, report, dict(usage, judged_seconds=round(time.monotonic()-started)),
                            args.no_render, set(args.only_parts or []))
    chosen, total = pick(good, target)
    borderline = [m for m in chosen if m['score'] == args.min_score]
    summary = dict(usage, judged_seconds=round(time.monotonic()-started), moments_found=len(moments),
                   moments_after_dedupe=len(good), chosen=len(chosen), reel_minutes=round(total/60, 2),
                   borderline=[f"{m['clip_name']} {m['start']:.0f}-{m['end']:.0f}s: {m['reason']}" for m in borderline])
    atomic_json(out/'report.json', report); atomic_json(out/'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'borderline'}), flush=True)
    if total < target*.9:
        print(f'NOTE: only {total/60:.1f} min of moments scored >= {args.min_score} in the clips judged.', flush=True)
    if chosen and not args.no_render:
        final = publish(chosen, out, {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe', 'fps': 60, 'render_height': 1080,
                                      'workers': 4, 'resolve': True, 'render': True, 'min_free_gb': 5, 'loudnorm': True})
        print(f'Published {final}', flush=True)


if __name__ == '__main__':
    with awake():  # Hold off system sleep only while this job runs.
        main()
