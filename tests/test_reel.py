"""Characterization tests for the Gemini reel logic: moment extraction, replay dedupe, picking, parts.

The parity test replays the real 2026-10-12 library plan from a local fixture (outside the repo);
it is skipped on machines without it.
"""
import json
import os
from pathlib import Path
import unittest


from clipcompiler.gemini import cost
from clipcompiler.reel import (Footage, dedupe, footage, merge_lenses, moments_for, part_plan, pick, shown_wall,
                               split_parts, unseen)

PARITY = Path(os.environ.get('LOCALAPPDATA', ''))/'AutoClipCompiler-dev'/'parity'


def m(id, score, start, end, clip_wall: float | None = 0., duration=30., recorded_at=0.):
    return dict(id=id, score=score, start=start, end=end, clip_wall=clip_wall, duration=duration,
                recorded_at=recorded_at, clip_name=id)


class MomentsForTests(unittest.TestCase):
    def test_clamps_skips_slivers_and_places_replay_in_wall_time(self):
        result = {'moments': [dict(start=-2, end=5, score=8, why_funny='a'),
                              dict(start=18, end=25, score=7, why_funny='b'),
                              dict(start=10, end=10.3, score=9, why_funny='too short')]}
        meta = {'duration': 20., 'audio_stream': 1}
        found = list(moments_for(Path('c.mp4'), 1000., True, result, meta, 'h'))
        self.assertEqual([(f['start'], f['end'], f['score']) for f in found], [(0., 5., 8), (18., 20., 7)])
        self.assertEqual(found[0]['clip_wall'], 980.)  # OBS names a replay when saved: it ends at `when`.
        self.assertEqual(found[0]['reason'], 'a')

    def test_no_filename_time_means_no_wall_clock(self):
        found = list(moments_for(Path('c.mp4'), 1000., False, {'moments': [dict(start=1, end=4, score=7, why_funny='x')]},
                                 {'duration': 20., 'audio_stream': None}, 'h'))
        self.assertIsNone(found[0]['clip_wall'])


