"""The skill formula encodes the intended taste: golden orderings from feature fixtures, plus properties
(monotone in difficulty, penalties never help, bounded, tolerant of junk)."""
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from clipcompiler import skill

FIXTURES = Path(__file__).parent/'fixtures'/'skill'


def case(name):
    data = json.loads((FIXTURES/f'{name}.json').read_text(encoding='utf-8'))
    return data['features'], data['game']


def scored(name, preset='balanced'):
    features, game = case(name)
    return skill.score(features, skill.load_profile(skill.profile_name(game), preset))


def valued(name, preset='balanced'):
    features, game = case(name)
    return skill.value(features, skill.load_profile(skill.profile_name(game), preset))


class GoldenOrderingTests(unittest.TestCase):
    def assertOutranks(self, better, worse, preset='balanced'):
        a, b = valued(better, preset), valued(worse, preset)   # Unrounded, so near-ties still order correctly.
        self.assertGreater(a, b, f'{better} ({a:.3f}) should outrank {worse} ({b:.3f}) with preset {preset}')

    def test_flick_one_tap_headshot_beats_a_sloppy_two_kill_spray(self):
        self.assertOutranks('cs2_flick_one_tap_headshot', 'cs2_sloppy_two_kill_spray')
        self.assertGreaterEqual(scored('cs2_flick_one_tap_headshot'), 7)   # Makes the default cut...
        self.assertIn(scored('cs2_sloppy_two_kill_spray'), range(3, 6))    # ...the spray is still two kills: mid, not junk.

    def test_two_kills_beat_one_of_the_same_kind_and_clean_beats_sloppy(self):
        self.assertOutranks('cs2_sloppy_two_kill_spray', 'cs2_single_plain_kill')
        self.assertOutranks('cs2_clean_two_headshots', 'cs2_sloppy_two_kill_spray')

    def test_an_ace_scores_high_and_a_clean_ace_scores_top(self):
        self.assertGreaterEqual(scored('cs2_ace_easy_kills'), 7)
        self.assertEqual(scored('cs2_clean_ace'), 10)
        self.assertOutranks('cs2_clean_ace', 'cs2_ace_easy_kills')

    def test_a_1v3_clutch_beats_three_kills_on_unaware_enemies(self):
        self.assertOutranks('cs2_1v3_clutch_average_aim', 'cs2_three_unaware_from_behind')

    def test_an_ace_of_easy_kills_does_not_beat_a_flick_into_a_clutch(self):
        self.assertOutranks('cs2_flick_plus_1v2_clutch', 'cs2_ace_easy_kills')

    def test_rust_npc_kills_stay_below_the_default_threshold(self):
        self.assertLess(scored('rust_npc_kills'), 7)
        self.assertOutranks('rust_outnumbered_long_range_bow', 'rust_npc_kills')

    def test_an_exit_frag_scores_below_the_same_kill_in_a_live_round(self):
        self.assertOutranks('cs2_same_kill_live_round', 'cs2_exit_frag')

    def test_presets_shift_the_balance_the_intended_way(self):
        # Aim-focused: one great shot beats a one-kill clutch won on situation alone.
        self.assertOutranks('cs2_flick_one_tap_headshot', 'cs2_1v1_low_hp_plain_kill', 'aim')
        aim_gap = valued('cs2_flick_one_tap_headshot', 'aim') - valued('cs2_1v1_low_hp_plain_kill', 'aim')
        clutch_gap = valued('cs2_flick_one_tap_headshot', 'clutch') - valued('cs2_1v1_low_hp_plain_kill', 'clutch')
        self.assertGreater(aim_gap, clutch_gap)   # The clutch closes the gap when situation counts most...
        self.assertOutranks('cs2_1v3_clutch_average_aim', 'cs2_flick_one_tap_headshot', 'clutch')   # ...and a 1v3 wins.

    def test_every_fixture_scores_within_1_to_10(self):
        for f in FIXTURES.glob('*.json'):
            for preset in skill.PRESETS:
                self.assertIn(scored(f.stem, preset), range(1, 11))


