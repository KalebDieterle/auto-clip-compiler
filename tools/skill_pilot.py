"""Skill-scoring spot check on your own clips (live Gemini, about $0.01 per clip; judgments are saved).

Put ~10 clips you consider great plays in one folder and ~10 ordinary ones in another, then:

  python tools/skill_pilot.py --great "D:\\pilot\\great" --meh "D:\\pilot\\meh" [--preset aim] [--min-skill-score 7]

Pass: every great clip has a play at or above the minimum skill score, most meh clips do not, and
each miss can be explained from its tags. Also prints the measured tokens per triage and close-up,
for tuning the cost estimates (skill.TYPICAL_TRIAGE / TYPICAL_DETAIL / PLAYS_PER_CLIP).
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcompiler import job, settings as config, skill  # noqa: E402
from clipcompiler.reel import discover  # noqa: E402
from clipcompiler.store import HashIndex  # noqa: E402
from clipcompiler import paths  # noqa: E402
from gemini_reel import api_key, default_cache  # noqa: E402


def best_per_clip(moments):
    best = {}
    for m in moments:
        if m.get('lenses') == ['skill'] and m['score'] > best.get(m['clip_name'], {'score': 0})['score']:
            best[m['clip_name']] = m
    return best


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--great', required=True); p.add_argument('--meh', required=True)
    p.add_argument('--preset', choices=skill.PRESETS, default='balanced'); p.add_argument('--game', default='auto')
    p.add_argument('--min-skill-score', type=int, default=7); p.add_argument('--cache')
    args = p.parse_args()
    cache = Path(args.cache) if args.cache else default_cache()
    profiles = skill.Profiles(args.preset, args.game, config.profiles_dir())
    hasher = HashIndex(paths.hash_index_path()); verdicts = {}
    tokens = {'triage': [], 'detail': []}
    try:
        for label, folder in (('great', args.great), ('meh', args.meh)):
            clips = discover(folder)
            result = job.JudgeJob(clips, cache, Path(folder)/'.pilot-scratch', api_key=api_key(), content_mode='skill',
                                  profiles=profiles, min_skill_score=args.min_skill_score, hasher=hasher, workers=2).run()
            best = best_per_clip(result['moments'])
            for entry in result['report']:
                for play in (entry.get('skill') or {}).get('features', {}).values():
                    if play.get('_meta'): tokens['detail'].append(play['_meta'].get('input_tokens') or 0)
            for _, _, path in clips:
                record = skill.load_record(cache, hasher(path))
                meta = (record or {}).get('triage', {}).get('_meta') or {}
                if meta.get('input_tokens'): tokens['triage'].append(meta['input_tokens'])
                m = best.get(path.name)
                verdicts.setdefault(label, []).append((path.name, m))
    finally:
        hasher.save()
    print()
    for label in ('great', 'meh'):
        print(label.upper())
        for name, m in verdicts.get(label, []):
            if m: print(f"  {m['score']:>2}  {name}: {m['reason']}  [{', '.join(m['skill_tags'] + m['skill_drawbacks'])}]")
            else: print(f"   -  {name}: no play found")
    great = verdicts.get('great', []); meh = verdicts.get('meh', [])
    hits = sum(bool(m) and m['score'] >= args.min_skill_score for _, m in great)
    false = sum(bool(m) and m['score'] >= args.min_skill_score for _, m in meh)
    print(f"\nGreat clips at or above {args.min_skill_score}: {hits}/{len(great)}   Meh clips at or above: {false}/{len(meh)}")
    print('PASS' if great and hits == len(great) and false <= len(meh)//3 else 'CHECK THE MISSES ABOVE')
    for kind, values in tokens.items():
        if values: print(f'Measured {kind} input tokens: mean {sum(values)/len(values):.0f} over {len(values)}')
    total = len(great) + len(meh)
    if total: print(f"Close-ups per clip: {len(tokens['detail'])/total:.2f}")


if __name__ == '__main__':
    main()
