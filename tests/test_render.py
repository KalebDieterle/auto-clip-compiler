"""Rendering with real ffmpeg on synthetic clips: atomic publish, frame joins, audio tracks, progress, cancel."""
import csv
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import xml.etree.ElementTree as ET

from clipcompiler.media import Cancelled, channels, command, probe, source_channels
from clipcompiler.render import frame_cuts, frame_duration, publish
from clipcompiler.store import file_hash
from tests.helpers import clip

SETTINGS = {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe', 'fps': 15, 'render_height': 90, 'workers': 2,
            'resolve': True, 'render': True, 'min_free_gb': 0}


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.src = clip(cls.root/"friend's video.mp4", seconds=3); cls.silent = cls.root/'silent.mp4'
        command(['ffmpeg', '-v', 'error', '-y', '-i', cls.src, '-an', '-c:v', 'copy', cls.silent])

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def items(self):
        return [dict(id=str(i), source=str(i), source_path=str(p), source_hash=file_hash(p), start=0, end=2,
                     duration=probe(p)['duration'], audio_stream=probe(p)['audio_stream'])
                for i, p in enumerate((self.src, self.silent))]

    def test_real_render_audio_and_silent_csv_xml_agree_and_previous_survives(self):
        items = self.items(); before = [file_hash(self.src), file_hash(self.silent)]
        a = publish(items, self.root, SETTINGS); original = file_hash(a/'Combined_highlights.mp4')
        b = publish(items, self.root, SETTINGS)
        self.assertNotEqual(a, b); self.assertEqual(file_hash(a/'Combined_highlights.mp4'), original)
        self.assertAlmostEqual(probe(b/'Combined_highlights.mp4')['duration'], 4, delta=.15)
        clips = ET.parse(b/'Combined.fcpxml').findall('.//asset-clip')
        self.assertEqual([c.attrib['duration'] for c in clips], ['30/15s', '30/15s'])
        self.assertEqual([file_hash(self.src), file_hash(self.silent)], before)
        self.assertEqual(len((b/'manifest.csv').read_text().splitlines()), 3)

    def test_failed_render_never_publishes_and_frees_segments(self):
        output = self.root/'failure'
        with patch('clipcompiler.render.command', side_effect=RuntimeError('encoder failed')), self.assertRaises(RuntimeError):
            publish(self.items(), output, SETTINGS)
        self.assertFalse(list(output.glob('render-/2*')))
        reports = [json.loads(p.read_text()) for p in output.rglob('render.json')]
        self.assertTrue(reports and all(r['status'] == 'failed' for r in reports))
        self.assertFalse(list(output.rglob('segments')))

    def test_changed_source_is_rejected(self):
        items = self.items(); items[0]['source_hash'] = 'not-the-content'
        with self.assertRaises(ValueError): publish(items, self.root, SETTINGS)

    def test_progress_reports_every_segment(self):
        seen = []
        publish(self.items(), self.root/'progress', SETTINGS, on_progress=lambda d, n: seen.append((d, n)))
        self.assertEqual(sorted(seen), [(1, 2), (2, 2)])

    def test_cancel_stops_ffmpeg_and_publishes_nothing(self):
        output = self.root/'cancelled'; cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            publish(self.items(), output, SETTINGS, cancel=cancel)
        self.assertFalse(list(output.glob('render-/2*'))); self.assertFalse(list(output.rglob('*.mp4')))

    def test_cancel_mid_encode_kills_the_running_ffmpeg(self):
        long = clip(self.root/'long.mp4', seconds=40)
        item = dict(id='l', source='l', source_path=str(long), source_hash=None, start=0, end=39,
                    duration=probe(long)['duration'], audio_stream=probe(long)['audio_stream'])
        cancel = threading.Event(); slow = dict(SETTINGS, render_height=1440, fps=60)
        timer = threading.Timer(.5, cancel.set); timer.start()
        with self.assertRaises(Cancelled):
            publish([item], self.root/'mid-cancel', slow, cancel=cancel)
        self.assertFalse(list((self.root/'mid-cancel').glob('render-/2*')))


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

    def test_setup_from_another_replay_never_repeats_an_instant_after_snapping(self):
        lead = dict(source='b', clip_wall=100., start=10., end=20.007, duration=30.)       # ends at wall 120.007
        core = dict(id='m', source='a', clip_wall=120., start=.007, end=5., duration=30., lead=[lead])
        c = frame_cuts([core], 60)[0]
        self.assertEqual(c['start'], 1/60)
        self.assertGreaterEqual(c['clip_wall']+c['start'], c['lead'][0]['clip_wall']+c['lead'][0]['end']-1e-9)

    def test_setup_never_repeats_the_last_frame_of_the_moment_before_it(self):
        before = dict(id='p', source='a', clip_wall=100., start=1., end=7.007, duration=30.)    # Shows up to wall 107.0167.
        lead = dict(source='a', clip_wall=100., start=7.007, end=9., duration=30.)
        after = dict(id='m', source='b', clip_wall=105., start=4.5, end=8., duration=30., lead=[lead])
        cuts = frame_cuts([before, after], 60)
        self.assertEqual((cuts[0]['end'], cuts[1]['lead'][0]['start']), (421/60, 421/60))

    def test_earlier_setup_from_the_previous_moments_recording_is_kept(self):
        before = dict(id='p', source='a', clip_wall=100., start=10., end=15., duration=30.)
        after = dict(id='m', source='b', clip_wall=105., start=20., end=24., duration=30.,
                     lead=[dict(source='a', clip_wall=100., start=0., end=5., duration=30.)])
        self.assertEqual([(s['start'], s['end']) for s in frame_cuts([before, after], 60)[1]['lead']], [(0., 5.)])

    def test_a_setup_slice_inside_the_previous_moment_is_dropped_and_the_next_one_reconciled(self):
        before = dict(id='p', source='a', clip_wall=100., start=1., end=7.007, duration=30.)
        lead = [dict(source='a', clip_wall=100., start=2., end=6., duration=30.),
                dict(source='a', clip_wall=100., start=7.007, end=9., duration=30.)]
        after = dict(id='m', source='b', clip_wall=105., start=4.5, end=8., duration=30., lead=lead)
        self.assertEqual([(s['start'], s['end']) for s in frame_cuts([before, after], 60)[1]['lead']], [(421/60, 9.)])

    def test_every_setup_slice_is_checked_against_the_previous_moment(self):
        before = dict(id='p', source='a', clip_wall=100., start=8., end=10.007, duration=30.)   # Shows up to 601/60.
        lead = [dict(source='a', clip_wall=100., start=5., end=6., duration=30.),
                dict(source='a', clip_wall=100., start=10.007, end=12., duration=30.)]
        after = dict(id='m', source='b', clip_wall=105., start=9., end=12., duration=30., lead=lead)
        self.assertEqual([(s['start'], s['end']) for s in frame_cuts([before, after], 60)[1]['lead']], [(5., 6.), (601/60, 12.)])

    def test_ntsc_frame_duration(self):
        self.assertEqual([frame_duration(f) for f in (59.94006, 60, 29.97, 0)], ['1001/60000s', '1/60s', '1001/30000s', None])


