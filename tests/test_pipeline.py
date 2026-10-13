"""Orchestration, overlap integration, frame joins, multitrack, evaluation and CLI tests.

Models are faked (no Ollama, Whisper or YAMNet); FFmpeg, alignment and rendering are real.
"""
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from clipcompiler import cli, pipeline
from clipcompiler.media import command, probe
from clipcompiler.render import fcpxml, frame_cuts, manifest, publish
from clipcompiler.store import Store
import evaluate


class FakeAudio:
    model_digest = 'whisper-test'; yamnet_digest = None
    def __init__(self, *args): pass
    def load(self): pass
    def transcribe(self, wav, offset): return {'status': 'empty_speech', 'segments': [], 'language': 'en'}
    def evidence(self, wav, offset): return {'energy': [], 'laughter': [], 'yamnet': 'not_configured'}


class FakeJudge:
    """Keeps the middle of every window; fails for sources named in `broken`."""
    broken = set(); judged = []
    def __init__(self, *args, **kwargs): self.digest = 'judge-test'
    def preflight(self): return {'model': 'fake'}
    def observe(self, oid, frames, speech, evidence):
        return {'id': oid, 'observations': 'Frames observed', 'frame_times': [f['time'] for f in frames]}
    def judge(self, wid, start, end, speech, observations, evidence):
        FakeJudge.judged.append(wid)
        if any(wid.startswith(sid) for sid in FakeJudge.broken):
            raise ValueError('Judge returned malformed JSON')
        return {'id': wid, 'coverage': 'Whole window', 'moments': [dict(
            start=start, end=end, payoff_time=(start+end)/2, setup='Walk up', event='Bit', payoff='Fall',
            reaction='Laugh', score=7, confidence='high', recommendation='keep', reason='Funny',
            boundary_uncertain=False)]}


def clip(path, seconds=4, offset=0, tracks=1):
    """Moving test pattern; distinct sines per track. `offset` trims the head like a later replay."""
    args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size=160x90:rate=15:duration={seconds+offset}']
    for t in range(tracks):
        args += ['-f', 'lavfi', '-i', f'sine=frequency={440*(t+1)}:sample_rate=48000:duration={seconds+offset}']
    args += ['-ss', offset, '-map', '0:v']
    for t in range(tracks):
        args += ['-map', f'{t+1}:a']
    command(args + ['-c:v', 'libx264', '-g', '15', '-c:a', 'aac', path])
    return path