class PropertyTests(unittest.TestCase):
    profile = skill.load_profile('cs2')
    base = case('cs2_same_kill_live_round')[0]

    def test_adding_any_penalty_flag_never_raises_the_score(self):
        for features in (case(f.stem)[0] for f in FIXTURES.glob('*.json')):
            before = skill.score(features, self.profile)
            for flag in skill.PENALTIES:
                worse = dict(features, penalty_flags=list(set(features['penalty_flags']) | {flag}))
                self.assertLessEqual(skill.score(worse, self.profile), before, flag)

    def test_raising_any_difficulty_bucket_never_lowers_the_score(self):
        ladders = {'flick': ['none', 'small', 'medium', 'large'], 'reaction': ['slow', 'normal', 'fast'],
                   'range': ['close', 'mid', 'long'], 'shots': ['spray', 'burst', 'one_tap'], 'headshot': ['no', 'yes']}
        for field, ladder in ladders.items():
            values = []
            for value in ladder:
                features = copy.deepcopy(self.base); features['kills'][0][field] = value
                values.append(skill.score(features, self.profile))
            self.assertEqual(values, sorted(values), field)

    def test_more_enemies_in_a_clutch_never_lowers_the_score(self):
        values = []
        for enemies in range(1, 6):
            features = copy.deepcopy(self.base); features['situation'].update(teammates_alive=0, enemies_alive=enemies)
            values.append(skill.score(features, self.profile))
        self.assertEqual(values, sorted(values))

    def test_each_extra_kill_adds_more_than_the_one_before(self):
        def kills(n, kill):
            return skill.components(dict(self.base, kills=[kill]*n), self.profile)[2]
        for kill in (self.base['kills'][0], case('cs2_sloppy_two_kill_spray')[0]['kills'][0]):
            gains = [kills(n+1, kill) - kills(n, kill) for n in range(1, 4)]
            self.assertTrue(all(g > 0 for g in gains))
            self.assertEqual(gains, sorted(gains))   # 2K < 3K < 4K, each step bigger.

    def test_more_kills_never_lower_the_score(self):
        for name in ('cs2_same_kill_live_round', 'cs2_sloppy_two_kill_spray', 'cs2_three_unaware_from_behind'):
            features = case(name)[0]; one = features['kills'][0]
            values = [skill.value(dict(features, kills=[one]*n), self.profile) for n in range(1, 6)]
            self.assertEqual(values, sorted(values), name)

    def test_a_clean_kill_counts_at_least_as_much_as_a_sloppy_one(self):
        sloppy = case('cs2_sloppy_two_kill_spray')[0]['kills'][0]; clean = self.base['kills'][0]
        self.assertLessEqual(skill.kill_count_value([skill.kill_difficulty(sloppy, self.profile)]*2, self.profile),
                             skill.kill_count_value([skill.kill_difficulty(clean, self.profile)]*2, self.profile))

    def test_junk_and_missing_fields_degrade_to_neutral_instead_of_crashing(self):
        for features in ({}, {'kills': None}, {'kills': ['x', {'headshot': 'maybe', 'flick': 7}]},
                         {'situation': {'teammates_alive': 'zero'}}, {'penalty_flags': ['made_up']},
                         {'holistic_difficulty': True}):
            self.assertIn(skill.score(features, self.profile), range(1, 11))
        self.assertEqual(skill.score({}, self.profile), 1)

    def test_tags_explain_the_score(self):
        strengths, drawbacks = skill.tags(case('cs2_flick_plus_1v2_clutch')[0])
        for tag in ('2K', '1v2', '2 headshots', 'Big flick', 'Fast reaction', 'One-tap', 'Decisive'):
            self.assertIn(tag, strengths)
        self.assertEqual(drawbacks, [])
        self.assertEqual(skill.tags(case('rust_npc_kills')[0])[1], ['NPC/bot'])


class ProfileTests(unittest.TestCase):
    def test_game_maps_to_a_profile_and_the_override_wins(self):
        self.assertEqual([skill.profile_name(g) for g in ('cs2', 'rust', 'other', None)], ['cs2', 'rust', 'generic', 'generic'])
        self.assertEqual(skill.profile_name('cs2', 'rust'), 'rust')
        self.assertEqual(skill.load_profile('valorant')['name'], 'generic')

    def test_every_profile_has_the_same_shape(self):
        shapes = []
        for f in skill.PROFILE_DIR.glob('*.json'):
            p = json.loads(f.read_text(encoding='utf-8'))
            shapes.append({k: sorted(v) if isinstance(v, dict) else None for k, v in p.items() if k not in ('name', 'label', 'hints')})
        for a, b in itertools.combinations(shapes, 2):
            self.assertEqual(a, b)

    def test_a_user_copy_overrides_only_what_it_sets_and_a_broken_one_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'cs2.json').write_text(json.dumps({'kill': {'headshot': 0.5}}), encoding='utf-8')
            p = skill.load_profile('cs2', user_dir=d)
            self.assertEqual((p['kill']['headshot'], p['kill']['flick']['large']), (0.5, 0.3))
            (Path(d)/'rust.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(skill.load_profile('rust', user_dir=d)['name'], 'rust')

    def test_preset_replaces_the_weights(self):
        self.assertEqual(skill.load_profile('cs2', 'aim')['weights'], skill.PRESET_WEIGHTS['aim'])
        self.assertEqual(skill.load_profile('cs2', 'balanced')['weights'], {'mechanics': .6, 'situation': .3, 'multi': 1.0})


if __name__ == '__main__':
    unittest.main()
