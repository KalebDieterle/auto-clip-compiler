"""Look-back: find the setup a kept moment's payoff depends on (a callback, a name, the action that
caused it) in the footage just before it, including the previous saved replay when the recording runs on.

The funny judgment never changes: this is a second, narrow question about one known payoff. It may only
add excerpts BEFORE the moment's start; the moment's own start and end (and so Gemini's abrupt endings)
are never moved. Excerpts become extra source slices that play before the moment in the same event.
"""
from pathlib import Path
import uuid

from .media import audio_plan, command, mix_filter
from .store import identity

CONTEXT_VERSION = 'context-v3'
LINKED, FUNNY = 2, 2   # A beat plays first when the moment builds on it this much, or it is this funny itself.
LOOKBACK = 30.      # Seconds of footage before the moment that the look-back may see.
MAX_ADDED = 15.     # About the most seconds of setup one moment may gain (joins can add a little).
PAD = .5            # Gemini's times are ~1 s accurate (1 fps sampling): widen each excerpt a little.
JOIN = 2.           # Excerpts this close to each other, or to the moment, play straight through.
MIN_EXCERPT = 1.
SEAM = 1.           # Save names are whole seconds: neighbouring replays that meet within this are continuous.
MAX_PIECES = 3
EPS = 1e-3          # Wall times are epoch-sized floats: compare lengths with this much slack.

PROMPT = '''This video is a short stretch of a gaming session with friends. A funny moment was already chosen
for a compilation; it plays from {core_start:.1f} s to the end of this video.
What happens in it: setup: {setup} | payoff: {payoff} | reaction: {reaction}
{shown}
Watch AND listen to everything before {core_start:.1f} s. Split it into its beats (one exchange of lines,
or one action and the reaction to it); list at most 5, the ones closest to {core_start:.1f} s first, with start/end
in seconds of THIS video (each ends at or before {core_start:.1f} s). Quote what is said or describe what happens.
Rate each beat twice, as the friends who were there would:
link: how much the moment builds on this beat. 0 = unrelated. 1 = same situation, but the moment works just as
well without it. 2 = the moment plays better after it: it answers, mocks, echoes or continues it (a name or
nickname said earlier, the way someone said something, a claim that the moment proves wrong, an action that
starts what the moment shows). 3 = the moment makes little sense without it.
funny: how funny the beat is on its own, 0 = not at all, 3 = as funny as the moment.
Rate honestly: most footage before a moment is ordinary chatter and gets 0 on both.'''

SHOWN = ('{a:.1f} s to {b:.1f} s is the previous moment in the compilation. A beat between {b:.1f} s and '
         '{core_start:.1f} s that connects the two (what caused this moment, or what the previous one led to) '
         'counts as link 2 or 3.')

SCHEMA = {'type': 'object', 'properties': {
    'beats': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'start': {'type': 'number'}, 'end': {'type': 'number'}, 'evidence': {'type': 'string'},
        'link': {'type': 'integer'}, 'funny': {'type': 'integer'}, 'why': {'type': 'string'}},
        'required': ['start', 'end', 'evidence', 'link', 'funny', 'why']}},
    'note': {'type': 'string'}},
    'required': ['beats', 'note']}


def recording(when, hint, path, duration, digest):
    """A source the window can draw from; only replays named by save time have a wall clock."""
    return {'path': str(path), 'digest': digest, 'duration': duration, 'wall': when-duration if hint else None,
            'recorded_at': when}


def _own(anchor, recordings):
    """The anchor's own recording: same content AND same file (two copies of a clip can have different names)."""
    same = [r for r in recordings if r['digest'] == anchor['source'] and r['wall'] is not None]
    return (next((r for r in same if r['path'] == anchor.get('source_path')), None)
            or next((r for r in same if abs(r['wall']-anchor['clip_wall']) < EPS), None))


