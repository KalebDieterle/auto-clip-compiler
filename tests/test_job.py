"""JudgeJob / scan / render jobs with a fake judge (no network) on synthetic clips."""
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest

from clipcompiler import gemini, job
from clipcompiler.reel import discover
from clipcompiler.store import HashIndex
from tests.helpers import clip, judgment


class AuthError(Exception):
    code = 400


class JobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.source = cls.root/'clips'; cls.source.mkdir()
        for i in range(3):  # OBS-style names: saved 20:00:10, 20:00:20, 20:00:30.
            clip(cls.source/f'2026-10-01 20-00-{i+1}0.mp4', seconds=4, frequency=300+100*i)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); self.cache = Path(self.work.name)/'cache'; self.cache.mkdir()
        self.events, self.calls = [], []

    def tearDown(self): self.work.cleanup()

    def run_job(self, behaviour=None, **options):
        def fake(path, stop):
            self.calls.append(path.name)
            if behaviour: return behaviour(path, stop, len(self.calls))
            return judgment((.5, 3.5, 8))
        options.setdefault('workers', 1)
        return job.JudgeJob(discover(self.source), self.cache, Path(self.work.name), judge_fn=fake,
                            emit=self.events.append, **options).run()

    def statuses(self):
        return [e['status'] for e in self.events if e['type'] == 'clip']

    def test_judges_each_clip_once_then_reuses_the_cache(self):
        first = self.run_job()
        self.assertEqual((len(self.calls), first['stopped'], len(first['moments'])), (3, None, 3))
        self.assertEqual(len(list(self.cache.glob('*.json'))), 3)
        self.assertAlmostEqual(first['usage']['usd'], 3*gemini.cost(judgment()['_meta']))
        self.assertEqual([e['type'] for e in self.events], ['judge_start', 'clip', 'clip', 'clip', 'judge_done'])
        self.assertEqual([e['done'] for e in self.events if e['type'] == 'clip'], [1, 2, 3])
        self.events.clear()
        second = self.run_job()
        self.assertEqual((len(self.calls), second['usage']['usd'], self.statuses()), (3, 0, ['cached']*3))
        self.assertEqual(self.events[-1]['type'], 'judge_done')

    def test_a_failed_clip_is_reported_not_cached_and_the_rest_continue(self):
        def flaky(path, stop, n):
            if n == 2: raise RuntimeError('Gemini 500 forever')
            return judgment((.5, 3.5, 8))
        result = self.run_job(flaky)
        self.assertEqual(sorted(self.statuses()), ['failed', 'judged', 'judged'])
        self.assertEqual((len(list(self.cache.glob('*.json'))), result['usage']['failed']), (2, 1))
        self.assertIn('Gemini 500 forever', json.dumps(result['report']))

    def test_budget_stops_with_room_for_in_flight_clips(self):
        one = gemini.cost(judgment()['_meta'])
        result = self.run_job(budget_usd=one*1.9)
        self.assertEqual((result['stopped'], len(self.calls)), ('budget', 1))
        self.assertTrue(any(e['type'] == 'stopping' and e['reason'] == 'budget' for e in self.events))

    def test_daily_quota_stops_cleanly(self):
        def quota(path, stop, n):
            if n == 2: raise gemini.DailyLimit('429 per day')
            return judgment((.5, 3.5, 8))
        result = self.run_job(quota)
        self.assertEqual((result['stopped'], len(self.calls), result['usage']['clips']), ('daily_limit', 2, 1))

    def test_user_cancel_finishes_the_current_clip_and_starts_no_more(self):
        cancel = threading.Event()
        def cancelling(path, stop, n):
            cancel.set(); return judgment((.5, 3.5, 8))
        result = self.run_job(cancelling, cancel=cancel)
        self.assertEqual((result['stopped'], len(self.calls), len(list(self.cache.glob('*.json')))), ('cancelled', 1, 1))

    def test_rejected_key_stops_after_the_first_failure(self):
        def rejected(path, stop, n): raise AuthError('400 API key not valid. Please pass a valid API key.')
        result = self.run_job(rejected)
        self.assertEqual((result['stopped'], len(self.calls)), ('auth', 1))

    def test_reel_mode_stops_once_target_footage_exists(self):
        result = self.run_job(target_seconds=2)
        self.assertEqual((result['stopped'], len(self.calls)), ('target', 1))

    def test_cache_only_never_judges(self):
        result = self.run_job(cache_only=True)
        self.assertEqual((self.calls, self.statuses(), result['usage']['usd']), ([], ['unjudged']*3, 0))

    def test_scan_marks_cached_clips_and_the_hash_index_skips_rehashing(self):
        self.run_job(lambda p, s, n: judgment() if n == 1 else (_ for _ in ()).throw(RuntimeError('x')))
        index = HashIndex(Path(self.work.name)/'index.json')
        rows = job.scan(discover(self.source), self.cache, index)
        self.assertEqual(sorted(r['cached'] for r in rows), [False, False, True])
        index.save(); again = HashIndex(Path(self.work.name)/'index.json')
        with unittest.mock.patch('clipcompiler.store.file_hash', side_effect=AssertionError('rehashed')):
            self.assertEqual([again(r['path']) for r in rows], [r['digest'] for r in rows])

    def test_render_parts_names_files_and_a_rerun_skips_finished_parts(self):
        result = self.run_job()
        cands = job.candidates(result['moments'], 7)
        self.assertEqual(len(cands), 3)
        settings = dict(job.render_settings(height=90, fps=15), min_free_gb=0)
        out = Path(self.work.name)/'out'
        files, plan = job.render_parts(cands, out, 60, settings, emit=self.events.append)
        self.assertEqual([Path(f).name for f in files], ['Part 01 (2026-10-01 to 2026-10-01).mp4'])
        self.assertEqual(plan[0]['moments'], 3)
        with unittest.mock.patch('clipcompiler.job.publish', side_effect=AssertionError('re-rendered')):
            job.render_parts(cands, out, 60, settings)

    def test_render_reel_never_overwrites_an_earlier_reel(self):
        cands = job.candidates(self.run_job()['moments'], 7)
        settings = dict(job.render_settings(height=90, fps=15), min_free_gb=0)
        out = Path(self.work.name)/'reel'
        a = job.render_reel(cands, out, settings); b = job.render_reel(cands, out, settings)
        self.assertNotEqual(a, b); self.assertTrue(a.exists() and b.exists())
        self.assertEqual(b.name, 'Highlights (2026-10-01 to 2026-10-01) (#2).mp4')


