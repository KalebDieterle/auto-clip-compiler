"""Skill and Both compilations through the UI bridge: per-mode scan estimates, live judging with fakes,
review fields, and the chained two-stage batch against the fake Google service. No network, no window."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import keyring

from clipcompiler import gemini, skill
from clipcompiler.ui.api import Api
from tests.fake_gemini import FakeGemini
from tests.helpers import clip, judgment
from tests.test_batch import skill_judge
from tests.test_settings import FAKE_KEY, MemoryKeyring
from tests.test_skill_job import detail_result, triage_result


class SkillApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.source = cls.root/'clips'; cls.source.mkdir()
        for i in range(3):
            clip(cls.source/f'2026-10-0{i+1} 20-00-10.mp4', seconds=5, frequency=300+100*i)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ACC_HOME': self.home.name}); self.env.start()
        self.previous = keyring.get_keyring(); keyring.set_keyring(MemoryKeyring())
        self.calls, self.seen = [], 0
        self.google = FakeGemini(judge=skill_judge, processing_polls=0)
        self.api = self.make_api()
        self.api.set_key(FAKE_KEY, False)
        self.out = Path(self.home.name)/'out'

    def make_api(self):
        def funny(path, stop):
            self.calls.append('funny'); return judgment((.5, 3.5, 8))
        def skilled(kind, path, stop, profile, play, window):
            self.calls.append(kind)
            return triage_result() if kind == 'triage' else detail_result()
        return Api(judge_fn=funny, skill_fn=skilled, client_factory=lambda key: self.google)

    def tearDown(self):
        keyring.set_keyring(self.previous); self.env.stop(); self.home.cleanup()

    def settings(self, **changes):
        base = {'source': str(self.source), 'output': str(self.out), 'onboarded': True, 'mode': 'reel', 'target_minutes': 30}
        self.assertNotIn('error', self.api.save_settings({**base, **changes}))

    def wait(self, kind, timeout=60):
        """The next event of `kind` after the last one consumed; fails fast on an error event."""
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            for e in self.api.events(self.seen)['events']:
                self.seen = e['seq']
                if e['type'] == 'error': self.fail(e['message'])
                if e['type'] == kind: return e
            time.sleep(.05)
        self.fail(f'no {kind} event')

    def idle(self):
        deadline = time.monotonic()+30
        while self.api.events(10**9)['busy'] and time.monotonic() < deadline: time.sleep(.05)

    def scan(self):
        self.assertTrue(self.api.scan()['ok']); done = self.wait('scan_done'); self.idle()
        return done


class LiveSkillApiTests(SkillApiBase):
    def test_scan_estimates_every_compilation_type(self):
        self.settings(content_mode='skill')
        done = self.scan()
        est = done['estimates']
        self.assertEqual({m: est[m]['clips'] for m in est}, {'funny': 3, 'skill': 3, 'both': 3})
        self.assertLess(est['funny']['live'], est['skill']['live'])
        self.assertAlmostEqual(est['both']['live'], est['funny']['live'] + est['skill']['live'], delta=.011)
        self.assertEqual((est['funny']['approx'], est['skill']['approx']), (False, True))
        self.assertEqual((done['to_judge'], done['estimate_usd']), (3, est['skill']['live']))   # Headline = selected type.

    def test_skill_compilation_judges_reviews_and_explains(self):
        self.settings(content_mode='skill', min_skill_score=7)
        self.scan()
        self.assertTrue(self.api.start_judge()['ok'])
        ready = self.wait('review_ready'); self.idle()
        self.assertEqual(self.calls.count('funny'), 0); self.assertEqual(self.calls.count('triage'), 3)
        review = self.api.get_review()
        self.assertEqual(review['run']['content_mode'], 'skill')
        self.assertEqual(ready['moments'], len(review['moments']))
        m = review['moments'][0]
        self.assertEqual((m['lenses'], m['lens'], m['game']), (['skill'], 'skill', 'cs2'))
        self.assertEqual(m['score'], m['skill_score']); self.assertIn('Headshot', m['skill_tags'])
        self.assertTrue(m['kills'])
        report = json.loads((Path(review['run']['folder'])/'report.json').read_text(encoding='utf-8'))
        self.assertIn('features', report[0]['skill'])

    def test_a_rescan_after_judging_costs_nothing_for_that_type(self):
        self.settings(content_mode='both')
        self.scan(); self.api.start_judge(); self.wait('review_ready'); self.idle()
        done = self.scan()
        self.assertEqual({m: done['estimates'][m]['clips'] for m in done['estimates']}, {'funny': 0, 'skill': 0, 'both': 0})
        calls = len(self.calls)
        self.api.start_judge(); self.wait('review_ready'); self.idle()
        self.assertEqual(len(self.calls), calls)   # Everything came from the cache.

    def test_both_compilation_tags_moments_and_preselects_by_share(self):
        self.settings(content_mode='both', skill_share=.5, target_minutes=1)
        self.scan(); self.api.start_judge(); self.wait('review_ready'); self.idle()
        review = self.api.get_review()
        lenses = {tuple(m['lenses']) for m in review['moments']}
        self.assertIn(('funny', 'skill'), lenses)          # The same play judged both ways is one moment.
        both = next(m for m in review['moments'] if len(m['lenses']) == 2)
        self.assertEqual((both['funny_score'], both['skill_score']), (8, skill.score(detail_result(), skill.load_profile('cs2'))))
        self.assertTrue(both['skill_reason'])
        self.assertEqual(review['run']['skill_share'], .5)

    def test_funny_runs_from_before_the_update_still_open(self):
        self.settings()
        self.scan(); self.api.start_judge(); self.wait('review_ready'); self.idle()
        folder = Path(self.api.get_review()['run']['folder'])
        run = json.loads((folder/'run.json').read_text(encoding='utf-8'))
        for key in ('content_mode', 'min_skill_score', 'skill_share', 'skill_preset', 'game_override'): run.pop(key)
        for m in run['moments']:
            for key in ('lenses', 'funny_score'): m.pop(key, None)
        (folder/'run.json').write_text(json.dumps(run), encoding='utf-8')
        review = self.make_api().open_run(str(folder))
        self.assertEqual(review['run']['content_mode'], 'funny')
        self.assertEqual({tuple(m['lenses']) for m in review['moments']}, {('funny',)})

    def test_edit_profiles_seeds_examples_and_a_how_to(self):
        with patch('os.startfile', create=True) as opened:
            result = self.api.edit_profiles()
        folder = Path(result['folder'])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ['README.txt', 'cs2.default.json', 'generic.default.json', 'rust.default.json'])
        opened.assert_called_once()
        self.assertEqual(skill.load_profile('cs2', user_dir=folder)['kill']['headshot'], .3)   # Examples change nothing.


class BatchSkillApiTests(SkillApiBase):
    def make_api(self):
        return Api(judge_fn=lambda p, s: self.fail('live judging used in batch mode'),
                   skill_fn=lambda *a: self.fail('live judging used in batch mode'), client_factory=lambda key: self.google)

    def finish_current(self):
        job = self.api.get_state()['run']['batch']['job']
        self.google.batches.finish(job)
        r = self.api.check_batch(); self.assertEqual((r['done'], r['collecting']), (True, True))

    def test_skill_batch_chains_stage_two_then_opens_review(self):
        self.settings(content_mode='both', judge_mode='batch')
        self.scan()
        self.assertTrue(self.api.start_judge()['ok'])
        first = self.wait('batch_waiting'); self.idle()
        self.assertEqual((first['stage'], first['batch']['clips']), (1, 3))
        self.finish_current()
        second = self.wait('batch_waiting'); self.idle()   # Collected stage 1, uploaded the close-ups automatically.
        self.assertEqual((second['stage'], second['batch']['clips'], second['batch']['stage']), (2, 3, 2))
        self.assertEqual(self.api.get_state()['run']['stage'], 2)
        self.finish_current()
        ready = self.wait('review_ready'); self.idle()
        review = self.api.get_review()
        self.assertEqual(ready['moments'], len(review['moments']))
        self.assertTrue(any(len(m['lenses']) == 2 for m in review['moments']))
        # The fake bills every result line the same tokens: 3 funny + 3 triage + 3 close-ups, at half price.
        per_line = gemini.cost({'input_tokens': 2850, 'output_tokens': 200, 'thinking_tokens': 450})*gemini.BATCH_DISCOUNT
        self.assertAlmostEqual(review['run']['usage']['usd'], 9*per_line, places=4)
        self.assertEqual([n for n in self.google.files.store if not n.startswith('files/results')], [])

    def test_clips_waiting_in_stage_two_are_never_sent_again(self):
        self.settings(content_mode='skill', judge_mode='batch')
        self.scan(); self.api.start_judge(); self.wait('batch_waiting'); self.idle()
        self.finish_current(); self.wait('batch_waiting'); self.idle()
        done = self.scan()
        self.assertEqual((done['estimates']['skill']['in_batch'], done['estimates']['skill']['clips']), (3, 0))
        self.assertIn('error', self.api.start_judge())

    def test_cancelling_stage_two_deletes_its_uploads(self):
        self.settings(content_mode='skill', judge_mode='batch')
        self.scan(); self.api.start_judge(); self.wait('batch_waiting'); self.idle()
        self.finish_current(); self.wait('batch_waiting'); self.idle()
        self.assertTrue(self.api.cancel_batch()['ok'])
        self.assertEqual(self.api.get_state()['run']['status'], 'cancelled')
        self.assertEqual([n for n in self.google.files.store if not n.startswith('files/results')], [])

    def test_a_skill_batch_where_triage_found_nothing_goes_straight_to_review(self):
        self.google.judge = lambda key: {'game': 'other', 'summary': 'menu', 'plays': []}
        self.settings(content_mode='skill', judge_mode='batch')
        self.scan(); self.api.start_judge(); self.wait('batch_waiting'); self.idle()
        self.finish_current()
        ready = self.wait('review_ready'); self.idle()
        self.assertEqual(ready['moments'], 0)


if __name__ == '__main__':
    unittest.main()