def window(anchor, recordings, previous=None):
    """The stretch [lookback .. moment end] as pieces of real recordings, played back to back.
    Window times are positions in that playback (a small gap between replays is skipped, not kept).
    anchor: a moment (clip_wall, start, end, source, source_path). previous: the wall span the reel plays
    just before it. Returns None when the moment has no wall clock or nothing before it was recorded."""
    if anchor.get('clip_wall') is None: return None
    core = (anchor['clip_wall']+anchor['start'], anchor['clip_wall']+anchor['end'])
    lo = core[0]-LOOKBACK
    own = _own(anchor, recordings)
    if own is None: return None
    pieces = [(own, max(lo, own['wall']), core[1])]
    cursor = pieces[0][1]
    while cursor > lo+.5 and len(pieces) < MAX_PIECES:
        used = [p[0] for p in pieces]
        reach = [r for r in recordings if r['wall'] is not None and all(r is not u for u in used)
                 and r['wall'] < cursor-.5 and r['wall']+r['duration'] >= cursor-SEAM]
        if not reach: break
        r = min(reach, key=lambda r: (r['wall'], r['path']))   # The one that reaches furthest back.
        start = max(lo, r['wall'])
        pieces.insert(0, (r, start, min(cursor, r['wall']+r['duration']))); cursor = start
    at, out = 0., []
    for r, a, b in pieces:
        out.append({'path': r['path'], 'digest': r['digest'], 'duration': r['duration'], 'clip_wall': r['wall'],
                    'start': a-r['wall'], 'end': b-r['wall'], 'at': at, 'wall_start': a, 'recorded_at': r['recorded_at']})
        at += b-a
    def place(t):   # Wall time -> window time; inside a skipped gap, the next piece's start.
        for p in out:
            if t <= p['wall_start']: return p['at']
            if t <= p['wall_start']+p['end']-p['start']: return p['at']+t-p['wall_start']
        return at
    start = place(core[0])
    if start < 1: return None   # Nothing before the moment to look at.
    shown = None
    if previous and previous[1] > out[0]['wall_start']+.5:
        shown = (place(previous[0]), min(start, place(previous[1])))
        if shown[1]-shown[0] < .5: shown = None
    return {'core': (start, at), 'pieces': out, 'shown': shown}


def prompt(window, anchor):
    core_start = window['core'][0]
    shown = SHOWN.format(a=window['shown'][0], b=window['shown'][1], core_start=core_start) if window['shown'] else ''
    return PROMPT.format(core_start=core_start, shown=shown, setup=anchor.get('setup') or '?',
                         payoff=anchor.get('payoff') or anchor.get('reason') or '?', reaction=anchor.get('reaction') or '?')


def key(window, anchor, model):
    """Cache identity: the exact footage asked about and the exact question asked."""
    return identity([CONTEXT_VERSION, model, [(p['digest'], round(p['start'], 3), round(p['end'], 3)) for p in window['pieces']],
                     prompt(window, anchor)])


def proxy(window, directory, ffmpeg='ffmpeg', cancel=None):
    """One small 480p/10 fps video of the window's pieces back to back: its timeline is the window's."""
    target = Path(directory)/f'{uuid.uuid4().hex[:16]}.mp4'   # Unique: workers never share a file.
    args, graph = [ffmpeg, '-v', 'error', '-y'], []
    for n, p in enumerate(window['pieces']):
        d = p['end']-p['start']; fit = f'apad,atrim=duration={d:.6f}'
        args += ['-ss', f"{p['start']:.3f}", '-t', f'{d:.3f}', '-i', p['path']]
        _, plan = audio_plan(p['path'], ffmpeg, cancel=cancel)
        graph.append(f'[{n}:v:0]scale=-2:480,setsar=1,fps=10[v{n}]')
        if not plan: graph.append(f'anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={d:.6f}[a{n}]')
        elif len(plan) == 1: graph.append(f'[{n}:{plan[0]}]aresample=48000,aformat=channel_layouts=stereo,{fit}[a{n}]')
        else: graph.append(mix_filter(plan, fit, label=f'a{n}', src=n))
    k = len(window['pieces'])
    graph.append(''.join(f'[v{n}][a{n}]' for n in range(k)) + f'concat=n={k}:v=1:a=1[v][a]')
    command(args + ['-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-crf', '30', '-c:a', 'aac', '-b:a', '96k', '-ac', '2', target],
            timeout=600, cancel=cancel)
    return target


def ask(window, anchor):
    return {'kind': 'context', 'prompt': prompt(window, anchor), 'schema': SCHEMA, 'version': CONTEXT_VERSION}


def _union(spans):
    """Overlapping [a, b, evidence] spans merged, sorted by start. Spans that only touch stay apart, so the cap
    in resolve can still take one of them."""
    out = []
    for a, b, ev in sorted(spans, key=lambda s: s[0]):
        if out and a < out[-1][1]: out[-1][1] = max(out[-1][1], b); out[-1][2] = out[-1][2]+ev
        else: out.append([a, b, list(ev)])
    return out


def plan(result, window):
    """Every setup excerpt Gemini's answer supports, in window time: beats rated linked or funny, widened by PAD,
    inside [0, moment start), overlapping ones merged. Which of them play (the cap, joins, nothing twice) is
    decided by resolve against what the reel really plays, so a beat inside the previous moment is still kept
    here in case Review cuts that moment."""
    core = window['core'][0]
    spans = []
    for e in (result or {}).get('beats') or []:
        try:
            if int(e.get('link') or 0) < LINKED and int(e.get('funny') or 0) < FUNNY: continue
            a, b = max(0., float(e['start'])-PAD), min(core, float(e['end'])+PAD)
        except (KeyError, TypeError, ValueError): continue
        if b-a >= MIN_EXCERPT-EPS: spans.append([a, b, [e]])
    return [{'start': round(a, 3), 'end': round(b, 3), 'evidence': [x.get('evidence', '') for x in ev]} for a, b, ev in _union(spans)]