class UnseenTests(unittest.TestCase):
    """Leaving out footage an earlier compilation played (reel.unseen, fed by reel.footage)."""
    def test_same_recording_mostly_shown_is_dropped_and_untouched_kept(self):
        shown = footage(dict(m('old', 8, 2, 10), source='A'))
        out = unseen([dict(m('a', 8, 3, 10), source='A'), dict(m('b', 8, 3, 10), source='B', clip_wall=100.)], shown)
        self.assertEqual([x['id'] for x in out], ['b'])

    def test_overlapping_replay_save_counts_through_wall_time(self):
        shown = footage(dict(m('old', 8, 10, 20, clip_wall=0.), source='A'))   # Wall 10-20.
        out = unseen([dict(m('a', 8, 2, 12, clip_wall=8.), source='B')], shown)    # Wall 10-20 again, other file.
        self.assertEqual(out, [])

    def test_partly_shown_keeps_only_new_footage(self):
        shown = footage(dict(m('old', 8, 0, 4), source='A'))
        out = unseen([dict(m('a', 8, 2, 12), source='A')], shown)
        self.assertEqual([(x['start'], x['end']) for x in out], [(4, 12)])

    def test_setup_that_played_counts_as_shown(self):
        old = dict(m('old', 8, 10, 14), source='A', lead=[dict(source='B', start=1, end=6, clip_wall=None)])
        out = unseen([dict(m('a', 8, 1, 7, clip_wall=None), source='B')], footage(old))
        self.assertEqual(out, [])

    def test_coverage_is_the_union_so_order_does_not_matter(self):
        a, b = dict(m('x', 8, 0, 3), source='A'), dict(m('y', 8, 6, 9), source='A')   # 6 of 10 s shown.
        for shown in ([a, b], [b, a]):
            self.assertEqual(unseen([dict(m('c', 8, 0, 10), source='A')], [s for x in shown for s in footage(x)]), [])

    def test_longest_new_stretch_is_kept_under_a_new_id(self):
        shown = footage(dict(m('x', 8, 3, 5), source='A'))
        out = unseen([dict(m('c', 8, 0, 12), source='A')], shown)
        self.assertEqual([(x['start'], x['end']) for x in out], [(5, 12)])
        self.assertNotEqual(out[0]['id'], 'c')   # Previews are cached by id: new bounds, new id.

    def test_skill_play_is_dropped_rather_than_trimmed_away_from_its_kill(self):
        shown = footage(dict(m('x', 8, 0, 4), source='A'))
        play = dict(m('k', 9, 0, 10), source='A', kills=[2.])
        self.assertEqual(unseen([play], shown), [])
        kept = unseen([dict(play, kills=[8.])], shown)   # Kill still in what is left: trimmed, still a play.
        self.assertEqual([(x['start'], x['end']) for x in kept], [(4, 10)])

    def test_malformed_footage_is_ignored(self):
        bad = [{}, {'start': 'x'}, {'source': 'A', 'start': 5, 'end': 1}, {'source': 'A', 'start': 0, 'end': float('inf')}]
        out = unseen([dict(m('a', 8, 0, 10), source='A')], bad)
        self.assertEqual([x['id'] for x in out], ['a'])
        self.assertEqual(Footage([{'source': 'A', 'start': 0, 'end': 4}]).slices[0]['clip_wall'], None)   # Normalized.

    def test_same_recording_maps_onto_its_current_clock(self):
        p = {'source': 'A', 'start': 5., 'end': 8., 'clip_wall': None}   # Rendered before it had a dated name.
        self.assertEqual(shown_wall(p, {'source': 'A', 'clip_wall': 100.}), (105., 108.))
        self.assertIsNone(shown_wall(p, {'source': 'B', 'clip_wall': 100.}))

    def test_index_finds_far_wall_slices_only_when_they_overlap(self):
        shown = [dict(source=f'S{i}', start=0., end=10., clip_wall=1000.*i) for i in range(500)]
        out = unseen([dict(m('a', 8, 0, 10, clip_wall=250_000.), source='B'),
                      dict(m('b', 8, 0, 10, clip_wall=250_500.), source='C')], shown)
        self.assertEqual([x['id'] for x in out], ['b'])

    def test_no_wall_clock_and_other_recording_is_never_matched(self):
        shown = footage(dict(m('old', 8, 0, 10, clip_wall=None), source='A'))
        out = unseen([dict(m('a', 8, 0, 10, clip_wall=None), source='B')], shown)
        self.assertEqual([x['id'] for x in out], ['a'])


class DedupeTests(unittest.TestCase):
    def test_event_inside_accepted_recording_is_merged_into_it(self):
        out = dedupe([m('a', 8, 10, 15), m('b', 7, 7, 12, clip_wall=5)])
        self.assertEqual([(x['id'], x['start'], x['end']) for x in out], [('a', 10, 17)])

    def test_event_only_the_lower_score_recording_holds_switches_to_it_keeping_best_score(self):
        out = dedupe([m('a', 9, 8, 12, duration=12), m('b', 7, 5, 10, clip_wall=5)])
        self.assertEqual([(x['id'], x['score'], x['start'], x['end']) for x in out], [('b', 9, 3, 10)])

    def test_mostly_repeated_moment_is_dropped(self):
        out = dedupe([m('a', 9, 6, 12, duration=12), m('b', 7, 0, 4, clip_wall=10)])
        self.assertEqual([x['id'] for x in out], ['a'])

    def test_partly_repeated_moment_keeps_only_new_footage(self):
        out = dedupe([m('a', 9, 4, 12, duration=12), m('b', 7, 0, 10, clip_wall=10)])
        self.assertEqual([(x['id'], x['start'], x['end']) for x in out], [('a', 4, 12), ('b', 2, 10)])

    def test_leftover_under_two_seconds_is_dropped(self):
        out = dedupe([m('a', 9, 4, 12, duration=12), m('b', 7, 0, 3.4, clip_wall=10.5)])
        self.assertEqual([x['id'] for x in out], ['a'])

    def test_later_replay_missing_under_a_second_of_lead_in_still_takes_the_event(self):
        # Real case (2026-10-17 20-05-05 / 20-05-15, the "rigga" callback): the later save starts 0.283 s
        # after the event, so without slack the payoff only it holds (8.8 s) was dropped whole.
        a = m('a', 8, 11.5, 29.983333, clip_wall=475.016667, duration=29.983333)
        b = m('b', 7, 0., 27., clip_wall=486.8, duration=28.2)
        out = dedupe([a, b])
        self.assertEqual([(x['id'], x['score'], x['start'], round(x['end'], 6)) for x in out], [('b', 8, 0., 27.)])

    def test_slack_never_cuts_an_ending(self):
        # The event runs 0.5 s past the end of every recording: no slack there, so it is not merged.
        out = dedupe([m('a', 9, 20, 30, duration=30), m('b', 7, 0, 12, clip_wall=19, duration=10.5)])
        self.assertEqual([x['id'] for x in out], ['a'])

    def test_exact_home_beats_a_slack_home(self):
        # Union is wall 2.5-12. The accepted (better) recording a needs 0.5 s of slack; b holds it exactly: use b.
        out = dedupe([m('a', 9, .5, 9, clip_wall=3., duration=20), m('b', 7, 2.5, 8, clip_wall=0., duration=20)])
        self.assertEqual([(x['id'], x['score'], x['start'], x['end']) for x in out], [('b', 9, 2.5, 12.)])

    def test_clips_without_wall_time_are_never_merged(self):
        out = dedupe([m('a', 9, 0, 10, clip_wall=None), m('b', 8, 0, 10, clip_wall=None)])
        self.assertEqual(len(out), 2)


