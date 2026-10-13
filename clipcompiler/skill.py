"""Skill judging: Gemini reports what happened in a play, this module turns that into a score.

Two stages per clip:
  triage  the whole 480p proxy at Gemini's default sampling: which game, and where the candidate plays are
  detail  a short window around each candidate, cut from the original and judged at a high frame rate:
          per-kill facts (headshot, flick, reaction, shots...), the situation (1vX, low HP) and red flags

The model only reports facts in fixed buckets; score() is a pure function of those facts and a game
profile (skill_profiles/*.json). Scores are never cached, so changing a preset or a profile re-scores
every cached play for free.
"""
import json
import math
from pathlib import Path

from .store import atomic_json, identity

RECORD_VERSION = 'skill-v1'
TRIAGE_VERSION = 'skill-a-v1'
DETAIL_VERSION = 'skill-b-v1'
GAMES = ('cs2', 'rust', 'other')
PRESETS = ('balanced', 'aim', 'clutch')
PRESET_WEIGHTS = {'aim': {'mechanics': .8, 'situation': .2, 'multi': .5},
                  'clutch': {'mechanics': .4, 'situation': .6, 'multi': 1.0}}
PROFILE_DIR = Path(__file__).resolve().parent/'skill_profiles'
SPECIALS = ('wallbang', 'through_smoke', 'noscope', 'airborne', 'blind', 'collateral', 'melee')
PENALTIES = ('enemy_unaware', 'enemy_pre_damaged', 'long_spray_many_misses', 'npc_or_bot', 'exit_frag',
             'teammate_did_work')
# Stage-B window: seconds of lead-in and follow-through around a play, and the longest window sent.
PAD_BEFORE, PAD_AFTER, MAX_WINDOW = 2., 1., 8.
WINDOW_FPS = 12          # Frames per second Gemini samples from the window (tuned in the pilot).
# Typical tokens, for cost estimates until the pilot measures real ones.
TYPICAL_TRIAGE = {'input_tokens': 2850, 'output_tokens': 250, 'thinking_tokens': 450}
TYPICAL_DETAIL = {'input_tokens': 19000, 'output_tokens': 450, 'thinking_tokens': 600}
PLAYS_PER_CLIP = .4      # Average stage-B windows per clip (guess until measured).

TRIAGE_PROMPT = '''You are scouting gaming clips for a SKILL highlight compilation.
Watch AND listen to this whole clip. First say which game it is.
Then list every play that shows real skill or could: kills, fights, clutches, outplays, impressive aim or movement.
For each play give start/end in seconds covering the whole fight (from first contact to just after the last kill),
how many kills the recording player got, and a preliminary 1-10 for how impressive it looks.
Skill is NOT kill count. A fast flick headshot beats a sloppy spray-down of two players. Kills on unaware,
already-damaged or AI enemies are easy. Winning while outnumbered or low on health is hard.
If there is no fighting at all, return an empty list and say why in summary.'''

TRIAGE_SCHEMA = {'type': 'object', 'properties': {
    'game': {'type': 'string', 'enum': list(GAMES)},
    'summary': {'type': 'string'},
    'plays': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'start': {'type': 'number'}, 'end': {'type': 'number'}, 'kills': {'type': 'integer'},
        'preliminary': {'type': 'integer'}, 'what': {'type': 'string'}},
        'required': ['start', 'end', 'kills', 'preliminary', 'what']}}},
    'required': ['game', 'summary', 'plays']}

DETAIL_PROMPT = '''You are judging ONE play from a gaming clip for a SKILL highlight compilation. Report facts, not opinions.
This short video is a layout of three views of the same moment:
  LEFT: the CENTER of the screen, zoomed in (the crosshair is in the middle). Use it to judge aim: how far the
        crosshair travelled to the target (flick), how fast the shot came after the enemy appeared (reaction),
        and whether it landed on the head.
  TOP-RIGHT: {panel}.
  BOTTOM-RIGHT: the whole screen.
Times are seconds from the start of THIS short video.
For every kill by the recording player give its time and the facts. Use "unclear" or false rather than guessing.
  flick: how far the crosshair moved onto the target right before the kill (none = already on target).
  reaction: fast = shot almost as soon as the enemy was visible.
  shots: one_tap = a single shot or tap; spray = held fire with several misses.
Situation at the start of the play: teammates_alive (not counting the player) and enemies_alive.
penalty_flags: anything that made it easy (enemy unaware or facing away, enemy already hurt by someone else,
long spray with many misses, an AI/NPC/bot/animal, a kill after the round was decided, a teammate did most of the work).
evidence: what you actually saw or heard that supports the headshot/kill facts (kill feed icon, hit sound, crosshair).
holistic_difficulty: 1-10, how hard this play would look to an experienced player.
start/end: the whole play including a beat after the last kill.
{hints}'''