def slices(spans, window):
    """Window-time spans as source slices; a span crossing two recordings becomes two slices that share
    its `excerpt` number (so a short setup split at a seam is judged by its whole length)."""
    out = []
    for n, s in enumerate(spans):
        for p in window['pieces']:
            a, b = max(s['start'], p['at']), min(s['end'], p['at']+p['end']-p['start'])
            if b-a < .05: continue
            out.append({'source_path': p['path'], 'source': p['digest'], 'source_hash': p['digest'],
                        'start': round(p['start']+a-p['at'], 3), 'end': round(p['start']+b-p['at'], 3),
                        'duration': p['duration'], 'clip_wall': p['clip_wall'], 'recorded_at': p['recorded_at'],
                        'evidence': s['evidence'], 'excerpt': n})
    return out


def _wall(s):
    return (s['clip_wall']+s['start'], s['clip_wall']+s['end']) if s.get('clip_wall') is not None else None


def _cut(s, span):
    """s without the part that falls inside the wall span: [], [s], or the pieces left either side."""
    w = _wall(s)
    if not w or w[1] <= span[0] or w[0] >= span[1]: return [s]
    out = []
    if w[0] < span[0]: out.append(dict(s, end=round(s['start']+span[0]-w[0], 3)))
    if w[1] > span[1]: out.append(dict(s, start=round(s['end']-(w[1]-span[1]), 3)))
    return out


def cut_all(s, spans):
    """s without any of the wall spans."""
    left = [s]
    for span in spans: left = [q for p in left for q in _cut(p, span)]
    return left


def _clear(a, b, played, clip_wall):
    """True when footage [a, b] of a recording shows nothing the reel already played (a gap safe to fill)."""
    if clip_wall is None: return True
    return all(w[1] <= clip_wall+a+EPS or w[0] >= clip_wall+b-EPS for w in played)


def resolve(kept):
    """The setup each kept moment plays first, as decided now (after Review). In recording order, each
    moment's `lead` slices are trimmed so nothing plays twice: never into the moment itself or into the moment
    the reel plays just before it. A setup too short to follow once trimmed is dropped; the setups nearest the
    moment are kept while they fit MAX_ADDED (one that cannot fit is skipped, the others still count); setups
    within JOIN of each other in one recording play straight through, and one that ends within JOIN of the
    moment in its recording moves the moment's start instead, so that cut has no seam. Returns new dicts."""
    out, previous = [], []   # previous: wall spans of the moment the reel shows just before this one.
    for m in sorted(kept, key=lambda m: (m['recorded_at'], m['start'])):
        m = dict(m); pieces = []
        core = _wall(m)
        played = previous + [tuple(w) for w in m.get('avoid') or []]   # avoid: footage an earlier compilation played.
        for s in m.get('lead') or []:
            left = cut_all(s, played + ([core] if core else []))
            if s['source'] == m['source']:
                left = [dict(p, end=min(p['end'], m['start'])) for p in left]
            pieces += [p for p in left if p['end']-p['start'] >= .05]
        excerpts = {}   # Pieces of one excerpt (split at a seam or by trimming) count together.
        for p in pieces: excerpts.setdefault(p.get('excerpt'), []).append(p)
        near = lambda ps: max((_wall(p) or (0., p['end']))[1] for p in ps)
        lead, total = [], 0.
        for ps in sorted(excerpts.values(), key=near, reverse=True):
            size = sum(p['end']-p['start'] for p in ps)
            if size < MIN_EXCERPT-EPS or total+size > MAX_ADDED+EPS: continue
            lead += ps; total += size
        lead.sort(key=lambda s: _wall(s) or (0., s['start']))
        joined = []
        for s in lead:
            p = joined[-1] if joined else None
            if p and p['source'] == s['source'] and 0 <= s['start']-p['end'] <= JOIN \
                    and _clear(p['end'], s['start'], played, s.get('clip_wall')):
                joined[-1] = dict(p, end=s['end'])
            else: joined.append(dict(s))
        last = joined[-1] if joined else None
        if last and last['source'] == m['source'] and 0 <= m['start']-last['end'] <= JOIN \
                and _clear(last['end'], m['start'], played, m.get('clip_wall')):
            m['start'] = joined.pop()['start']
        m['lead'] = joined
        out.append(m)
        previous = [w for w in [_wall(s) for s in joined] + [_wall(m)] if w]
    return out
