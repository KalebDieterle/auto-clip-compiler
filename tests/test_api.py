"""The UI bridge end to end without a window: onboarding, scan, judge (fake), review edits, previews, render."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import keyring

from clipcompiler.ui.api import Api, PREVIEW_PAD
from clipcompiler.ui.server import Server
from tests.helpers import clip, judgment
from tests.test_settings import MemoryKeyring, FAKE_KEY


class ApiFlowTests(unittest.TestCase):
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
        self.judged = []
        def fake(path, stop):
            self.judged.append(path.name)
            return judgment((1, 4, 8), (5, 7.5, 5))
        self.api = Api(judge_fn=fake)
        self.out = Path(self.home.name)/'out'

    def tearDown(self):
        keyring.set_keyring(self.previous); self.env.stop(); self.home.cleanup()

    def wait(self, kind, timeout=60):
        """Drain events until one of type `kind` arrives; fail fast on an error event."""
        deadline, seen = time.monotonic()+timeout, 0
        while time.monotonic() < deadline:
            batch = self.api.events(seen)['events']
            for e in batch:
                seen = e['seq']
                if e['type'] == 'error': self.fail(e['message'])
                if e['type'] == kind: return e
            time.sleep(.05)
        self.fail(f'no {kind} event')

    def wait_idle(self):
        deadline = time.monotonic()+30
        while self.api.events(10**9)['busy'] and time.monotonic() < deadline: time.sleep(.05)

    def onboard(self):
        self.assertEqual(self.api.set_key(FAKE_KEY, False)['masked'][-4:], 'xxxx')
        self.api.save_settings({'source': str(self.source), 'output': str(self.out), 'onboarded': True,
                                'mode': 'parts', 'min_score': 7})

    def judge(self):
        self.onboard()
        self.assertTrue(self.api.scan()['ok'])
        done = self.wait('scan_done'); self.wait_idle()
        self.assertEqual((done['clips'], done['to_judge']), (3, 3))
        self.assertAlmostEqual(done['estimate_usd'], 0.01, delta=.01)
        self.assertTrue(self.api.start_judge()['ok'])
        ready = self.wait('review_ready'); self.wait_idle()
        return ready

    def test_state_never_exposes_the_key(self):
        self.onboard()
        state = self.api.get_state()
        self.assertTrue(state['key']['present']); self.assertNotIn(FAKE_KEY, json.dumps(state))

    def test_full_flow_scan_judge_review_render(self):
        ready = self.judge()
        self.assertEqual((ready['moments'], len(self.judged)), (3, 3))  # Score-5 moments are below 7.
        review = self.api.get_review()
        self.assertEqual([m['keep'] for m in review['moments']], [True]*3)
        self.assertEqual(review['totals']['kept'], 3)
        first = review['moments'][0]
        cut = self.api.set_decision(first['id'], False)
        self.assertEqual(cut['totals']['kept'], 2)
        trimmed = self.api.set_decision(review['moments'][1]['id'], None, -50, 99)['moment']
        self.assertEqual((trimmed['start'], trimmed['end']), (0, 7))  # Clamped to the 3 s pad and clip end.
        self.assertTrue(self.api.start_render()['ok'])
        done = self.wait('render_done'); self.wait_idle()
        self.assertEqual(len(done['files']), 1); self.assertTrue(Path(done['files'][0]).exists())
        run = json.loads((Path(self.api.get_state()['run']['folder'])/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(run['status'], 'rendered')
        self.assertFalse(run['decisions'][first['id']]['keep'])

    def test_discarding_a_draft_recycles_its_folder_and_keeps_judgments(self):
        self.judge()
        drafts = self.api.get_state()['drafts']
        self.assertEqual([d['status'] for d in drafts], ['review'])
        folder = Path(drafts[0]['folder'])
        with patch('clipcompiler.ui.api.recycle', side_effect=shutil.rmtree) as recycled:
            self.assertTrue(self.api.discard_run(str(folder))['ok'])
        recycled.assert_called_once_with(folder)
        state = self.api.get_state()
        self.assertFalse(folder.exists()); self.assertIsNone(state['run']); self.assertEqual(state['drafts'], [])
        fresh = Api(judge_fn=lambda p, s: self.fail('re-judged'))
        fresh.scan(); e = None
        deadline = time.monotonic()+30
        while not e and time.monotonic() < deadline:
            e = next((x for x in fresh.events(0)['events'] if x['type'] == 'scan_done'), None); time.sleep(.05)
        self.assertEqual((e['cached'], e['to_judge']), (3, 0))  # Judgments live in the cache, not the draft.

    def test_discard_refuses_rendered_runs_and_folders_outside_the_output(self):
        self.judge()
        self.api.start_render(); self.wait('render_done'); self.wait_idle()
        folder = self.api.get_state()['run']['folder']
        self.assertEqual(self.api.get_state()['drafts'], [])
        with patch('clipcompiler.ui.api.recycle') as recycled:
            self.assertIn('error', self.api.discard_run(folder))
            stray = Path(self.home.name)/'Compilation elsewhere'; stray.mkdir(); (stray/'run.json').write_text('{}')
            self.assertIn('error', self.api.discard_run(str(stray)))
            self.assertIn('error', self.api.discard_run(str(self.out)))
        recycled.assert_not_called()

    def test_rescan_after_judging_costs_nothing_and_reopening_keeps_decisions(self):
        self.judge()
        review = self.api.get_review(); mid = review['moments'][2]['id']
        self.api.set_decision(mid, False)
        folder = self.api.get_state()['run']['folder']
        fresh = Api(judge_fn=lambda p, s: self.fail('re-judged'))
        reopened = fresh.open_run(folder)
        self.assertFalse(next(m for m in reopened['moments'] if m['id'] == mid)['keep'])
        self.assertEqual(fresh.get_state()['recent'][0]['folder'], folder)
        fresh.scan(); e = None
        deadline = time.monotonic()+30
        while not e and time.monotonic() < deadline:
            e = next((x for x in fresh.events(0)['events'] if x['type'] == 'scan_done'), None); time.sleep(.05)
        self.assertEqual((e['cached'], e['to_judge'], e['estimate_usd']), (3, 0, 0))

    def test_history_keeps_runs_after_the_output_folder_changes(self):
        self.judge()
        folder = self.api.get_state()['run']['folder']
        older = Path(self.home.name)/'Compilation 2026-10-01 12-00-00'; older.mkdir()  # Next to the output folder.
        (older/'run.json').write_text(json.dumps({'created': '2026-10-01T12:00:00', 'status': 'rendered', 'moments': [],
                                                  'mode': 'reel', 'usd_paid': 0.5}), encoding='utf-8')
        runs = self.api.history()['runs']
        self.assertEqual([r['folder'] for r in runs], [folder, str(older)])
        self.assertEqual(runs[1]['usd'], 0.5)
        elsewhere = Path(self.home.name)/'new-out'; elsewhere.mkdir()
        self.api.save_settings({'output': str(elsewhere)})
        fresh = Api(judge_fn=lambda p, s: self.fail('re-judged'))
        self.assertEqual(fresh.get_state()['recent'], [])
        self.assertEqual([r['folder'] for r in fresh.history()['runs']], [folder, str(older)])
        with patch('clipcompiler.ui.api.os.startfile', create=True) as opened:
            self.assertTrue(fresh.open_path(str(older))['ok'])
        opened.assert_called_once()
        self.assertEqual(fresh.open_run(folder)['run']['folder'], folder)

    def test_trim_keeps_a_minimum_length_and_stays_inside_the_preview_window(self):
        self.judge()
        m = self.api.get_review()['moments'][0]
        r = self.api.set_decision(m['id'], None, 3.9, None)['moment']
        self.assertEqual((r['start'], r['end']), (3.9, 4.9))  # The edited edge wins; the other keeps 1 s away.
        r = self.api.set_decision(m['id'], None, None, 0.2)['moment']
        self.assertGreaterEqual(r['end']-r['start'], 1.0); self.assertGreaterEqual(r['start'], max(0, 1-PREVIEW_PAD))

    def test_previews_and_thumbnails_are_cached_and_served_with_token(self):
        self.judge()
        server = Server(Path(__file__).parent, Path(self.home.name)/'previews').start()
        self.api._server = server
        try:
            m = self.api.get_review()['moments'][0]
            url = self.api.preview(m['id'])['url']; thumb = self.api.thumbnail(m['id'])['url']
            import urllib.request, urllib.error
            req = urllib.request.Request(url, headers={'Range': 'bytes=0-99'})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual((resp.status, len(resp.read())), (206, 100))
            with urllib.request.urlopen(thumb) as resp:
                self.assertEqual(resp.headers['Content-Type'], 'image/jpeg')
            for bad in (url.split('?')[0]+'?t=wrong', f'{server.url}/media/..%2F..%2Fsettings.json?t={server.token}'):
                with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(bad)
            before = Path(self.home.name, 'previews').stat().st_mtime_ns
            self.assertEqual(self.api.preview(m['id'])['url'], url)
        finally:
            server.stop()

    def test_one_job_at_a_time_and_guarded_side_effects(self):
        self.judge()
        self.assertTrue(self.api.start_render()['ok'])
        self.assertIn('Already', self.api.scan()['error'])
        self.wait('render_done'); self.wait_idle()
        self.assertIn('error', self.api.open_url('https://evil.example/'))
        self.assertIn('error', self.api.open_path(r'C:\Windows'))

    def test_missing_key_blocks_paid_judging(self):
        self.onboard(); self.api.clear_key()
        self.api.scan(); self.wait('scan_done'); self.wait_idle()
        self.assertIn('API key', self.api.start_judge()['error'])


if __name__ == '__main__':
    unittest.main()
