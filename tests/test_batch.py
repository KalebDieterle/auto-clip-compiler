"""Batch API judging against a faithful fake of Google's Files + Batches API (no network)."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from clipcompiler import batch, gemini, job
from clipcompiler.media import Cancelled
from clipcompiler.reel import discover
from clipcompiler.store import file_hash
from tests.fake_gemini import FakeGemini
from tests.helpers import clip


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.source = cls.root/'clips'; cls.source.mkdir()
        for i in range(4):
            clip(cls.source/f'2026-10-0{i+1} 20-00-10.mp4', seconds=5, frequency=300+100*i)
        cls.clips = discover(cls.source)
        cls.digests = {c[2].name: file_hash(c[2]) for c in cls.clips}

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); w = Path(self.work.name)
        self.cache, self.job_dir, self.gc = w/'cache', w/'job', FakeGemini()

    def tearDown(self): self.work.cleanup()

    def submit(self, **options):
        return batch.submit(self.clips, self.job_dir, self.cache, self.gc, model='m', workers=2, **options)

    def test_submit_uploads_uncached_clips_and_writes_valid_requests(self):
        self.cache.mkdir()
        cached = self.digests['2026-10-01 20-00-10.mp4']
        (self.cache/f'{cached}.json').write_text('{}', encoding='utf-8')
        events = []
        state = self.submit(emit=events.append)
        self.assertEqual(len(state['clips']), 3); self.assertNotIn(cached, state['clips'])
        self.assertEqual(state['status'], 'JOB_STATE_PENDING'); self.assertTrue(state['job'])
        requests = self.gc.batches.jobs[state['job']]['requests']
        self.assertEqual({r['key'] for r in requests}, set(state['clips']))
        r = requests[0]['request']
        self.assertEqual(r['contents'][0]['parts'][1]['text'], gemini.PROMPT)
        self.assertEqual(r['generationConfig']['responseSchema']['type'], 'OBJECT')
        self.assertEqual(r['generationConfig']['responseSchema']['properties']['moments']['items']['properties']['score']['type'], 'INTEGER')
        self.assertEqual(r['contents'][0]['parts'][0]['fileData']['mimeType'], 'video/mp4')
        self.assertEqual(events[-1]['type'], 'batch_submitted'); self.assertEqual(events[-1]['clips'], 3)
        self.assertAlmostEqual(state['estimate_usd'], 3*batch.PER_CLIP, places=4)
        self.assertFalse(list((self.job_dir/'.proxies').iterdir()))  # Proxies are temporary.

    def test_collect_writes_live_shaped_judgments_and_deletes_every_upload(self):
        state = self.submit()
        self.gc.error_keys.add(next(iter(state['clips'])))
        self.gc.batches.finish(state['job'])
        state = batch.collect(state, self.job_dir, self.gc, self.cache)
        self.assertEqual((state['judged'], len(state['failed'])), (3, 1))
        self.assertEqual(set(self.gc.files.store), {f'files/results-{state["job"].split("/")[1]}'})  # Only Google's output left.
        result = json.loads(next(self.cache.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(result['moments'][0]['score'], 8)
        self.assertEqual((result['_meta']['batch'], result['_meta']['thinking_tokens'], result['_meta']['prompt']),
                         (True, 450, gemini.PROMPT_VERSION))
        self.assertAlmostEqual(state['usd'], 3*gemini.cost(result['_meta'])*.5, places=4)
        # Cached batch results feed the normal pipeline: a cache-only judge finds the moments, $0.
        run = job.JudgeJob(self.clips, self.cache, Path(self.work.name), cache_only=True).run()
        self.assertEqual((len(run['moments']), run['usage']['usd']), (3, 0))
        again = batch.collect(state, self.job_dir, self.gc, self.cache)
        self.assertEqual(again['judged'], 3)  # Idempotent.

    def test_interrupted_submit_resumes_without_reuploading(self):
        cancel = threading.Event(); uploads = []
        def stop_after_two(e):
            if e['type'] == 'batch_upload' and e['done'] >= 2: uploads.append(e['done']); cancel.set()
        with self.assertRaises(Cancelled):
            batch.submit(self.clips, self.job_dir, self.cache, self.gc, model='m', workers=1, emit=stop_after_two, cancel=cancel)
        self.assertEqual(self.gc.files.store, {})  # A cancelled submit leaves nothing on Google.
        state = batch.load(self.job_dir); self.assertEqual(state['status'], 'cancelled')
        # A crash (no cleanup) mid-upload: state remembers the uploads, and a re-submit reuses them.
        fresh = Path(self.work.name)/'job2'
        gc = FakeGemini(); first = {}
        def crash(e):
            if e['type'] == 'batch_upload' and e['done'] == 2: first.update(batch.load(fresh) or {}); raise SystemExit
        try:
            batch.submit(self.clips, fresh, self.cache, gc, model='m', workers=1, emit=crash)
        except SystemExit:
            pass
        saved = batch.load(fresh); before = len(saved['clips']) if saved else 0
        uploads_before = sum(1 for c in gc.calls if c[0] == 'upload')
        state = batch.submit(self.clips, fresh, self.cache, gc, model='m', workers=1)
        self.assertEqual(len(state['clips']), 4)
        self.assertEqual(sum(1 for c in gc.calls if c[0] == 'upload') - uploads_before, 4 - before + 1)  # +1 = requests file

    def test_budget_submits_only_the_newest_clips_that_fit(self):
        events = []
        state = self.submit(max_usd=batch.PER_CLIP*2.5, emit=events.append)
        paths = sorted(Path(c['path']).name for c in state['clips'].values())
        self.assertEqual(paths, ['2026-10-03 20-00-10.mp4', '2026-10-04 20-00-10.mp4'])
        self.assertEqual(events[-1]['capped'], 2)

    def test_a_transient_upload_error_is_retried(self):
        class Instant(threading.Event):
            def wait(self, timeout=None): return self.is_set()  # Skip the retry back-off.
        self.gc.files.fail_uploads = 1  # One transient reset: retried, not lost.
        state = self.submit(cancel=Instant())
        self.assertEqual((len(state['clips']), state['failed']), (4, []))

    def test_expired_or_failed_jobs_clean_up_and_report(self):
        state = self.submit()
        self.gc.batches.finish(state['job'], 'JOB_STATE_EXPIRED')
        state = batch.collect(state, self.job_dir, self.gc, self.cache)
        self.assertEqual((state['judged'], state['failed'][-1], state['status']), (0, {'job': 'JOB_STATE_EXPIRED'}, 'JOB_STATE_EXPIRED'))
        self.assertEqual(self.gc.files.store, {})

    def test_collect_refuses_unfinished_jobs(self):
        state = self.submit()
        with self.assertRaises(RuntimeError): batch.collect(state, self.job_dir, self.gc, self.cache)
        self.assertTrue(self.gc.files.store)  # Uploads stay while Google still needs them.

    def test_cancel_stops_the_job_and_deletes_uploads(self):
        state = self.submit()
        batch.cancel(state, self.job_dir, self.gc)
        self.assertEqual((self.gc.batches.cancelled, self.gc.files.store), ([state['job']], {}))
        self.assertEqual(batch.load(self.job_dir)['status'], 'JOB_STATE_CANCELLED')

    def test_nothing_to_judge_creates_no_job(self):
        self.cache.mkdir()
        for d in self.digests.values(): (self.cache/f'{d}.json').write_text('{}', encoding='utf-8')
        state = self.submit()
        self.assertEqual((state['job'], state['status'], self.gc.batches.jobs), (None, 'nothing', {}))

    def test_a_second_submit_to_a_submitted_job_dir_is_refused(self):
        self.submit()
        with self.assertRaises(RuntimeError): self.submit()


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
