"""Gemini judge: a 480p proxy of each clip is uploaded, watched and listened to, then deleted.

The prompt, schema and pricing are shared by live judging and the Batch API tool.
"""
import json
import logging
from pathlib import Path
import time

from .media import Cancelled, command
from .store import identity

log = logging.getLogger(__name__)

DEFAULT_MODEL = 'gemini-flash-latest'
PROMPT_VERSION = 'reel-v2'
PROMPT = '''You are picking moments for a funny compilation of gaming clips with friends.
Watch AND listen to this whole clip. Find moments that are actually funny or memorable:
banter, laughter, screaming, unexpected outcomes, fails, visual absurdity, great reactions.
Ordinary kills, generic callouts and plain loudness do not count.
For each moment give start/end in seconds that include the full setup, the payoff AND the reaction
(do not cut the laugh off). Score 1-10 for how well it would play in a compilation to friends who
were there. If nothing qualifies, return an empty list and say why in summary.
Only humor counts. Skill highlights (good plays, clutch fights, nice shots, big kills) are NOT wanted
unless something genuinely funny happens in them: score pure skill moments 4 or lower.'''

SCHEMA = {'type': 'object', 'properties': {
    'summary': {'type': 'string'},
    'moments': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'start': {'type': 'number'}, 'end': {'type': 'number'}, 'score': {'type': 'integer'},
        'setup': {'type': 'string'}, 'payoff': {'type': 'string'}, 'reaction': {'type': 'string'},
        'why_funny': {'type': 'string'}},
        'required': ['start', 'end', 'score', 'setup', 'payoff', 'reaction', 'why_funny']}}},
    'required': ['summary', 'moments']}

# Gemini 3.8 Flash paid tier, USD per 1M tokens, through 2026-12-31 (doubles 2027-01-01).
PRICE_IN, PRICE_OUT = 0.75, 3.75
BATCH_DISCOUNT = .5
# Measured on 2,687 real 30 s clips (2026-10-12): ~$0.0045 per clip live.
TYPICAL_TOKENS = {'input_tokens': 2850, 'output_tokens': 200, 'thinking_tokens': 450}


def cost(meta):
    """Billed dollars for one judgment; thinking tokens bill at the output rate."""
    billed_out = (meta.get('output_tokens') or 0) + (meta.get('thinking_tokens') or 0)
    return ((meta.get('input_tokens') or 0)*PRICE_IN + billed_out*PRICE_OUT) / 1e6


def estimate(clips, batch=False):
    return clips * cost(TYPICAL_TOKENS) * (BATCH_DISCOUNT if batch else 1)


class DailyLimit(Exception):
    """Quota exhausted for the day: stop judging, render what exists."""


def client(api_key):
    from google import genai
    return genai.Client(api_key=api_key)


def proxy(path, directory, ffmpeg='ffmpeg', cancel=None):
    """Small 480p/10fps copy for upload; same timeline, so moment times map 1:1 to the original."""
    target = Path(directory)/(identity([str(path)])[:16]+'.mp4')
    tail = ['-i', path, '-map', '0:v:0', '-map', '0:a:0?', '-vf', 'scale=-2:480,fps=10', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-crf', '30', '-c:a', 'aac', '-b:a', '96k', '-ac', '2', target]
    try:
        # OBS records AV1; hardware decode is ~3x faster than software.
        command([ffmpeg, '-v', 'error', '-y', '-hwaccel', 'd3d11va'] + tail, cancel=cancel)
    except RuntimeError:
        command([ffmpeg, '-v', 'error', '-y'] + tail, cancel=cancel)  # No usable GPU decoder.
    return target


def judge(gc, model, path, stop, scratch, ffmpeg='ffmpeg'):
    small = proxy(path, scratch, ffmpeg, stop)
    try:
        return upload_and_judge(gc, model, path, small, stop)
    finally:
        small.unlink(missing_ok=True)


def upload_and_judge(gc, model, path, small, stop):
    from google.genai import errors
    for attempt in range(4):
        if stop.is_set(): raise Cancelled()
        try:
            uploaded = gc.files.upload(file=str(small)); break
        except Exception as exc:  # SSL resets, timeouts, 5xx during upload (httpx wraps some of them).
            if attempt == 3 or isinstance(exc, errors.ClientError): raise
            log.warning('%s: upload error (%s); retrying', path.name, exc); time.sleep(10*(attempt+1))
    try:
        while uploaded.state.name == 'PROCESSING':
            time.sleep(2); uploaded = gc.files.get(name=uploaded.name)
        if uploaded.state.name != 'ACTIVE':
            raise RuntimeError(f'Upload failed: {uploaded.state}')
        started = time.monotonic()
        for attempt in range(7):
            if stop.is_set(): raise Cancelled()
            try:
                response = gc.models.generate_content(model=model, contents=[uploaded, PROMPT],
                    config={'response_mime_type': 'application/json', 'response_schema': SCHEMA, 'temperature': 0})
                break
            except (errors.ServerError, errors.ClientError) as exc:
                text = str(exc)
                if exc.code == 429 and ('per day' in text.lower() or 'PerDay' in text):
                    raise DailyLimit(text) from exc
                if exc.code not in (429, 500, 503) or attempt == 6: raise
                wait = min(240, 15 * 2**attempt)
                log.warning('%s: Gemini %s; retrying in %ss', path.name, exc.code, wait)
                if stop.wait(wait): raise Cancelled()
        result = json.loads(response.text)
        usage = response.usage_metadata
        result['_meta'] = {'model': model, 'prompt': PROMPT_VERSION, 'seconds': round(time.monotonic()-started, 1),
                           'input_tokens': usage.prompt_token_count, 'output_tokens': usage.candidates_token_count,
                           'thinking_tokens': usage.thoughts_token_count}
        return result
    finally:
        try: gc.files.delete(name=uploaded.name)  # Never leave clips on Google's side.
        except Exception as exc: log.warning('could not delete upload %s: %s', uploaded.name, exc)