def dominant_frequency(path):
    """Zero crossings per second / 2 of a mono 16 kHz WAV: plenty for telling 440 Hz from 880 Hz."""
    with wave.open(str(path)) as reader:
        frames = reader.readframes(reader.getnframes()); rate = reader.getframerate()
    samples = [int.from_bytes(frames[i:i+2], 'little', signed=True) for i in range(0, min(len(frames), rate*2), 2)]
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples)/rate)


class MultitrackTests(unittest.TestCase):
    def test_render_manifest_and_fcpxml_agree_on_the_chosen_track(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = clip(root/'two tracks.mp4', seconds=3, tracks=2)
            meta = probe(src, audio_track=1)
            item = dict(id='c', source='s', source_path=str(src), source_hash=None, start=0, end=2,
                        duration=meta['duration'], audio_stream=meta['audio_stream'], audio_streams=[meta['audio_stream']],
                        audio_channels=channels(meta),
                        audio_src_channels=source_channels(meta), audio_sources=len(meta['audio_streams']),
                        audio_total_channels=sum(s['channels'] for s in meta['audio_streams']),
                        source_width=meta['width'], source_height=meta['height'], source_fps=meta['fps'])
            self.assertEqual(item['audio_stream'], meta['audio_streams'][1]['index'])
            out = publish([item], root, dict(SETTINGS, workers=1))
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
            self.assertAlmostEqual(dominant_frequency(wav), 880, delta=25)


class SetupTests(unittest.TestCase):
    def test_setup_from_another_replay_plays_first_in_one_segment(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            before = clip(root/'before.mp4', seconds=3, tracks=2, frequency=300)   # Mixed: 300 + 600 Hz.
            after = clip(root/'after.mp4', seconds=3, frequency=1000)
            lead = dict(source='b', source_path=str(before), source_hash=file_hash(before), start=1, end=2.5,
                        duration=probe(before)['duration'], evidence=['line'])
            item = dict(id='m', source='a', source_path=str(after), source_hash=file_hash(after), start=0, end=2,
                        duration=probe(after)['duration'], lead=[lead])
            out = publish([item, dict(item, id='n', lead=None, start=2.2, end=2.9)], root, dict(SETTINGS, workers=1, loudnorm=True))
            self.assertAlmostEqual(probe(out/'Combined_highlights.mp4')['duration'], 1.5+2+.7, delta=.15)
            rows = list(csv.DictReader((out/'manifest.csv').open(encoding='utf-8')))
            self.assertEqual([(r['candidate_id'], r['recommendation'], Path(r['source_path']).name) for r in rows],
                             [('m', 'setup', 'before.mp4'), ('m', 'keep', 'after.mp4'), ('n', 'keep', 'after.mp4')])
            clips = ET.parse(out/'Combined.fcpxml').findall('.//asset-clip')
            self.assertEqual([c.attrib['duration'] for c in clips], ['23/15s', '30/15s', '11/15s'])
            # The setup really plays first: the opening second carries the older replay's tones.
            wav = root/'head.wav'
            command(['ffmpeg', '-v', 'error', '-y', '-i', out/'Combined_highlights.mp4', '-t', '1', '-ac', '1', '-ar', '16000', wav])
            self.assertNotAlmostEqual(dominant_frequency(wav), 1000, delta=60)


if __name__ == '__main__':
    unittest.main()