def settings(source, output, **changes):
    return dict(dict(source=str(source), output=str(output), artifact_root=str(Path(output)/'.cache'),
        judge_model='fake', judge_mode='visual', ollama_url='http://127.0.0.1:11434', cpu=False,
        whisper_model='small', yamnet_path=None, audio_track=None, llm_timeout=30, frame_batch=12,
        workers=2, short_seconds=60, limit=None, include=[], top=None, min_score=None, min_dur=0.,
        max_dur=None, ranked=False, render=True, resolve=True, timestamp_overlay=False, render_height=90,
        fps=15, ffmpeg='ffmpeg', ffprobe='ffprobe', min_free_gb=0, analyze_only=False, allow_partial=False),
        **changes)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root/'clips'; self.source.mkdir(); self.out = self.root/'out'
        FakeJudge.broken = set(); FakeJudge.judged = []
        self.patches = [patch.object(pipeline, 'Ollama', FakeJudge), patch.object(pipeline, 'AudioAnalyzer', FakeAudio),
                        patch('clipcompiler.store.time.sleep')]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in self.patches: p.stop()
        self.temp.cleanup()

    def run_dir(self): return self.out/'run'

    def sid(self, name):
        store = Store(self.run_dir())
        try: return next(s['id'] for s in store.sources() if Path(s['path']).name == name)
        finally: store.close()

    def test_failure_withholds_output_then_resume_completes_with_cache(self):
        # Different lengths: identical bytes would (correctly) be deduplicated to one copy.
        clip(self.source/'good.mp4'); clip(self.source/'bad.mp4', seconds=5)
        store = Store(self.run_dir()); store.close()
        # Fail exactly one source. Its ID is fixed by path, so look it up after discovery.
        with patch.object(FakeJudge, 'judge', autospec=True, side_effect=ValueError('bad JSON')) as broken:
            summary = pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        self.assertEqual(summary['status'], 'incomplete'); self.assertEqual(len(summary['failed']), 2)
        self.assertGreater(broken.call_count, 0)
        self.assertFalse(list(self.run_dir().glob('render-/*')))  # Nothing published.
        # Repair the judge for one source only; the other still fails -> still incomplete.
        FakeJudge.broken = {self.sid('bad.mp4')}
        summary = pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        self.assertEqual([Path(f['path']).name for f in summary['failed']], ['bad.mp4'])
        self.assertFalse(list(self.run_dir().glob('render-/*')))
        # Fully repaired: completes, and good.mp4's judgment is served from cache.
        FakeJudge.broken = set(); FakeJudge.judged = []
        summary = pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        self.assertEqual(summary['status'], 'complete')
        self.assertTrue(all(w.startswith(self.sid('bad.mp4')) for w in FakeJudge.judged))
        rendered = list(self.run_dir().glob('render-/*/Combined_highlights.mp4'))
        self.assertEqual(len(rendered), 1)
        self.assertAlmostEqual(probe(rendered[0])['duration'], 9, delta=.2)

    def test_partial_output_is_explicit_and_labelled(self):
        clip(self.source/'good.mp4'); clip(self.source/'bad.mp4')
        Store(self.run_dir()).close()
        FakeJudge.broken = {pipeline.identity(str((self.source/'bad.mp4').resolve()).casefold())}
        summary = pipeline.analyze(self.run_dir(), settings(self.source, self.out, allow_partial=True))
        self.assertEqual(summary['status'], 'incomplete')
        published = list(self.run_dir().glob('partial-/*'))
        self.assertEqual(len(published), 1)
        self.assertTrue((published[0]/'Partial_highlights.mp4').is_file())
        report = json.loads((published[0]/'render.json').read_text())
        self.assertEqual((report['status'], [Path(o['path']).name for o in report['omissions']]), ('partial', ['bad.mp4']))
        with self.assertRaises(ValueError):  # Saved-decision render still refuses without the flag.
            pipeline.render_saved(self.run_dir(), {'allow_partial': False})

    def test_render_failure_keeps_analysis_complete_and_rerenderable(self):
        clip(self.source/'good.mp4'); Store(self.run_dir()).close()
        with patch.object(pipeline, 'publish', side_effect=RuntimeError('disk full')), self.assertRaises(RuntimeError):
            pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        store = Store(self.run_dir())
        try: self.assertEqual((store.get('state'), store.get('render_error')), ('complete', 'disk full'))
        finally: store.close()
        self.assertTrue(pipeline.render_saved(self.run_dir()).is_dir())

    def test_exact_duplicate_plays_once(self):
        clip(self.source/'a.mp4'); shutil.copy(self.source/'a.mp4', self.source/'a copy.mp4'); Store(self.run_dir()).close()
        pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        cuts = json.loads((self.run_dir()/'selections.json').read_text())
        self.assertEqual(len({c['event_group'] for c in cuts}), 1)
        self.assertAlmostEqual(sum(c['end']-c['start'] for c in cuts), 4, delta=.1)

    def test_offset_replay_overlap_only_removes_matched_footage(self):
        # Same session saved twice: the second replay starts 3 s later and runs 3 s past.
        clip(self.source/'Replay 2026-01-01 10-00-00.mp4', seconds=10)
        clip(self.source/'Replay 2026-01-01 10-00-03.mp4', seconds=10, offset=3); Store(self.run_dir()).close()
        summary = pipeline.analyze(self.run_dir(), settings(self.source, self.out))
        self.assertEqual(summary['status'], 'complete')
        store = Store(self.run_dir())
        try: rows = [dict(r, data=json.loads(r['data'])) for r in store.db.execute('SELECT * FROM alignments')]
        finally: store.close()
        self.assertEqual([(r['status'], abs(r['data']['offset'])) for r in rows], [('confirmed', 3)])
        cuts = json.loads((self.run_dir()/'selections.json').read_text())
        spans = sorted((c['timeline_start'], c['timeline_end']) for c in cuts)
        self.assertEqual(len({c['event_group'] for c in cuts}), 1)
        for (a, b), (x, y) in zip(spans, spans[1:]):  # Contiguous, never overlapping.
            self.assertLessEqual(b, x+1e-6)
        # Timeline zero is whichever recording roots the group; only the covered span matters.
        self.assertAlmostEqual(spans[-1][1]-spans[0][0], 13, delta=.1)
        rendered = next(self.run_dir().glob('render-/*/Combined_highlights.mp4'))
        self.assertAlmostEqual(probe(rendered)['duration'], 13, delta=.2)