ENUM = lambda *values: {'type': 'string', 'enum': list(values)}
DETAIL_SCHEMA = {'type': 'object', 'properties': {
    'start': {'type': 'number'}, 'end': {'type': 'number'},
    'kills': {'type': 'array', 'items': {'type': 'object', 'properties': {
        't': {'type': 'number'},
        'headshot': ENUM('yes', 'no', 'unclear'),
        'flick': ENUM('none', 'small', 'medium', 'large'),
        'reaction': ENUM('fast', 'normal', 'slow', 'unclear'),
        'range': ENUM('close', 'mid', 'long'),
        'target_moving': {'type': 'boolean'},
        'shots': ENUM('one_tap', 'burst', 'spray'),
        'special': {'type': 'array', 'items': ENUM(*SPECIALS)}},
        'required': ['t', 'headshot', 'flick', 'reaction', 'range', 'target_moving', 'shots', 'special']}},
    'situation': {'type': 'object', 'properties': {
        'teammates_alive': {'type': 'integer'}, 'enemies_alive': {'type': 'integer'},
        'low_hp': {'type': 'boolean'}, 'round_decisive': {'type': 'boolean'},
        'outnumbered': {'type': 'boolean'}, 'enemies_aware': {'type': 'boolean'}},
        'required': ['teammates_alive', 'enemies_alive', 'low_hp', 'round_decisive', 'outnumbered', 'enemies_aware']},
    'penalty_flags': {'type': 'array', 'items': ENUM(*PENALTIES)},
    'evidence': {'type': 'string'},
    'holistic_difficulty': {'type': 'integer'},
    'one_liner': {'type': 'string'}},
    'required': ['start', 'end', 'kills', 'situation', 'penalty_flags', 'evidence', 'holistic_difficulty', 'one_liner']}


# --- profiles ------------------------------------------------------------------------------------

def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def profile_name(game, override='auto'):
    if override and override != 'auto': return override
    return game if game in ('cs2', 'rust') else 'generic'