class LengthTests(unittest.TestCase):
    def test_parts_and_plans_count_each_moments_setup(self):
        moments = [dict(m(str(t), 8, 0, 10, recorded_at=t*60), lead=[dict(start=0, end=10)]) for t in range(6)]
        parts = split_parts(moments, 30)
        self.assertEqual([len(p) for p in parts], [2, 2, 2])
        self.assertEqual([r['minutes'] for r in part_plan(parts)], [.7, .7, .7])


class PickTests(unittest.TestCase):
    def test_best_first_until_target_then_chronological(self):
        chosen, total = pick([m('x', 9, 0, 10, recorded_at=3), m('y', 8, 0, 10, recorded_at=1),
                              m('z', 7, 0, 10, recorded_at=2)], 15)
        self.assertEqual(([c['id'] for c in chosen], total), (['y', 'x'], 20.))


class SplitPartsTests(unittest.TestCase):
    def sizes(self, times, part_seconds):
        return [len(p) for p in split_parts([m(str(t), 8, 0, 30, recorded_at=t) for t in times], part_seconds)]

    def test_full_part_breaks_at_length(self):
        self.assertEqual(self.sizes([0, 60, 120, 180, 240, 300], 100), [4, 2])

    def test_mostly_full_part_prefers_a_session_break(self):
        self.assertEqual(self.sizes([0, 60, 120, 5000, 5060, 5120], 100), [3, 3])

    def test_short_tail_joins_previous_part(self):
        self.assertEqual(self.sizes([0, 60, 120, 180, 240], 110), [5])


def lens(id, kind, score, start, end, source='a', clip_wall=0., recorded_at=0., duration=30.):
    out = dict(m(id, score, start, end, clip_wall, duration, recorded_at), source=source, lenses=[kind])
    out['skill_score' if kind == 'skill' else 'funny_score'] = score
    if kind == 'skill': out.update(skill_tags=['Headshot'], skill_drawbacks=[], game='cs2', kills=[start+1], reason=f'skill {id}')
    return out


class LensMergeTests(unittest.TestCase):
    def test_a_play_that_is_both_funny_and_skillful_is_shown_once_with_both_tags(self):
        merged = merge_lenses([lens('f', 'funny', 8, 10, 16)], [lens('s', 'skill', 9, 11, 18)])
        self.assertEqual(len(merged), 1); x = merged[0]
        self.assertEqual((x['lenses'], x['start'], x['end'], x['funny_score'], x['skill_score']), (['funny', 'skill'], 10, 18, 8, 9))
        self.assertEqual((x['skill_tags'], x['skill_reason']), (['Headshot'], 'skill s'))

    def test_a_skill_moment_partly_repeating_a_funny_one_keeps_only_new_footage(self):
        merged = merge_lenses([lens('f', 'funny', 8, 10, 20)], [lens('s', 'skill', 9, 18, 26)])
        self.assertEqual([(x['id'], x['start'], x['end']) for x in merged], [('f', 10, 20), ('s', 20, 26)])

    def test_replays_of_the_same_play_merge_across_recordings_in_wall_time(self):
        funny = lens('f', 'funny', 8, 10, 16, source='a', clip_wall=100.)
        skilled = lens('s', 'skill', 9, 5, 11, source='b', clip_wall=105.)   # Same wall time 110-116.
        self.assertEqual([x['lenses'] for x in merge_lenses([funny], [skilled])], [['funny', 'skill']])

    def test_unrelated_moments_stay_separate(self):
        merged = merge_lenses([lens('f', 'funny', 8, 0, 5)], [lens('s', 'skill', 9, 5, 9, source='b', clip_wall=None)])
        self.assertEqual([x['lenses'] for x in merged], [['funny'], ['skill']])