class FakeFiles:
    def __init__(self): self.deleted = []
    def upload(self, file): return types.SimpleNamespace(name='files/1', state=types.SimpleNamespace(name='ACTIVE'))
    def get(self, name): raise AssertionError('not processing')
    def delete(self, name): self.deleted.append(name)


class UploadTests(unittest.TestCase):
    def test_upload_is_deleted_even_when_judging_fails(self):
        files = FakeFiles()
        class Models:
            def generate_content(self, **kwargs): raise ValueError('boom')
        gc = types.SimpleNamespace(files=files, models=Models())
        with self.assertRaises(ValueError):
            gemini.upload_and_judge(gc, 'm', Path('c.mp4'), Path('small.mp4'), threading.Event())
        self.assertEqual(files.deleted, ['files/1'])

    def test_success_records_usage_and_deletes_upload(self):
        files = FakeFiles()
        usage = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=2, thoughts_token_count=3)
        response = types.SimpleNamespace(text=json.dumps({'summary': 's', 'moments': []}), usage_metadata=usage)
        gc = types.SimpleNamespace(files=files, models=types.SimpleNamespace(generate_content=lambda **k: response))
        result = gemini.upload_and_judge(gc, 'm', Path('c.mp4'), Path('small.mp4'), threading.Event())
        self.assertEqual((result['_meta']['thinking_tokens'], result['_meta']['prompt'], files.deleted),
                         (3, gemini.PROMPT_VERSION, ['files/1']))


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
