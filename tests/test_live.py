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
            before = {f.name for f in gemini.client(api_key).files.list()}
            events = []
            result = job.JudgeJob(discover(source), root/'cache', root/'scratch', api_key=api_key, workers=2,
                                  emit=events.append).run()
            self.assertEqual((result['usage']['clips'], result['usage']['failed']), (2, 0))
            self.assertEqual(len(list((root/'cache').glob('*.json'))), 2)
            self.assertGreater(result['usage']['input_tokens'], 0)
            self.assertLess(result['usage']['usd'], .05)
            after = {f.name for f in gemini.client(api_key).files.list()}
            self.assertFalse(after - before, 'uploads were left on Google')


if __name__ == '__main__':
    unittest.main()