def load_profile(name, preset='balanced', user_dir=None):
    """A game profile: the built-in JSON, overlaid by the user's copy (if any), then the preset's weights."""
    name = name if (PROFILE_DIR/f'{name}.json').exists() else 'generic'
    profile = json.loads((PROFILE_DIR/f'{name}.json').read_text(encoding='utf-8'))
    if user_dir and (Path(user_dir)/f'{name}.json').exists():
        try:
            profile = _merge(profile, json.loads((Path(user_dir)/f'{name}.json').read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass  # A broken user copy never blocks judging; the built-in profile is used.
    if preset in PRESET_WEIGHTS:
        profile = dict(profile, weights=dict(PRESET_WEIGHTS[preset]))
    return profile


class Profiles:
    """Resolves the scoring profile for each clip from its detected game, a manual override and a preset."""

    def __init__(self, preset='balanced', override='auto', user_dir=None):
        self.preset, self.override, self.user_dir, self._cache = preset, override, user_dir, {}

    def for_game(self, game):
        name = profile_name(game, self.override)
        if name not in self._cache: self._cache[name] = load_profile(name, self.preset, self.user_dir)
        return self._cache[name]

    def for_record(self, record):
        return self.for_game((record or {}).get('triage', {}).get('game'))


# --- scoring (pure) ------------------------------------------------------------------------------

def _clamp(x, lo=0., hi=1.):
    return max(lo, min(hi, x))


def _pick(table, value):
    """Bucket weight; an unknown or missing bucket is neutral (0) rather than an error."""
    return table.get(value, 0.) if isinstance(table, dict) else 0.


def kill_difficulty(kill, profile):
    k = profile['kill']
    total = (k['headshot'] if kill.get('headshot') == 'yes' else 0.) + _pick(k['flick'], kill.get('flick')) \
        + _pick(k['reaction'], kill.get('reaction')) + _pick(k['range'], kill.get('range')) \
        + (k['target_moving'] if kill.get('target_moving') is True else 0.) + _pick(k['shots'], kill.get('shots'))
    total += sum(_pick(k['special'], s) for s in set(kill.get('special') or []))
    return _clamp(total)


def situation_value(situation, profile):
    s, sit = profile['situation'], situation or {}
    total = 0.
    teammates, enemies = sit.get('teammates_alive'), sit.get('enemies_alive')
    if isinstance(teammates, int) and isinstance(enemies, int) and teammates == 0 and enemies >= 1:
        total += s['clutch'].get(str(min(enemies, 4)), 0.)
    for flag in ('low_hp', 'round_decisive', 'outnumbered', 'enemies_aware'):
        if sit.get(flag) is True: total += s[flag]
    return _clamp(total)


def kill_count_value(difficulty, profile):
    """0-1 credit for how many kills there were. Every kill is worth something on its own (a sloppy 2K is
    still two kills), and each extra kill adds more than the one before (2K < 3K < 4K < ace). An easy kill
    counts for a little less than a clean one (min_credit), but quality mainly shows up in mechanics."""
    m = profile['multi']
    if not difficulty: return 0.
    n = sum(m['min_credit'] + (1 - m['min_credit'])*min(1., d/m['floor']) if m['floor'] > 0 else 1. for d in difficulty)
    return _clamp((m['per_kill']*n + n*(n - 1)/2) / m['ramp'])


def components(features, profile):
    """(mechanics, situation, kill count, penalty factor), each 0-1."""
    kills = [k for k in features.get('kills') or [] if isinstance(k, dict)]
    difficulty = sorted((kill_difficulty(k, profile) for k in kills), reverse=True)
    mechanics = _clamp(difficulty[0] + .35*difficulty[1]) if len(difficulty) > 1 else (difficulty[0] if difficulty else 0.)
    penalty = 1.
    for flag in set(features.get('penalty_flags') or []):
        penalty *= profile['penalties'].get(flag, 1.)
    return mechanics, situation_value(features.get('situation'), profile), kill_count_value(difficulty, profile), penalty


def value(features, profile):
    """0-1 skill value (unrounded, for ranking): how hard the play was (mechanics + situation), plus credit
    for the number of kills, times anything that made it easy, blended with the model's overall read."""
    mechanics, situation, kills, penalty = components(features, profile)
    w = profile['weights']; blend = profile['blend']
    quality = (w['mechanics']*mechanics + w['situation']*situation) / ((w['mechanics'] + w['situation']) or 1.)
    raw = _clamp(blend['quality']*quality + w['multi']*kills) * penalty
    holistic = features.get('holistic_difficulty')
    if isinstance(holistic, (int, float)) and not isinstance(holistic, bool):
        return (blend['formula']*raw + blend['holistic']*_clamp(holistic/10)) / ((blend['formula'] + blend['holistic']) or 1.)
    return raw


def score(features, profile):
    """1-10 skill score from a play's facts. Pure and deterministic."""
    return int(_clamp(math.floor(10*value(features, profile) + .5), 1, 10))


FLICK_TAGS = {'medium': 'Flick', 'large': 'Big flick'}
SPECIAL_TAGS = {'wallbang': 'Wallbang', 'through_smoke': 'Through smoke', 'noscope': 'No-scope', 'airborne': 'Mid-air',
                'blind': 'While blind', 'collateral': 'Collateral', 'melee': 'Melee'}
PENALTY_TAGS = {'enemy_unaware': 'Unaware enemy', 'enemy_pre_damaged': 'Pre-damaged', 'long_spray_many_misses': 'Sloppy spray',
                'npc_or_bot': 'NPC/bot', 'exit_frag': 'Exit frag', 'teammate_did_work': 'Team did the work'}


def tags(features):
    """Short labels explaining a score: strengths first, then what counted against it."""
    kills = [k for k in features.get('kills') or [] if isinstance(k, dict)]
    out = []
    if len(kills) >= 2: out.append(f'{len(kills)}K')
    sit = features.get('situation') or {}
    if sit.get('teammates_alive') == 0 and isinstance(sit.get('enemies_alive'), int) and sit['enemies_alive'] >= 1:
        out.append(f"1v{sit['enemies_alive']}")
    heads = sum(k.get('headshot') == 'yes' for k in kills)
    if heads: out.append('Headshot' if heads == 1 else f'{heads} headshots')
    flicks = [k.get('flick') for k in kills]
    if 'large' in flicks: out.append(FLICK_TAGS['large'])
    elif 'medium' in flicks: out.append(FLICK_TAGS['medium'])
    if any(k.get('reaction') == 'fast' for k in kills): out.append('Fast reaction')
    if any(k.get('shots') == 'one_tap' for k in kills): out.append('One-tap')
    if any(k.get('range') == 'long' for k in kills): out.append('Long range')
    for special in SPECIALS:
        if any(special in (k.get('special') or []) for k in kills): out.append(SPECIAL_TAGS[special])
    if sit.get('low_hp') is True: out.append('Low HP')
    if sit.get('outnumbered') is True and not any(t.startswith('1v') for t in out): out.append('Outnumbered')
    if sit.get('round_decisive') is True: out.append('Decisive')
    drawbacks = [PENALTY_TAGS[f] for f in PENALTIES if f in (features.get('penalty_flags') or [])]
    return out, drawbacks


# --- triage gating and windows -------------------------------------------------------------------

def play_id(play):
    return identity([round(float(play['start']), 1), round(float(play['end']), 1)])[:12]


def plays_to_review(triage, profile, duration=None):
    """Candidate plays worth a close look: promising or with a kill, best first, at most gate.max_plays."""
    gate = profile['gate']; chosen = []
    for p in (triage or {}).get('plays') or []:
        try:
            start, end = float(p['start']), float(p['end'])
        except (KeyError, TypeError, ValueError):
            continue
        if duration is not None: start, end = max(0., start), min(duration, end)
        if end - start < .5: continue
        if (p.get('preliminary') or 0) >= gate['min_prelim'] or (p.get('kills') or 0) >= 1:
            chosen.append(dict(p, start=start, end=end))
    chosen.sort(key=lambda p: (-(p.get('preliminary') or 0), -(p.get('kills') or 0), p['start']))
    return chosen[:gate['max_plays']]


def window_bounds(play, duration):
    """[start, end] of the stage-B window in clip seconds. Long plays keep their ending (the kills)."""
    start, end = max(0., play['start'] - PAD_BEFORE), min(duration, play['end'] + PAD_AFTER)
    if end - start > MAX_WINDOW: start = end - MAX_WINDOW
    return round(start, 3), round(end, 3)


def to_clip_time(detail, window, play):
    """Window-relative times from Gemini become clip times, clamped to the window. A broken span falls
    back to the triage play."""
    w0, w1 = window; length = w1 - w0
    out = dict(detail)
    try:
        start, end = w0 + _clamp(float(detail['start']), 0, length), w0 + _clamp(float(detail['end']), 0, length)
    except (KeyError, TypeError, ValueError):
        start, end = play['start'], play['end']
    if end - start < 1.:
        start, end = max(w0, play['start']), min(w1, play['end'])
    out['start'], out['end'] = round(start, 3), round(end, 3)
    kills = []
    for k in detail.get('kills') or []:
        if not isinstance(k, dict): continue
        try: t = w0 + _clamp(float(k.get('t', 0)), 0, length)
        except (TypeError, ValueError): t = w0
        kills.append(dict(k, t=round(t, 3)))
    out['kills'] = kills
    return out


def detail_prompt(profile):
    return DETAIL_PROMPT.format(panel=profile['panel']['describes'], hints=profile.get('hints', ''))


# --- cache records -------------------------------------------------------------------------------

def record_path(cache, digest):
    return Path(cache)/'skill'/f'{digest}.json'


def load_record(cache, digest):
    path = record_path(cache, digest)
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    except (OSError, ValueError):
        return None


def save_record(cache, digest, record):
    path = record_path(cache, digest); path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, record)


def new_record(triage):
    return {'version': RECORD_VERSION, 'triage': triage, 'plays': {}}


def pending_plays(record, profile, duration=None):
    """Plays that pass the gate but have no stage-B judgment yet."""
    if not record: return []
    return [p for p in plays_to_review(record.get('triage'), profile, duration) if play_id(p) not in record.get('plays', {})]


def complete(record, profile, duration=None):
    return record is not None and not pending_plays(record, profile, duration)


def is_stale(record):
    return bool(record) and record.get('version') != RECORD_VERSION


# --- moments -------------------------------------------------------------------------------------

def moments_for(path, when, hint, record, meta, digest, profile):
    """Skill moments for one clip, in the same shape as reel.moments_for (plus lens and skill fields)."""
    if not record: return
    clip_start = when - meta['duration'] if hint else None
    plays = record.get('plays', {})
    for p in plays_to_review(record.get('triage'), profile, meta['duration']):
        detail = plays.get(play_id(p))
        if not detail: continue
        start, end = max(0., float(detail['start'])), min(meta['duration'], float(detail['end']))
        if end - start < .5: continue
        value = score(detail, profile); strengths, drawbacks = tags(detail)
        yield dict(id=identity([digest, 'skill', start, end]), source=digest, source_hash=digest, source_path=str(path),
                   start=start, end=end, duration=meta['duration'], audio_stream=meta['audio_stream'],
                   recorded_at=when, score=value, reason=detail.get('one_liner') or p.get('what', ''), transcript='',
                   recommendation='keep', clip_wall=clip_start, clip_name=path.name, lenses=['skill'],
                   skill_score=value, skill_tags=strengths, skill_drawbacks=drawbacks,
                   game=record.get('triage', {}).get('game'), kills=[k['t'] for k in detail.get('kills', [])])
