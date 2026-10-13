r"""Opt-in live check against the real Gemini API (costs about $0.01). Skipped unless ACC_LIVE=1.

  $env:ACC_LIVE = '1'; $env:GEMINI_API_KEY = '<key>'   # or a key saved in the app
  .\.venv\Scripts\python.exe -m unittest tests.test_live -v
"""
import os
from pathlib import Path
import tempfile
import unittest

from clipcompiler import gemini, job
from clipcompiler.reel import discover
from tests.helpers import clip


def key():
    if os.environ.get('GEMINI_API_KEY'): return os.environ['GEMINI_API_KEY']
    from clipcompiler import settings
    return settings.get_key()


@unittest.skipUnless(os.environ.get('ACC_LIVE') == '1', 'live Gemini test (set ACC_LIVE=1)')
class LiveGeminiTests(unittest.TestCase):
    def test_key_validates_and_two_clips_judge_cache_and_leave_no_uploads(self):
        from clipcompiler import settings
        api_key = key(); self.assertTrue(api_key, 'no API key')
        self.assertEqual(settings.validate_key(api_key)['state'], 'ok')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root/'clips'; source.mkdir()
            for i in range(2): clip(source/f'2026-10-0{i+1} 20-00-10.mp4', seconds=6, frequency=300+200*i)
            gc = gemini.client(api_key)
            before = {f.name for f in gc.files.list()}
            events = []
            result = job.JudgeJob(discover(source), root/'cache', root/'scratch', api_key=api_key, workers=2,
                                  emit=events.append).run()
            self.assertEqual((result['usage']['clips'], result['usage']['failed']), (2, 0))
            self.assertEqual(len(list((root/'cache').glob('*.json'))), 2)
            self.assertGreater(result['usage']['input_tokens'], 0)
            self.assertLess(result['usage']['usd'], .05)
            after = {f.name for f in gc.files.list()}
            self.assertFalse(after - before, 'uploads were left on Google')


    def test_skill_triage_and_a_close_up_return_valid_facts(self):
        """Stage A on a synthetic clip, then stage B forced on one window (test patterns have no real plays)."""
        import json
        from clipcompiler import skill
        api_key = key(); self.assertTrue(api_key, 'no API key')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root/'clips'; source.mkdir()
            path = clip(source/'2026-10-01 20-00-10.mp4', seconds=6)
            gc = gemini.client(api_key)
            before = {f.name for f in gc.files.list()}
            judge = job.JudgeJob(discover(source), root/'cache', root, api_key=api_key, workers=1, content_mode='skill')
            result = judge.run()
            self.assertEqual((result['usage']['clips'], result['usage']['failed']), (1, 0))
            record = json.loads(next((root/'cache'/'skill').glob('*.json')).read_text(encoding='utf-8'))
            self.assertIn(record['triage']['game'], skill.GAMES)
            play = {'start': 1., 'end': 4., 'kills': 1, 'preliminary': 5, 'what': 'forced'}
            detail = judge.detail(path, skill.load_profile('cs2'), play, skill.window_bounds(play, 6.))
            for field in ('kills', 'situation', 'penalty_flags', 'holistic_difficulty', 'one_liner'): self.assertIn(field, detail)
            self.assertIn(skill.score(detail, skill.load_profile('cs2')), range(1, 11))
            print(f"  tokens: triage {result['usage']['input_tokens']} in; close-up {detail['_meta']['input_tokens']} in, "
                  f"${gemini.cost(detail['_meta']):.4f}", flush=True)
            self.assertLess(result['usage']['usd'] + gemini.cost(detail['_meta']), .05)
            after = {f.name for f in gc.files.list()}
            self.assertFalse(after - before, 'uploads were left on Google')

    def test_batch_submit_wait_collect_end_to_end(self):
        """Real Batch API round trip on 2 tiny clips. Google usually finishes in ~10 min; gives up after 45."""
        import time
        from clipcompiler import batch
        api_key = key(); self.assertTrue(api_key, 'no API key')
        gc = gemini.client(api_key)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root/'clips'; source.mkdir()
            for i in range(2): clip(source/f'2026-10-0{i+1} 20-00-10.mp4', seconds=6, frequency=500+200*i)
            before = {f.name for f in gc.files.list()}
            state = batch.submit(discover(source), root/'job', root/'cache', gc, model=gemini.DEFAULT_MODEL, workers=2)
            self.assertEqual(len(state['clips']), 2); self.assertTrue(state['job'])
            try:
                deadline = time.monotonic() + 45*60
                while True:
                    job_ = batch.refresh(state, root/'job', gc)
                    print(f'  {time.strftime("%H:%M:%S")} {job_.state.name}', flush=True)
                    if job_.state.name in batch.DONE or time.monotonic() > deadline: break
                    time.sleep(30)
                self.assertEqual(job_.state.name, 'JOB_STATE_SUCCEEDED')
                state = batch.collect(state, root/'job', gc, root/'cache', job_)
                self.assertEqual((state['judged'], state['failed']), (2, []))
                result = job.JudgeJob(discover(source), root/'cache', root, cache_only=True).run()
                self.assertEqual((result['usage']['clips'], result['usage']['failed']), (2, 0))
                self.assertLess(state['usd'], .02)
            finally:
                if not state.get('collected'): batch.cancel(state, root/'job', gc)
            after = {f.name for f in gc.files.list()}
            self.assertFalse(after - before, 'uploads were left on Google')


if __name__ == '__main__':
    unittest.main()
