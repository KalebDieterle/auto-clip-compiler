"""Look-back for a kept moment's setup: windows across replays, beat filtering, slices, resolve, the job pass."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clipcompiler import context, gemini, job
from clipcompiler.media import probe
from tests.helpers import clip

A = {'path': 'a.mp4', 'digest': 'a', 'duration': 30., 'wall': 100., 'recorded_at': 130.}   # wall 100-130
B = {'path': 'b.mp4', 'digest': 'b', 'duration': 30., 'wall': 120., 'recorded_at': 150.}   # wall 120-150, overlaps A


def moment(source='b', clip_wall=120., start=15., end=25., recorded_at=150., **kw):
    return dict(dict(id=f'{source}{start}', source=source, source_hash=source, source_path=f'{source}.mp4', clip_wall=clip_wall,
                     start=start, end=end, duration=30., recorded_at=recorded_at, clip_name=f'{source}.mp4', lenses=['funny'],
                     score=8, reason='r', setup='s', payoff='p', reaction='x'), **kw)


def beat(start, end, link=0, funny=0):
    return {'start': start, 'end': end, 'link': link, 'funny': funny, 'evidence': f'{start}', 'why': 'w'}


class WindowTests(unittest.TestCase):
    def test_reaches_back_into_the_previous_overlapping_replay(self):
        w = context.window(moment(), [A, B])   # Moment at wall 135-145, look back to 105.
        self.assertEqual([(p['digest'], p['start'], p['end'], p['at']) for p in w['pieces']], [('a', 5., 20., 0.), ('b', 0., 25., 15.)])
        self.assertEqual(w['core'], (30., 40.)); self.assertIsNone(w['shown'])

    def test_unrecorded_gap_stops_the_window(self):
        early = dict(A, wall=60.)   # Ends at 90: 30 s before B begins.
        w = context.window(moment(), [early, B])
        self.assertEqual([p['digest'] for p in w['pieces']], ['b']); self.assertEqual(w['core'], (15., 25.))

    def test_a_small_gap_between_replays_is_skipped_in_window_time(self):
        early = dict(A, duration=19.25)   # Ends 0.75 s before B starts: within SEAM, so it still reaches.
        w = context.window(moment(), [early, B])
        self.assertEqual([(p['digest'], p['start'], p['end'], p['at']) for p in w['pieces']], [('a', 5., 19.25, 0.), ('b', 0., 25., 14.25)])
        self.assertEqual(w['core'], (29.25, 39.25))

    def test_the_moments_own_file_wins_over_a_copy_with_the_same_content(self):
        copy = dict(B, path='copy.mp4', wall=200.)
        w = context.window(moment(source_path='copy.mp4', clip_wall=200.), [B, copy])
        self.assertEqual([p['path'] for p in w['pieces']], ['copy.mp4'])

    def test_previous_moment_in_the_window_is_marked_shown(self):
        w = context.window(moment(), [A, B], previous=(110., 118.))
        self.assertEqual(w['shown'], (5., 13.))

    def test_no_wall_clock_or_nothing_before_means_no_window(self):
        self.assertIsNone(context.window(moment(clip_wall=None), [A, B]))
        self.assertIsNone(context.window(moment(source='a', clip_wall=100., start=.5, end=5.), [A]))


class PlanTests(unittest.TestCase):
    W = {'core': (30., 40.), 'shown': None}

    def test_only_linked_or_funny_beats_padded_inside_the_window(self):
        spans = context.plan({'beats': [beat(10, 12, link=2), beat(20, 22, funny=1), beat(27, 29.8, funny=2)]}, self.W)
        self.assertEqual([(s['start'], s['end']) for s in spans], [(9.5, 12.5), (26.5, 30.)])

    def test_every_qualifying_beat_is_kept_for_resolve_to_cap(self):
        spans = context.plan({'beats': [beat(0, 10, link=3), beat(14, 24, link=2), beat(23, 25, link=2)]}, self.W)
        self.assertEqual([(s['start'], s['end']) for s in spans], [(0., 10.5), (13.5, 25.5)])

    def test_beats_inside_the_previous_moment_are_kept_for_resolve(self):
        # If Review cuts the previous moment, this setup must still be there; resolve trims it otherwise.
        spans = context.plan({'beats': [beat(6, 10, link=3), beat(0, 2, link=2)]}, dict(self.W, shown=(5., 13.)))
        self.assertEqual([(s['start'], s['end']) for s in spans], [(0., 2.5), (5.5, 10.5)])

    def test_beats_that_only_touch_after_padding_stay_separate(self):
        spans = context.plan({'beats': [beat(0, 7, link=2), beat(8, 15, link=2)]}, dict(self.W, core=(16., 26.)))
        self.assertEqual([(s['start'], s['end']) for s in spans], [(0., 7.5), (7.5, 15.5)])

    def test_key_changes_with_every_word_of_the_question(self):
        w = context.window(moment(), [A, B])
        bare = moment(payoff='')
        self.assertNotEqual(context.key(w, dict(bare, reason='one'), 'f'), context.key(w, dict(bare, reason='two'), 'f'))
        self.assertEqual(context.key(w, moment(), 'f'), context.key(w, moment(), 'f'))

    def test_bad_answers_are_ignored(self):
        self.assertEqual(context.plan({'beats': [{'start': 'x', 'end': 2, 'link': 3}, beat(35, 39, link=3)]}, self.W), [])
        self.assertEqual(context.plan(None, self.W), [])

    def test_a_span_across_two_replays_becomes_two_slices(self):
        w = context.window(moment(), [A, B])
        out = context.slices([{'start': 13., 'end': 22., 'evidence': ['e']}], w)
        self.assertEqual([(s['source'], s['start'], s['end']) for s in out], [('a', 18., 20.), ('b', 0., 7.)])


def lead(source, clip_wall, start, end):
    return {'source': source, 'source_hash': source, 'source_path': f'{source}.mp4', 'clip_wall': clip_wall,
            'start': start, 'end': end, 'duration': 30., 'recorded_at': 0., 'evidence': ['e']}


class ResolveTests(unittest.TestCase):
    def test_setup_running_into_the_moment_in_its_recording_moves_the_start_instead(self):
        m = moment(lead=[lead('b', 120., 10., 15.)])
        out = context.resolve([m])[0]
        self.assertEqual((out['start'], out['lead']), (10., []))
        self.assertEqual(m['start'], 15.)   # Input untouched.

    def test_setup_with_a_gap_stays_a_separate_slice(self):
        out = context.resolve([moment(lead=[lead('b', 120., 8., 12.)])])[0]
        self.assertEqual((out['start'], [(s['start'], s['end']) for s in out['lead']]), (15., [(8., 12.)]))

    def test_cap_takes_the_nearest_setups_that_fit_and_skips_one_that_cannot(self):
        near, far = dict(lead('b', 120., 12., 19.5), excerpt=0), dict(lead('b', 120., 2., 10.), excerpt=1)
        out = context.resolve([moment(start=20., end=25., lead=[far, near])])[0]
        self.assertEqual((out['start'], out['lead']), (12., []))   # 7.5 + 8 > 15: the far one goes; near runs into the moment.
        huge, small = dict(lead('b', 120., 3.5, 19.5), excerpt=0), dict(lead('b', 120., 0., 3.), excerpt=1)
        out = context.resolve([moment(start=20., end=25., lead=[huge, small])])[0]
        self.assertEqual((out['start'], [(s['start'], s['end']) for s in out['lead']]), (20., [(0., 3.)]))

    def test_close_setups_in_one_recording_play_through_unless_the_gap_was_already_shown(self):
        two = [dict(lead('b', 120., 5., 8.), excerpt=0), dict(lead('b', 120., 9., 12.), excerpt=1)]
        out = context.resolve([moment(start=20., end=25., lead=two)])[0]
        self.assertEqual([(s['start'], s['end']) for s in out['lead']], [(5., 12.)])
        before = moment(source='a', clip_wall=100., start=28., end=29.5, recorded_at=130.)   # Plays wall 128-129.5.
        out = context.resolve([before, moment(start=20., end=25., lead=two)])[1]
        self.assertEqual([(s['start'], s['end']) for s in out['lead']], [(5., 8.), (9.5, 12.)])

    def test_never_replays_the_previous_moment_or_the_moment_itself(self):
        before = moment(source='a', clip_wall=100., start=20., end=28., recorded_at=130.)      # wall 120-128
        m = moment(lead=[lead('a', 100., 22., 37.)])                                            # wall 122-137
        out = context.resolve([m, before])
        self.assertEqual([(s['start'], s['end']) for s in out[1]['lead']], [(28., 35.)])       # wall 128-135

    def test_a_short_setup_split_at_a_seam_is_judged_whole(self):
        halves = [dict(lead('a', 100., 29.4, 30.), excerpt=0), dict(lead('b', 120., 10., 10.6), excerpt=0)]
        out = context.resolve([moment(lead=halves)])[0]
        self.assertEqual([(s['source'], s['start'], s['end']) for s in out['lead']], [('a', 29.4, 30.), ('b', 10., 10.6)])

    def test_epoch_wall_times_keep_an_exact_one_second_remainder(self):
        wall = 1792278322.85
        before = moment(source='a', clip_wall=wall, start=20., end=28., recorded_at=wall+30)
        m = moment(clip_wall=wall+20, recorded_at=wall+50, lead=[lead('a', wall, 22., 29.)])
        self.assertEqual([(s['start'], s['end']) for s in context.resolve([before, m])[1]['lead']], [(28., 29.)])

    def test_slivers_left_after_trimming_are_dropped(self):
        before = moment(source='a', clip_wall=100., start=20., end=29.5, recorded_at=130.)
        out = context.resolve([before, moment(lead=[lead('a', 100., 22., 30.)])])
        self.assertEqual(out[1]['lead'], [])


class LookbackJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.cache = Path(self.temp.name)/'cache'; self.cache.mkdir()
        self.calls = []

    def tearDown(self): self.temp.cleanup()

    def ask(self, w, m, stop):
        self.calls.append(m['id'])
        return {'beats': [beat(w['core'][0]-3, w['core'][0]-1, link=2)], 'note': '',
                '_meta': {'input_tokens': 4000, 'output_tokens': 300, 'thinking_tokens': 600}}

    def run_once(self, cands, keep, ask=None, **kw):
        return job.lookback(cands, keep, [A, B], self.cache, self.temp.name, ask_fn=ask or self.ask, workers=1, **kw)

    def test_kept_funny_moments_get_setup_and_answers_are_cached(self):
        funny, skilled, cut = moment(), moment(start=26., end=29., lenses=['skill']), moment(start=5., end=9.)
        usage = self.run_once([funny, skilled, cut], {funny['id'], skilled['id']})
        self.assertEqual(self.calls, [funny['id']])
        self.assertEqual((funny['lead_status'], skilled['lead_status']), ('found', 'none'))
        self.assertEqual([(s['source'], s['start'], s['end']) for s in funny['lead']], [('b', 11.5, 14.5)])
        self.assertEqual((usage['asked'], usage['cached']), (1, 0)); self.assertGreater(usage['usd'], 0)
        again = moment()
        usage = self.run_once([again], {again['id']})
        self.assertEqual((usage['asked'], usage['cached'], len(self.calls)), (0, 1, 1))
        self.assertEqual(again['lead'], funny['lead'])

    def test_out_of_money_stops_asking_but_keeps_the_review(self):
        def broke(w, m, stop):
            self.calls.append(m['id']); raise gemini.SpendLimit('spending cap')
        one, two = moment(), moment(source='a', clip_wall=100., start=20., end=26., recorded_at=130.)
        usage = self.run_once([one, two], {one['id'], two['id']}, ask=broke)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted([one['lead_status'], two['lead_status']]), ['unanalyzed', 'unanalyzed'])
        self.assertEqual(usage['asked'], 0)

    def test_a_blocked_project_stops_asking(self):
        class Denied(Exception):
            code = 403
        def denied(w, m, stop):
            self.calls.append(m['id']); raise Denied('403 PERMISSION_DENIED. Your project has been denied access.')
        one, two = moment(), moment(source='a', clip_wall=100., start=20., end=26., recorded_at=130.)
        self.run_once([one, two], {one['id'], two['id']}, ask=denied)
        self.assertEqual(len(self.calls), 1)

    def test_a_failed_ask_costs_only_that_moment_its_setup(self):
        def boom(w, m, stop): raise RuntimeError('500')
        m = moment()
        usage = self.run_once([m], {m['id']}, ask=boom)
        self.assertEqual((m['lead_status'], m['lead'], usage['failed']), ('failed', [], 1))

    def test_the_same_moment_listed_twice_is_asked_once(self):
        m = moment()
        usage = self.run_once([m, dict(m)], {m['id']})
        self.assertEqual((usage['asked'], len(self.calls)), (1, 1))

    def test_budget_stops_new_asks_before_the_cap(self):
        ms = [moment(start=s, end=s+3) for s in (12., 18., 24.)]
        usage = self.run_once(ms, {m['id'] for m in ms}, budget_usd=.02)   # Each ask holds back $0.015 while in flight.
        self.assertEqual((usage['asked'], [m['lead_status'] for m in ms]), (1, ['found', 'unanalyzed', 'unanalyzed']))

    def test_a_paid_answer_counts_even_if_saving_it_fails(self):
        m = moment()
        with patch('clipcompiler.job.atomic_json', side_effect=OSError('disk full')):
            usage = self.run_once([m], {m['id']})
        self.assertEqual(usage['asked'], 1); self.assertGreater(usage['usd'], 0)

    def test_cache_only_never_asks(self):
        m = moment()
        self.run_once([m], {m['id']}, cache_only=True)
        self.assertEqual((self.calls, m['lead_status']), ([], 'unanalyzed'))


class ProxyTests(unittest.TestCase):
    def test_stitched_proxy_has_the_window_timeline_and_mixes_every_track(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            a, b = clip(d/'a.mp4', seconds=6, tracks=2), clip(d/'b.mp4', seconds=6, tracks=2, frequency=550)
            recs = [dict(A, path=str(a), wall=100., duration=6.), dict(B, path=str(b), wall=104., duration=6.)]
            w = context.window(moment(clip_wall=104., start=3., end=5.), recs)   # Look back to wall 77: from A's start.
            self.assertEqual([(p['digest'], p['start'], p['end']) for p in w['pieces']], [('a', 0., 4.), ('b', 0., 5.)])
            out = context.proxy(w, d)
            meta = probe(out)
            self.assertAlmostEqual(meta['duration'], 9., delta=.25); self.assertEqual(meta['audio_count'], 1)


if __name__ == '__main__':
    unittest.main()
