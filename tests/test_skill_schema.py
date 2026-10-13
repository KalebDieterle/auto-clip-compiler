"""Skill schemas, triage gating, stage-B windows, window-to-clip time mapping, cache records and moments."""
from pathlib import Path
import tempfile
import unittest

from clipcompiler import batch, skill
from clipcompiler.media import probe, window
from tests.helpers import clip

CS2 = skill.load_profile('cs2')


def triage(*plays, game='cs2'):
    return {'game': game, 'summary': 's', 'plays': [dict(start=s, end=e, kills=k, preliminary=p, what=f'play {s}')
                                                     for s, e, k, p in plays]}


def detail(start=1, end=4, kills=((2, 'yes'),), holistic=8):
    return {'start': start, 'end': end, 'kills': [dict(t=t, headshot=h, flick='large', reaction='fast', range='mid',
                                                       target_moving=True, shots='one_tap', special=[]) for t, h in kills],
            'situation': dict(teammates_alive=1, enemies_alive=2, low_hp=False, round_decisive=False, outnumbered=False,
                              enemies_aware=True),
            'penalty_flags': [], 'evidence': 'kill feed', 'holistic_difficulty': holistic, 'one_liner': 'flick'}


class SchemaTests(unittest.TestCase):
    def test_rest_conversion_uppercases_types_and_keeps_enum_values(self):
        rest = batch.rest_schema(skill.DETAIL_SCHEMA)
        kill = rest['properties']['kills']['items']
        self.assertEqual((rest['type'], kill['type'], kill['properties']['flick']['type']), ('OBJECT', 'OBJECT', 'STRING'))
        self.assertEqual(kill['properties']['flick']['enum'], ['none', 'small', 'medium', 'large'])
        self.assertEqual(batch.rest_schema(skill.TRIAGE_SCHEMA)['properties']['game']['enum'], list(skill.GAMES))

    def test_every_required_field_is_a_declared_property(self):
        def walk(schema):
            if schema.get('type') == 'object':
                self.assertLessEqual(set(schema['required']), set(schema['properties']))
                for value in schema['properties'].values(): walk(value)
            if schema.get('type') == 'array': walk(schema['items'])
        walk(skill.TRIAGE_SCHEMA); walk(skill.DETAIL_SCHEMA)

    def test_detail_prompt_names_the_profile_panel_and_hints(self):
        text = skill.detail_prompt(CS2)
        self.assertIn('kill feed (top-right corner)', text); self.assertIn('Counter-Strike 2', text)
        self.assertNotIn('{', text)


class GateTests(unittest.TestCase):
    def test_promising_or_kill_plays_pass_best_first_capped(self):
        t = triage((0, 3, 0, 2), (4, 8, 1, 3), (10, 14, 0, 6), (15, 18, 2, 8), (20, 25, 3, 9), (26, 28, 1, 5))
        chosen = skill.plays_to_review(t, CS2)
        self.assertEqual([p['start'] for p in chosen], [20, 15, 10])   # Cap 3; the 0-kill prelim-2 play never passes.

    def test_slivers_bad_rows_and_out_of_clip_times_are_handled(self):
        t = triage((5, 5.2, 1, 9), (28, 40, 1, 7)); t['plays'].append({'start': 'x'})
        chosen = skill.plays_to_review(t, CS2, duration=30)
        self.assertEqual([(p['start'], p['end']) for p in chosen], [(28, 30)])
        self.assertEqual(skill.plays_to_review(None, CS2), [])

    def test_window_pads_clamps_and_keeps_the_end_of_long_plays(self):
        self.assertEqual(skill.window_bounds({'start': 5, 'end': 8}, 30), (3, 9))
        self.assertEqual(skill.window_bounds({'start': 1, 'end': 29.5}, 30), (22, 30))
        self.assertEqual(skill.window_bounds({'start': .5, 'end': 2}, 30), (0, 3))


