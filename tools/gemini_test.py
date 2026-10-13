"""Experiment: judge clips with Gemini watching the real video + audio, then render a compilation.

Uploads each clip to the Gemini Files API, deletes it after judging, caches judgments locally.
Requires GEMINI_API_KEY. Usage: python tools/gemini_test.py <clip-list.json> <output-dir> [--min-score 6]
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcompiler.media import probe, recording_time  # noqa: E402
from clipcompiler.render import publish  # noqa: E402
from clipcompiler.store import file_hash, identity, atomic_json  # noqa: E402

PROMPT = '''You are picking moments for a funny compilation of gaming clips with friends.
Watch AND listen to this whole clip. Find moments that are actually funny or memorable:
banter, laughter, screaming, unexpected outcomes, fails, visual absurdity, great reactions.
Ordinary kills, generic callouts and plain loudness do not count.
For each moment give start/end in seconds that include the full setup, the payoff AND the reaction
(do not cut the laugh off). Score 1-10 for how well it would play in a compilation to friends who
were there. If nothing qualifies, return an empty list and say why in summary.'''

SCHEMA = {'type': 'object', 'properties': {
    'summary': {'type': 'string'},
    'moments': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'start': {'type': 'number'}, 'end': {'type': 'number'}, 'score': {'type': 'integer'},
        'setup': {'type': 'string'}, 'payoff': {'type': 'string'}, 'reaction': {'type': 'string'},
        'why_funny': {'type': 'string'}},
        'required': ['start', 'end', 'score', 'setup', 'payoff', 'reaction', 'why_funny']}}},
    'required': ['summary', 'moments']}


def judge(client, model, path):
    uploaded = client.files.upload(file=str(path))
    try:
        while uploaded.state.name == 'PROCESSING':
            time.sleep(2); uploaded = client.files.get(name=uploaded.name)
        if uploaded.state.name != 'ACTIVE':
            raise RuntimeError(f'Upload failed: {uploaded.state}')
        started = time.monotonic()
        from google.genai import errors
        for attempt in range(6):
            try:
                response = client.models.generate_content(model=model, contents=[uploaded, PROMPT],
                    config={'response_mime_type': 'application/json', 'response_schema': SCHEMA, 'temperature': 0})
                break
            except errors.ServerError as exc:  # 503 "high demand" spikes are transient.
                if attempt == 5: raise
                wait = 15 * 2**attempt
                print(f'    Gemini busy ({exc.code}); retrying in {wait}s', flush=True); time.sleep(wait)
        result = json.loads(response.text)
        usage = response.usage_metadata
        result['_meta'] = {'model': model, 'seconds': round(time.monotonic()-started, 1),
                           'input_tokens': usage.prompt_token_count, 'output_tokens': usage.candidates_token_count}
        return result
    finally:
        client.files.delete(name=uploaded.name)  # Never leave clips on Google's side.


def main():
    p = argparse.ArgumentParser(); p.add_argument('clips'); p.add_argument('output')
    p.add_argument('--model', default='gemini-flash-latest'); p.add_argument('--min-score', type=int, default=6)
    p.add_argument('--exclude', action='append', default=[], help='Clip filename to leave out; repeatable.')
    args = p.parse_args()
    from google import genai
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True); cache = out/'judgments'; cache.mkdir(exist_ok=True)
    items = []; report = []
    for i, path in enumerate(map(Path, json.loads(Path(args.clips).read_text(encoding='utf-8'))), 1):
        if path.name in args.exclude:
            print(f'[{i}] {path.name}: excluded', flush=True); continue
        digest = file_hash(path); target = cache/(digest+'.json')
        if target.exists():
            result = json.loads(target.read_text(encoding='utf-8'))
        else:
            print(f'[{i}] judging {path.name} ...', flush=True)
            try:
                result = judge(client, args.model, path); atomic_json(target, result)
            except Exception as exc:
                print(f'[{i}] {path.name}: FAILED {exc}', flush=True)
                report.append({'clip': path.name, 'error': str(exc)}); continue
        meta = probe(path); recorded, _ = recording_time(path)
        print(f'[{i}] {path.name}: {len(result["moments"])} moments, {result["_meta"]}', flush=True)
        report.append({'clip': path.name, **result})
        for m in result['moments']:
            start, end = max(0., m['start']), min(meta['duration'], m['end'])
            if m['score'] < args.min_score or end-start < .5:
                continue
            items.append(dict(id=identity([digest, start, end]), source=digest, source_hash=digest, source_path=str(path),
                start=start, end=end, duration=meta['duration'], audio_stream=meta['audio_stream'], recorded_at=recorded,
                score=m['score'], reason=m['why_funny'], transcript='', recommendation='keep'))
    atomic_json(out/'report.json', report)
    items.sort(key=lambda c: (c['recorded_at'], c['start']))
    print(f'Selected {len(items)} moments scoring >= {args.min_score}', flush=True)
    if items:
        final = publish(items, out, {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe', 'fps': 60, 'render_height': 1080,
                                     'workers': 2, 'resolve': True, 'render': True, 'min_free_gb': 2})
        print(f'Published {final}', flush=True)


if __name__ == '__main__':
    main()
