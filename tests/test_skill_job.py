"""JudgeJob in skill and both modes with fake judges (no network) on synthetic clips: two stages, gating,
resume without re-paying, cost, budget, shared upload, and the funny cache left byte-identical."""
import json
import shutil
from pathlib import Path
import tempfile
import threading
import types
import unittest
import unittest.mock

from clipcompiler import gemini, job, skill
from clipcompiler.media import Cancelled
from clipcompiler.reel import discover
from tests.helpers import clip, judgment

TRIAGE_META = {'model': 'fake', 'prompt': skill.TRIAGE_VERSION, 'input_tokens': 2850, 'output_tokens': 250, 'thinking_tokens': 450}
DETAIL_META = {'model': 'fake', 'prompt': skill.DETAIL_VERSION, 'input_tokens': 19000, 'output_tokens': 450, 'thinking_tokens': 600}


def triage_result():
    """Two promising plays and one dud in a 4 s clip; the dud must never get a close look."""
    return {'game': 'cs2', 'summary': 'fight', 'plays': [
        {'start': .5, 'end': 2, 'kills': 1, 'preliminary': 8, 'what': 'flick'},
        {'start': 2.2, 'end': 3.6, 'kills': 2, 'preliminary': 6, 'what': 'trade'},
        {'start': 3.7, 'end': 3.95, 'kills': 0, 'preliminary': 2, 'what': 'nothing'}], '_meta': dict(TRIAGE_META)}


def detail_result(headshot='yes'):
    return {'start': .2, 'end': 2.8, 'kills': [dict(t=1.5, headshot=headshot, flick='large', reaction='fast', range='mid',
                                                    target_moving=True, shots='one_tap', special=[])],
            'situation': dict(teammates_alive=0, enemies_alive=2, low_hp=False, round_decisive=True, outnumbered=False,
                              enemies_aware=True),
            'penalty_flags': [], 'evidence': 'kill feed', 'holistic_difficulty': 9, 'one_liner': 'flick clutch',
            '_meta': dict(DETAIL_META)}


class SkillJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.source = cls.root/'clips'; cls.source.mkdir()
        for i in range(3):
            clip(cls.source/f'2026-10-01 20-00-{i+1}0.mp4', seconds=4, frequency=300+100*i)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); self.cache = Path(self.work.name)/'cache'; self.cache.mkdir()
        self.events, self.calls = [], []

    def tearDown(self): self.work.cleanup()

    def run_job(self, mode='skill', on_detail=None, on_triage=None, **options):
        def funny(path, stop):
            self.calls.append(('funny', path.name)); return judgment((.5, 3.5, 8))

        def skilled(kind, path, stop, profile, play, window):
            self.calls.append((kind, path.name))
            if kind == 'triage':
                return on_triage(path, stop) if on_triage else triage_result()
            self.assertEqual(profile['name'], 'cs2'); self.assertLessEqual(window[1]-window[0], skill.MAX_WINDOW)
            return on_detail(path, stop, play) if on_detail else detail_result()
        options.setdefault('workers', 1)
        return job.JudgeJob(discover(self.source), self.cache, Path(self.work.name), judge_fn=funny, skill_fn=skilled,
                            content_mode=mode, emit=self.events.append, **options).run()

    def kinds(self):
        return [k for k, _ in self.calls]

    def test_triage_then_a_close_look_only_at_gated_plays(self):
        result = self.run_job()
        self.assertEqual(self.kinds(), ['triage', 'detail', 'detail']*3)   # The dud play is never sent.
        self.assertEqual(len(list((self.cache/'skill').glob('*.json'))), 3)
        self.assertEqual(list(self.cache.glob('*.json')), [])               # Skill mode writes no funny judgments.
        moments = result['moments']
        self.assertEqual({tuple(m['lenses']) for m in moments}, {('skill',)})
        self.assertEqual(len(moments), 6)
        self.assertTrue(all(m['score'] >= 8 for m in moments))
        record = json.loads(next((self.cache/'skill').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['version'], skill.RECORD_VERSION)
        for play in record['plays'].values():       # Stored in clip time, with the window it came from.
            self.assertLessEqual(play['window'][0], play['start']); self.assertLessEqual(play['end'], play['window'][1])

    def test_cost_counts_both_stages_and_a_rerun_is_free(self):
        first = self.run_job()
        expected = 3*(gemini.cost(TRIAGE_META) + 2*gemini.cost(DETAIL_META))
        self.assertAlmostEqual(first['usage']['usd'], expected)
        self.assertEqual(first['usage']['details'], 6)
        self.calls.clear(); self.events.clear()
        second = self.run_job()
        self.assertEqual((self.calls, second['usage']['usd']), ([], 0))
        self.assertEqual([e['status'] for e in self.events if e['type'] == 'clip'], ['cached']*3)
        self.assertEqual(len(second['moments']), 6)

    def test_an_interrupted_clip_resumes_at_stage_b_without_repaying_triage(self):
        cancel = threading.Event()

        def stop_on_first_detail(path, stop, play):
            cancel.set(); raise Cancelled()
        first = self.run_job(on_detail=stop_on_first_detail, cancel=cancel)
        self.assertEqual((self.kinds(), first['stopped']), (['triage', 'detail'], 'cancelled'))
        self.assertAlmostEqual(first['usage']['usd'], gemini.cost(TRIAGE_META))   # The finished triage still bills.
        self.calls.clear()
        self.run_job()
        self.assertEqual(self.kinds()[:2], ['detail', 'detail'])   # First clip: straight to the close-ups.
        self.assertEqual(self.kinds().count('triage'), 2)          # Only the two clips never started.

    def test_budget_counts_close_ups_and_stops(self):
        one_clip = gemini.cost(TRIAGE_META) + 2*gemini.cost(DETAIL_META)
        result = self.run_job(budget_usd=one_clip*1.5)
        self.assertEqual((result['stopped'], self.kinds().count('triage')), ('budget', 1))

    def test_daily_limit_during_a_close_up_keeps_the_triage_and_stops(self):
        def quota(path, stop, play): raise gemini.DailyLimit('429 per day')
        result = self.run_job(on_detail=quota)
        self.assertEqual(result['stopped'], 'daily_limit')
        record = json.loads(next((self.cache/'skill').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['plays'], {})
        self.assertAlmostEqual(result['usage']['usd'], gemini.cost(TRIAGE_META))

    def test_both_mode_judges_each_lens_once_and_tags_moments(self):
        result = self.run_job('both')
        self.assertEqual(self.kinds().count('funny'), 3); self.assertEqual(self.kinds().count('triage'), 3)
        self.assertEqual({tuple(m['lenses']) for m in result['moments']}, {('funny',), ('skill',)})
        clip_events = [e for e in self.events if e['type'] == 'clip']
        self.assertEqual(clip_events[0]['lenses'], ['funny', 'skill', 'skill'])
        cands = job.candidates(result['moments'], 7, 7)
        self.assertTrue(any(set(m['lenses']) == {'funny', 'skill'} for m in cands))   # Same play: merged, both tags.

    def test_skill_target_stops_once_enough_skill_footage_exists(self):
        result = self.run_job(target_seconds=2)
        self.assertEqual((result['stopped'], self.kinds().count('triage')), ('target', 1))

    def test_both_target_waits_for_each_share(self):
        result = self.run_job('both', target_seconds=4, skill_share=.5)
        self.assertEqual(result['stopped'], 'target')
        shutil.rmtree(self.cache/'skill')
        funny_only = self.run_job('both', on_triage=lambda p, s: {'game': 'cs2', 'summary': '', 'plays': [],
                                                                  '_meta': dict(TRIAGE_META)}, target_seconds=4)
        self.assertIsNone(funny_only['stopped'])   # No skill footage at all: keeps judging every clip.

    def test_cache_only_both_mode_shows_whichever_lens_is_saved(self):
        self.run_job('funny')
        self.calls.clear(); self.events.clear()
        result = self.run_job('both', cache_only=True)
        self.assertEqual(self.calls, [])
        self.assertEqual([e['status'] for e in self.events if e['type'] == 'clip'], ['cached']*3)
        self.assertEqual({tuple(m['lenses']) for m in result['moments']}, {('funny',)})
        nothing = self.run_job('skill', cache_only=True)
        self.assertEqual(nothing['moments'], [])

    def test_skill_and_both_runs_leave_the_funny_cache_byte_identical(self):
        self.run_job('funny')
        before = {p.name: p.read_bytes() for p in self.cache.glob('*.json')}
        self.assertEqual(len(before), 3)
        self.run_job('skill'); self.run_job('both')
        self.assertEqual({p.name: p.read_bytes() for p in self.cache.glob('*.json')}, before)

    def test_report_explains_each_skill_score(self):
        result = self.run_job()
        entry = result['report'][0]['skill']
        self.assertEqual((entry['game'], len(entry['plays']), len(entry['features'])), ('cs2', 2, 2))
        self.assertIn('Headshot', entry['plays'][0]['skill_tags'])

    def test_profile_override_is_used_for_scoring(self):
        result = self.run_job(profiles=skill.Profiles(override='cs2', preset='aim'))
        self.assertTrue(all(m['game'] == 'cs2' for m in result['moments']))
        with self.assertRaises(ValueError):
            job.JudgeJob([], self.cache, Path(self.work.name), content_mode='silly')


class SharedUploadTests(unittest.TestCase):
    """The real Gemini path (stubbed client): one upload serves several questions, then is deleted."""
    def client(self, texts):
        files = types.SimpleNamespace(deleted=[], uploads=0)
        def upload(file):
            files.uploads += 1; return types.SimpleNamespace(name='files/1', uri='https://fake/files/1', state=types.SimpleNamespace(name='ACTIVE'))
        files.upload = upload; files.delete = lambda name: files.deleted.append(name)
        usage = types.SimpleNamespace(prompt_token_count=10, candidates_token_count=2, thoughts_token_count=3)
        sent = []
        def generate_content(**kwargs):
            sent.append(kwargs); return types.SimpleNamespace(text=json.dumps(texts[len(sent)-1]), usage_metadata=usage)
        return types.SimpleNamespace(files=files, models=types.SimpleNamespace(generate_content=generate_content)), files, sent

    def test_funny_and_triage_share_one_upload(self):
        gc, files, sent = self.client([{'summary': 's', 'moments': []}, {'game': 'cs2', 'summary': '', 'plays': []}])
        triage = {'kind': 'triage', 'prompt': skill.TRIAGE_PROMPT, 'schema': skill.TRIAGE_SCHEMA, 'version': skill.TRIAGE_VERSION}
        out = gemini.upload_and_ask(gc, 'm', Path('c.mp4'), Path('small.mp4'), threading.Event(), [gemini.FUNNY, triage])
        self.assertEqual((files.uploads, len(sent), files.deleted), (1, 2, ['files/1']))
        self.assertEqual([o['_meta']['prompt'] for o in out], [gemini.PROMPT_VERSION, skill.TRIAGE_VERSION])
        self.assertEqual(sent[1]['config']['response_schema'], skill.TRIAGE_SCHEMA)

    def test_a_close_up_asks_gemini_for_the_high_frame_rate(self):
        gc, files, sent = self.client([{'kills': []}])
        ask = {'kind': 'detail', 'prompt': 'p', 'schema': skill.DETAIL_SCHEMA, 'version': skill.DETAIL_VERSION, 'fps': 12}
        gemini.upload_and_ask(gc, 'm', Path('c.mp4'), Path('small.mp4'), threading.Event(), [ask])
        part = sent[0]['contents'][0]
        self.assertEqual((part.video_metadata.fps, part.file_data.file_uri), (12, 'https://fake/files/1'))

    def test_both_mode_job_sends_funny_and_triage_in_one_call(self):
        with tempfile.TemporaryDirectory() as d:
            source = clip(Path(d)/'2026-10-01 20-00-10.mp4', seconds=4)
            seen = []
            def fake_judge(gc, model, path, stop, scratch, ffmpeg='ffmpeg', asks=None):
                seen.append([a['kind'] for a in asks] if asks else None)
                return [judgment((.5, 3.5, 8)), dict(triage_result(), plays=[])]
            with unittest.mock.patch('clipcompiler.gemini.judge', side_effect=fake_judge), \
                    unittest.mock.patch('clipcompiler.gemini.client', return_value=object()):
                job.JudgeJob([(0, False, source)], Path(d)/'cache', Path(d), api_key='k', content_mode='both', workers=1).run()
            self.assertEqual(seen, [['funny', 'triage']])


if __name__ == '__main__':
    unittest.main()