class TimeMappingTests(unittest.TestCase):
    play = {'start': 12, 'end': 15}

    def test_window_relative_times_become_clip_times(self):
        out = skill.to_clip_time(detail(1, 4, kills=((2.5, 'yes'),)), (10, 16), self.play)
        self.assertEqual((out['start'], out['end'], out['kills'][0]['t']), (11, 14, 12.5))

    def test_times_beyond_the_window_are_clamped(self):
        out = skill.to_clip_time(detail(-3, 99, kills=((50, 'yes'), (-1, 'no'))), (10, 16), self.play)
        self.assertEqual((out['start'], out['end'], [k['t'] for k in out['kills']]), (10, 16, [16, 10]))

    def test_end_before_start_falls_back_to_the_triage_play(self):
        out = skill.to_clip_time(detail(4, 2), (10, 16), self.play)
        self.assertEqual((out['start'], out['end']), (12, 15))
        out = skill.to_clip_time({'kills': 'junk'}, (10, 16), self.play)
        self.assertEqual((out['start'], out['end'], out['kills']), (12, 15, []))


class RecordAndMomentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.cache = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def test_records_live_in_their_own_folder_and_track_pending_plays(self):
        t = triage((2, 6, 1, 7), (10, 14, 2, 8)); record = skill.new_record(t)
        skill.save_record(self.cache, 'abc', record)
        self.assertTrue((self.cache/'skill'/'abc.json').exists()); self.assertEqual(list(self.cache.glob('*.json')), [])
        self.assertEqual(len(skill.pending_plays(record, CS2)), 2); self.assertFalse(skill.complete(record, CS2))
        for p in skill.plays_to_review(t, CS2): record['plays'][skill.play_id(p)] = detail()
        self.assertTrue(skill.complete(skill.load_record(self.cache, 'abc') and record, CS2))
        self.assertFalse(skill.is_stale(record)); self.assertTrue(skill.is_stale(dict(record, version='skill-v0')))
        (self.cache/'skill'/'bad.json').write_text('{', encoding='utf-8')
        self.assertIsNone(skill.load_record(self.cache, 'bad')); self.assertIsNone(skill.load_record(self.cache, 'none'))

    def test_moments_carry_score_tags_lens_and_wall_time(self):
        t = triage((2, 6, 1, 7), (10, 14, 2, 8)); record = skill.new_record(t)
        p = skill.plays_to_review(t, CS2)[0]   # Only the best play has a close look so far.
        record['plays'][skill.play_id(p)] = skill.to_clip_time(detail(1, 4), skill.window_bounds(p, 30), p)
        meta = {'duration': 30., 'audio_stream': 1}
        found = list(skill.moments_for(Path('c.mp4'), 1000., True, record, meta, 'abc', CS2))
        self.assertEqual(len(found), 1); m = found[0]
        self.assertEqual((m['lenses'], m['start'], m['end'], m['clip_wall'], m['game']), (['skill'], 9, 12, 970., 'cs2'))
        self.assertEqual(m['score'], m['skill_score']); self.assertIn('Headshot', m['skill_tags'])
        self.assertEqual(m['reason'], 'flick'); self.assertEqual(m['kills'], [10])


class WindowMediaTests(unittest.TestCase):
    """Real ffmpeg on a tiny synthetic clip."""
    def test_composite_has_the_layout_rate_length_and_audio(self):
        with tempfile.TemporaryDirectory() as d:
            source = clip(Path(d)/'a.mp4', seconds=6)
            for profile in (CS2, skill.load_profile('rust'), skill.load_profile('generic')):
                out = window(source, 1., 4.5, profile['panel'], d, fps=skill.WINDOW_FPS)
                meta = probe(out)
                self.assertEqual((meta['width'], meta['height'], meta['fps']), (1280, 480, skill.WINDOW_FPS))
                self.assertAlmostEqual(meta['duration'], 3.5, delta=.15)
                self.assertIsNotNone(meta['audio_stream'])

    def test_window_at_the_clip_end_is_cut_short_not_padded(self):
        with tempfile.TemporaryDirectory() as d:
            source = clip(Path(d)/'a.mp4', seconds=4)
            w0, w1 = skill.window_bounds({'start': 3, 'end': 3.9}, 4.)
            out = window(source, w0, w1, CS2['panel'], d)
            self.assertAlmostEqual(probe(out)['duration'], w1-w0, delta=.15)


if __name__ == '__main__':
    unittest.main()
