"""Characterization tests for the Gemini reel logic: moment extraction, replay dedupe, picking, parts.

The parity test replays the real 2026-10-12 library plan from a local fixture (outside the repo);
it is skipped on machines without it.
"""
import json
import os
from pathlib import Path
import unittest


from clipcompiler.gemini import cost
from clipcompiler.reel import dedupe, moments_for, pick, split_parts

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

    def test_clips_without_wall_time_are_never_merged(self):
        out = dedupe([m('a', 9, 0, 10, clip_wall=None), m('b', 8, 0, 10, clip_wall=None)])
        self.assertEqual(len(out), 2)


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
