"""Batch mode through the UI bridge, against the fake Google service: submit, wait, check, collect,
review, render; app restarts, cancel and resume. No network, no window."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import keyring

from clipcompiler import batch
from clipcompiler.ui.api import Api
from tests.fake_gemini import FakeGemini
from tests.helpers import clip
from tests.test_settings import FAKE_KEY, MemoryKeyring


class ApiBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.source = cls.root/'clips'; cls.source.mkdir()
        for i in range(3):
            clip(cls.source/f'2026-10-0{i+1} 20-00-10.mp4', seconds=8, frequency=300+100*i)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ACC_HOME': self.home.name}); self.env.start()
        self.previous = keyring.get_keyring(); keyring.set_keyring(MemoryKeyring())
        self.google = FakeGemini(processing_polls=0); self.keys = []
        self.api = self.new_api()
        self.api.set_key(FAKE_KEY, False)
        self.api.save_settings({'source': str(self.source), 'output': str(Path(self.home.name)/'out'), 'onboarded': True,
                                'mode': 'parts', 'judge_mode': 'batch'})

    def new_api(self):
        def factory(key):
            self.keys.append(key); return self.google
        return Api(judge_fn=lambda p, s: self.fail('live judging used in batch mode'), client_factory=factory)

    def tearDown(self):
        keyring.set_keyring(self.previous); self.env.stop(); self.home.cleanup()

    def wait(self, api, kind, timeout=60):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            for e in api.events(0)['events']:
                if e['type'] == 'error': self.fail(e['message'])
                if e['type'] == kind: return e
            time.sleep(.05)
        self.fail(f'no {kind} event')

    def idle(self, api):
        deadline = time.monotonic()+30
        while api.events(10**9)['busy'] and time.monotonic() < deadline: time.sleep(.05)

    def submit(self):
        self.api.scan(); done = self.wait(self.api, 'scan_done'); self.idle(self.api)
        self.assertAlmostEqual(done['estimate_batch_usd'], done['estimate_usd']/2, delta=.01)
        self.assertTrue(self.api.start_judge()['ok'])
        waiting = self.wait(self.api, 'batch_waiting'); self.idle(self.api)
        return waiting

    def test_submit_wait_check_collect_review_render(self):
        waiting = self.submit()
        self.assertEqual((waiting['batch']['clips'], waiting['batch']['status']), (3, 'JOB_STATE_PENDING'))
        self.assertTrue(waiting['batch']['expires'])
        self.assertEqual(set(self.keys), {FAKE_KEY})  # The client got the stored key.
        state = self.api.get_state()
        self.assertEqual(state['run']['status'], 'batch_waiting')
        self.assertNotIn(FAKE_KEY, json.dumps(state))
        job_name = waiting['batch']['job']
        self.google.batches.jobs[job_name]['state'] = 'JOB_STATE_RUNNING'
        r = self.api.check_batch()
        self.assertEqual((r['status'], r['done']), ('JOB_STATE_RUNNING', False))
        self.google.batches.finish(job_name)
        r = self.api.check_batch()
        self.assertEqual((r['done'], r['collecting']), (True, True))
        ready = self.wait(self.api, 'review_ready'); self.idle(self.api)
        self.assertEqual(ready['moments'], 3)
        review = self.api.get_review()
        self.assertEqual(review['run']['status'], 'review')
        self.assertAlmostEqual(review['run']['usage']['usd'], 3*batch.PER_CLIP, delta=.001)
        self.assertEqual(review['run']['batch']['judged'], 3)
        self.assertEqual([n for n in self.google.files.store if not n.startswith('files/results')], [])
        self.assertTrue(self.api.start_render()['ok'])
        done = self.wait(self.api, 'render_done'); self.idle(self.api)
        self.assertTrue(Path(done['files'][0]).exists())

    def test_waiting_batch_survives_an_app_restart_and_is_checked_from_disk(self):
        waiting = self.submit()
        self.google.batches.finish(waiting['batch']['job'])
        reopened = self.new_api()  # The app was closed and opened again.
        recent = reopened.get_state()['recent'][0]
        self.assertEqual((recent['status'], recent['judge_mode']), ('batch_waiting', 'batch'))
        r = reopened.check_batch(recent['folder'])
        self.assertTrue(r['collecting'])
        self.assertEqual(self.wait(reopened, 'review_ready')['moments'], 3); self.idle(reopened)

    def test_cancel_a_waiting_batch_deletes_uploads(self):
        waiting = self.submit()
        self.assertTrue(self.api.cancel_batch()['ok'])
        self.assertEqual(self.google.batches.cancelled, [waiting['batch']['job']])
        self.assertEqual(self.google.files.store, {})
        self.assertEqual(self.api.get_state()['run']['status'], 'cancelled')
        self.assertEqual(self.api.check_batch()['done'], False)

    def test_discarding_a_waiting_batch_cancels_it_at_google_first(self):
        waiting = self.submit()
        folder = self.api.get_state()['drafts'][0]['folder']
        with patch('clipcompiler.ui.api.recycle') as recycled:
            self.assertTrue(self.api.discard_run(folder)['ok'])
        self.assertEqual(self.google.batches.cancelled, [waiting['batch']['job']])
        self.assertEqual(self.google.files.store, {})
        recycled.assert_called_once_with(Path(folder))
        self.assertIsNone(self.api.get_state()['run'])

    def test_failed_batch_still_opens_review_and_says_why(self):
        waiting = self.submit()
        self.google.batches.finish(waiting['batch']['job'], 'JOB_STATE_EXPIRED')
        self.api.check_batch()
        ready = self.wait(self.api, 'review_ready'); self.idle(self.api)
        self.assertEqual((ready['moments'], ready['stopped']), (0, 'batch_expired'))
        self.assertEqual(self.google.files.store, {})

    def test_interrupted_upload_resumes(self):
        self.api.scan(); self.wait(self.api, 'scan_done'); self.idle(self.api)
        self.api.start_judge(); self.api.cancel()
        self.idle(self.api)
        self.assertEqual(self.api.get_state()['run']['status'], 'cancelled')
        self.assertTrue(self.api.resume_batch()['ok'])
        waiting = self.wait(self.api, 'batch_waiting'); self.idle(self.api)
        self.assertEqual(waiting['batch']['clips'], 3)

    def test_everything_already_judged_skips_google_entirely(self):
        waiting = self.submit()
        self.google.batches.finish(waiting['batch']['job']); self.api.check_batch()
        self.wait(self.api, 'review_ready'); self.idle(self.api)
        calls = len(self.google.calls)
        self.api.scan(); self.idle(self.api)
        self.assertTrue(self.api.start_judge()['ok'])
        self.idle(self.api)
        self.assertEqual(self.api.get_state()['run']['status'], 'review')
        self.assertEqual(len(self.google.calls), calls)

    def test_clips_waiting_in_a_batch_are_never_sent_twice(self):
        self.submit()
        self.api.scan(); done = self.wait_latest(self.api, 'scan_done'); self.idle(self.api)
        self.assertEqual((done['to_judge'], done['in_batch'], done['estimate_batch_usd']), (0, 3, 0))
        self.api.save_settings({'judge_mode': 'live'})  # Not by live judging either.
        self.assertIn('already in a batch', self.api.start_judge()['error'])
        self.assertEqual(len(self.google.batches.jobs), 1)

    def wait_latest(self, api, kind, timeout=60):
        deadline, seen = time.monotonic()+timeout, api.events(0)['events'][-1]['seq']
        while time.monotonic() < deadline:
            for e in api.events(seen)['events']:
                if e['type'] == kind: return e
            time.sleep(.05)
        self.fail(f'no new {kind} event')

    def test_budget_cap_limits_clips_in_the_batch(self):
        self.api.save_settings({'budget_usd': round(batch.PER_CLIP*1.5, 5)})
        waiting = self.submit()
        self.assertEqual(waiting['batch']['clips'], 1)


if __name__ == '__main__':
    unittest.main()