class QuotaPickTests(unittest.TestCase):
    def moments(self):
        out = [dict(lens(f'f{i}', 'funny', 10-i, 0, 10, source=f'f{i}', recorded_at=i), lens='funny', margin=3-i) for i in range(4)]
        out += [dict(lens(f's{i}', 'skill', 9-i, 0, 10, source=f's{i}', recorded_at=10+i), lens='skill', margin=2-i) for i in range(4)]
        return out

    def test_each_lens_fills_its_share_by_its_own_score(self):
        chosen, total = pick(self.moments(), 40, skill_share=.5)
        self.assertEqual(sorted(x['id'] for x in chosen), ['f0', 'f1', 's0', 's1'])
        self.assertEqual(total, 40)

    def test_leftover_time_goes_to_the_biggest_margin_when_a_lens_runs_short(self):
        only_two_skill = [x for x in self.moments() if x['id'] not in ('s2', 's3')]
        chosen, _ = pick(only_two_skill, 60, skill_share=.5)
        self.assertEqual(sorted(x['id'] for x in chosen), ['f0', 'f1', 'f2', 'f3', 's0', 's1'])

    def test_without_a_share_pick_is_unchanged(self):
        chosen, _ = pick(self.moments(), 20)
        self.assertEqual(sorted(x['id'] for x in chosen), ['f0', 'f1'])


class CandidateLensTests(unittest.TestCase):
    def test_each_lens_uses_its_own_threshold_and_gets_a_primary_lens(self):
        from clipcompiler.job import candidates
        moments = [lens('f', 'funny', 6, 0, 5, source='a'), lens('s', 'skill', 6, 0, 5, source='b'),
                   lens('f2', 'funny', 10, 10, 16, source='c'), lens('s2', 'skill', 8, 11, 17, source='c')]
        cands = candidates(moments, min_score=7, min_skill_score=6)
        self.assertEqual([(x['id'], x['lenses'], x['lens'], x['margin']) for x in cands],
                         [('s', ['skill'], 'skill', 0), ('f2', ['funny', 'skill'], 'funny', 3)])
        tie = candidates([lens('f', 'funny', 9, 0, 5), lens('s', 'skill', 8, 0, 5)], 7, 6)
        self.assertEqual((tie[0]['lens'], tie[0]['score']), ('skill', 8))   # Equal margins: shown as a skill play.


class CostTests(unittest.TestCase):
    def test_thinking_bills_at_output_rate_and_missing_counts_are_zero(self):
        self.assertAlmostEqual(cost({'input_tokens': 2850, 'output_tokens': 200, 'thinking_tokens': 450}), .004575)
        self.assertEqual(cost({'input_tokens': None}), 0)


@unittest.skipUnless((PARITY/'plan.json').exists(), 'no local library parity fixture')
class LibraryParityTests(unittest.TestCase):
    def test_reproduces_the_2026_10_12_library_plan(self):
        moments = json.loads((PARITY/'moments.json').read_text(encoding='utf-8'))
        expected = json.loads((PARITY/'plan.json').read_text(encoding='utf-8'))
        good = dedupe([dict(x) for x in moments if x['score'] >= expected['min_score']])
        parts = split_parts(good, expected['part_seconds'])
        self.assertEqual([[x['id'] for x in p] for p in parts], [p['ids'] for p in expected['parts']])


if __name__ == '__main__':
    unittest.main()