class FrameJoinTests(unittest.TestCase):
    def test_cross_source_join_never_repeats_a_frame(self):
        fps = 30
        # Offset 2.37 s is off the 30 fps grid of the second recording.
        a = dict(id='a', source='a', start=1.01, end=5.0, duration=10, event_group='g', timeline_offset=0.)
        b = dict(id='b', source='b', start=5.0-2.37, end=8.0, duration=10, event_group='g', timeline_offset=2.37)
        cuts = frame_cuts([a, b], fps)
        gap = cuts[1]['timeline_start'] - cuts[0]['timeline_end']
        self.assertGreaterEqual(gap, -1e-9); self.assertLess(gap, 1/fps)
        for c in cuts:
            for key in ('start', 'end'): self.assertAlmostEqual(c[key]*fps, round(c[key]*fps))

    def test_same_source_outward_rounding_still_deduplicated(self):
        cuts = frame_cuts([dict(id='x', source='s', start=1.01, end=2.01, duration=9),
                           dict(id='y', source='s', start=2.01, end=3, duration=9)], 30)
        self.assertEqual(cuts[0]['end'], cuts[1]['start'])


class MultitrackTests(unittest.TestCase):
    def test_render_manifest_and_fcpxml_agree_on_the_chosen_track(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = clip(root/'two tracks.mp4', seconds=3, tracks=2)
            meta = probe(src, audio_track=1)
            from clipcompiler.selection import channels, source_channels
            item = dict(id='c', source='s', source_path=str(src), source_hash=None, start=0, end=2,
                        duration=meta['duration'], audio_stream=meta['audio_stream'], audio_channels=channels(meta),
                        audio_src_channels=source_channels(meta), audio_sources=len(meta['audio_streams']),
                        audio_total_channels=sum(s['channels'] for s in meta['audio_streams']),
                        source_width=meta['width'], source_height=meta['height'], source_fps=meta['fps'])
            self.assertEqual(item['audio_stream'], meta['audio_streams'][1]['index'])
            out = publish([item], root, dict(ffmpeg='ffmpeg', ffprobe='ffprobe', fps=15, render_height=90,
                                             workers=1, resolve=True, render=True, min_free_gb=0))
            rows = list(csv.DictReader((out/'manifest.csv').open(encoding='utf-8')))
            self.assertEqual(rows[0]['audio_stream'], str(item['audio_stream']))
            xml = ET.parse(out/'Combined.fcpxml')
            asset = xml.find('.//asset'); source = xml.find('.//audio-channel-source')
            mono = meta['audio_streams'][0]['channels']
            expected = ', '.join(str(mono+i+1) for i in range(meta['audio_streams'][1]['channels']))
            self.assertEqual((asset.attrib['audioSources'], source.attrib['srcCh']), ('2', expected))
            fmt = xml.find(f".//format[@id='{asset.attrib['format']}']")
            self.assertEqual((fmt.attrib['width'], fmt.attrib['frameDuration']), ('160', '1/15s'))
            # The render really used track 2 (880 Hz), not track 1 (440 Hz).
            wav = root/'check.wav'
            command(['ffmpeg', '-v', 'error', '-y', '-i', out/'Combined_highlights.mp4', '-ac', '1', '-ar', '16000', wav])
            from clipcompiler.media import read_audio
            import numpy as np
            audio = read_audio(wav)[:16000]
            peak = np.argmax(np.abs(np.fft.rfft(audio))) * 16000 / len(audio)
            self.assertAlmostEqual(peak, 880, delta=10)

    def test_ntsc_frame_duration(self):
        from clipcompiler.render import frame_duration
        self.assertEqual([frame_duration(f) for f in (59.94006, 60, 29.97, 0)], ['1001/60000s', '1/60s', '1001/30000s', None])


class EvaluationTests(unittest.TestCase):
    def test_favorites_land_in_both_halves_and_groups_stay_intact(self):
        groups = {i: [{'source_path': f'f{i}' if i < 3 else f'p{i}'}] for i in range(38)}
        groups[38] = [{'source_path': 'dup1'}, {'source_path': 'dup2'}]
        calibration = evaluate.split_groups(groups, {'f0', 'f1', 'f2'})
        favorite_groups = {0, 1, 2}
        self.assertTrue(favorite_groups & calibration and favorite_groups - calibration)
        self.assertEqual(sum(len(groups[g]) for g in calibration), 20)

    def test_scores_the_published_cut_list_in_shared_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = Store(root)
            for sid in 'ab':
                store.source(sid, root/f'{sid}.mp4', 'h'+sid, 'complete', {'duration': 20, 'recorded_at': 0, 'audio_stream': 0})
            # select() would also return a's 10..15 candidate; the render was limited to the top one.
            store.replace_candidates('a', [dict(id='top', source='a', source_hash='ha', start=0, end=8, score=9, recommendation='keep'),
                                           dict(id='low', source='a', source_hash='ha', start=10, end=15, score=2, recommendation='keep')])
            with store.db:
                store.db.execute('INSERT INTO alignments VALUES (?,?,?,?)', ('a', 'b', 'confirmed', json.dumps({'offset': 5, 'first_match': 5, 'last_match': 14})))
            store.set('summary', {'status': 'complete'})
            render = root/'render-'/'x'; render.mkdir(parents=True)
            (render/'selections.json').write_text(json.dumps([dict(id='top', source='a', source_path=str(root/'a.mp4'), start=0, end=8,
                event_group='a', timeline_start=0, timeline_end=8)]))
            store.set('last_render', str(render)); store.close()
            labels = root/'labels.csv'
            rows = [dict(source_path=str(root/'a.mp4'), source_hash='ha', split='held_out', known_favorite='yes', start=1, end=7, label='keep'),
                    # A reject labelled on b's copy of a's footage (b 0..2 == a 5..7) DID play.
                    dict(source_path=str(root/'b.mp4'), source_hash='hb', split='held_out', known_favorite='', start=0, end=2, label='reject'),
                    dict(source_path=str(root/'a.mp4'), source_hash='ha', split='held_out', known_favorite='', start=10, end=15, label='reject')]
            with labels.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=evaluate.LABEL_FIELDS, restval=''); writer.writeheader(); writer.writerows(rows)
            result = evaluate.measure(labels, root, 'held_out')
            self.assertEqual((result['favorite_recall'], result['unwanted_inclusions']), (1, 1))


class CliTests(unittest.TestCase):
    def test_invalid_inputs_fail_without_creating_a_run(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = root/'clips'; src.mkdir(); out = root/'out'
            base = ['--source', str(src), '--output', str(out)]
            for extra in (['--fps', '0'], ['--min-score', '11'], ['--frame-batch', '13'], ['--render-height', '91'],
                          ['--short-seconds', '121'], ['--min-dur', '5', '--max-dur', '2'], ['--preflight', '0']):
                with self.subTest(extra=extra):
                    self.assertEqual(cli.main(base+extra), 2)
            self.assertFalse(out.exists() and any(out.iterdir()))
            self.assertEqual(cli.main(['--source', str(src), '--output', str(src/'inside')]), 2)
            self.assertEqual(cli.main(['--resume', str(root/'missing')]), 2)
            with self.assertRaises(SystemExit): cli.main(base+['--laugh-threshold', '0.3'])

    def test_resume_cannot_change_inventory_and_render_cannot_change_analysis(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.set('settings', {'fps': 15}); store.close()
            self.assertEqual(cli.main(['--resume', root, '--source', 'elsewhere']), 2)
            self.assertEqual(cli.main(['--render-from', root, '--judge-model', 'other']), 2)


if __name__ == '__main__': unittest.main()
