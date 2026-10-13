"""Turn cached judgments into a reel: clip discovery, replay dedupe, picking and splitting into parts."""
import datetime
from pathlib import Path

from .media import VIDEO_EXTS, recording_time
from .store import identity


def discover(source, since=None, until=None):
    """(recorded_at, time_from_filename, path) for every video under source, newest first."""
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
                   recommendation='keep', clip_wall=clip_start, clip_name=path.name, lenses=['funny'],
                   funny_score=int(m['score']))


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


def overlap(a, b):
    """Seconds two moments share: in the same recording, else in wall time; <= 0 when unrelated."""
    if a.get('source') and a.get('source') == b.get('source'):
        return min(a['end'], b['end']) - max(a['start'], b['start'])
    wa, wb = wall(a), wall(b)
    if wa is None or wb is None: return 0.
    return min(wa[1], wb[1]) - max(wa[0], wb[0])


def absorb(f, s):
    """Fold skill moment s into funny moment f: one moment, both tags, both scores."""
    if f.get('source') and f.get('source') == s.get('source'):
        f['start'], f['end'] = min(f['start'], s['start']), max(f['end'], s['end'])
    elif wall(f) and wall(s):
        union = (min(wall(f)[0], wall(s)[0]), max(wall(f)[1], wall(s)[1]))
        if f['clip_wall'] <= union[0] and union[1] <= f['clip_wall']+f['duration']:
            f['start'], f['end'] = union[0]-f['clip_wall'], union[1]-f['clip_wall']
    f['lenses'] = sorted(set(f.get('lenses', ['funny'])) | {'skill'})
    for key in ('skill_score', 'skill_tags', 'skill_drawbacks', 'game', 'kills'):
        if key in s: f[key] = s[key]
    f['skill_reason'] = s['reason']


def merge_lenses(funny, skilled):
    """A play that is both funny and skillful is shown once with both tags. A skill moment that only
    partly repeats a funny one keeps just its new footage, like replay dedupe."""
    out = [dict(m) for m in funny]
    for s in sorted(skilled, key=lambda m: (-m['score'], m['id'])):
        s = dict(s); keep = True
        for f in out:
            if 'funny' not in f.get('lenses', ['funny']): continue
            shared = overlap(s, f)
            if shared <= .5: continue
            if shared >= .5*min(s['end']-s['start'], f['end']-f['start']):
                absorb(f, s); keep = False; break
            same = f.get('source') and f.get('source') == s.get('source')
            fs, fe = (f['start'], f['end']) if same else (wall(f)[0]-s['clip_wall'], wall(f)[1]-s['clip_wall'])
            if s['start'] < fs: s['end'] = fs
            else: s['start'] = fe
        if keep and s['end']-s['start'] >= 2: out.append(s)
    return out


def pick(moments, target, skill_share=None):
    """Best first until target seconds, then chronological. With skill_share (Both mode), skill and funny
    moments each fill their own share by their own score; leftover time goes to whatever clears its
    threshold by the most."""
    if skill_share is None:
        chosen, total = [], 0.
        for m in sorted(moments, key=lambda m: (-m['score'], m['recorded_at'])):
            if total >= target: break
            chosen.append(m); total += m['end']-m['start']
        return sorted(chosen, key=lambda m: (m['recorded_at'], m['start'])), total
    quota = {'skill': target*skill_share, 'funny': target*(1-skill_share)}
    chosen, used = [], {'skill': 0., 'funny': 0.}
    for lens in ('skill', 'funny'):
        for m in sorted((m for m in moments if m.get('lens', 'funny') == lens), key=lambda m: (-m['score'], m['recorded_at'])):
            if used[lens] >= quota[lens]: break
            chosen.append(m); used[lens] += m['end']-m['start']
    total = sum(used.values()); taken = {m['id'] for m in chosen}
    for m in sorted((m for m in moments if m['id'] not in taken), key=lambda m: (-m.get('margin', 0), m['recorded_at'])):
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


def day(t):
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d')


def part_plan(parts):
    return [{'part': i, 'from': day(p[0]['recorded_at']), 'to': day(p[-1]['recorded_at']), 'moments': len(p),
             'minutes': round(sum(m['end']-m['start'] for m in p)/60, 1)} for i, p in enumerate(parts, 1)]


def part_name(row):
    return f"Part {row['part']:02} ({row['from']} to {row['to']}).mp4"
