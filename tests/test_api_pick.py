"""Choosing specific clips from a scan: counts and estimates follow the choice, only chosen clips are
judged (live and batch), the choice resets on a new scan, and nothing here calls the API."""
from pathlib import Path
from unittest.mock import patch

from tests.test_api_skill import SkillApiBase


class ClipPickerTests(SkillApiBase):
    def setUp(self):
        super().setUp()
        self.settings(content_mode='funny')
        self.scan()
        self.paths = [c['path'] for c in self.api.scan_clips()['clips']]

    def test_picker_lists_every_scanned_clip_newest_first_all_chosen(self):
        clips = self.api.scan_clips()['clips']
        self.assertEqual(len(clips), 3)
        self.assertEqual([c['name'] for c in clips], sorted((c['name'] for c in clips), reverse=True))
        self.assertTrue(all(c['chosen'] and not c['ready'] and not c['pending'] and c['mb'] > 0 for c in clips))

    def test_choosing_clips_updates_counts_and_estimates_without_calling_google(self):
        with patch('clipcompiler.gemini.client', side_effect=AssertionError('API called')):
            one = self.api.select_clips(self.paths[:1])
        self.assertEqual((one['clips'], one['total'], one['to_judge'], one['picked']), (1, 3, 1, True))
        everything = self.api.select_clips(None)
        self.assertEqual((everything['clips'], everything['picked']), (3, False))
        self.assertAlmostEqual(one['estimate_usd']*3, everything['estimate_usd'], delta=.011)
        self.assertEqual({m: one['estimates'][m]['clips'] for m in one['estimates']}, {'funny': 1, 'skill': 1, 'both': 1})

    def test_only_chosen_clips_are_judged_and_recorded(self):
        self.api.select_clips(self.paths[1:])
        self.assertTrue(self.api.start_judge()['ok'])
        self.wait('review_ready'); self.idle()
        self.assertEqual(self.calls.count('funny'), 2)
        run = self.api.get_review()['run']
        import json
        recorded = json.loads((Path(run['folder'])/'run.json').read_text(encoding='utf-8'))['clips']
        self.assertEqual(sorted(p for _, _, p in recorded), sorted(self.paths[1:]))

    def test_after_judging_the_picker_marks_those_clips_as_free(self):
        self.api.select_clips(self.paths[:1]); self.api.start_judge(); self.wait('review_ready'); self.idle()
        self.scan()
        ready = {c['path']: c['ready'] for c in self.api.scan_clips()['clips']}
        self.assertEqual(ready, {p: p == self.paths[0] for p in self.paths})

    def test_unknown_paths_are_ignored_and_choosing_everything_means_all(self):
        summary = self.api.select_clips(self.paths[:1] + ['C:/not/scanned.mp4'])
        self.assertEqual(summary['clips'], 1)
        self.assertEqual(self.api.select_clips(list(self.paths))['picked'], False)

    def test_nothing_chosen_refuses_to_start(self):
        self.assertEqual(self.api.select_clips([])['clips'], 0)
        self.assertEqual(self.api.start_judge(), {'error': 'Choose at least one clip.'})

    def test_a_new_scan_resets_the_choice(self):
        self.api.select_clips(self.paths[:1])
        done = self.scan()
        self.assertEqual((done['clips'], done['picked']), (3, False))
        self.assertTrue(all(c['chosen'] for c in self.api.scan_clips()['clips']))

    def test_a_batch_submits_only_the_chosen_clips(self):
        self.api = self.make_batch_api(); self.seen = 0
        self.settings(content_mode='funny', judge_mode='batch'); self.scan()
        self.api.select_clips(self.paths[:2])
        self.assertTrue(self.api.start_judge()['ok'])
        waiting = self.wait('batch_waiting'); self.idle()
        self.assertEqual(waiting['batch']['clips'], 2)

    def make_batch_api(self):
        from clipcompiler.ui.api import Api
        return Api(judge_fn=lambda p, s: self.fail('live judging used in batch mode'), client_factory=lambda key: self.google)

    def test_open_clip_plays_only_scanned_clips(self):
        with patch('os.startfile', create=True) as opened:
            self.assertEqual(self.api.open_clip(self.paths[0]), {'ok': True})
            self.assertIn('error', self.api.open_clip('C:/Windows/System32/calc.exe'))
        opened.assert_called_once_with(self.paths[0])

    def test_picker_needs_a_scan_first(self):
        fresh = self.make_api()
        self.assertIn('error', fresh.scan_clips()); self.assertIn('error', fresh.select_clips(None))


if __name__ == '__main__':
    import unittest
    unittest.main()
