"""Multi-track audio with real ffmpeg: which tracks a clip plays, and that judge uploads, skill close-ups
and renders all carry that sound (nothing muted, nothing doubled, single-track clips unchanged)."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import xml.etree.ElementTree as ET

from clipcompiler import gemini, media
from clipcompiler.media import audio_plan, command, is_silent, plan_by_sound, probe, track_name
from clipcompiler.render import publish
from tests.helpers import clip
from tests.test_render import SETTINGS


def tone(frequency, seconds=3, volume=.3):
    return f'sine=frequency={frequency}:sample_rate=48000:duration={seconds},volume={volume}'


def noise(seed, seconds=3, color='pink'):
    return f'anoisesrc=color={color}:seed={seed}:amplitude=0.25:sample_rate=48000:duration={seconds}'


def mixed(*sources):
    """One lavfi graph that sums sources at full level, the way a recorder writes its mix track."""
    labels = ''.join(f'{s}[s{i}];' for i, s in enumerate(sources))
    return labels + ''.join(f'[s{i}]' for i in range(len(sources))) + f'amix=inputs={len(sources)}:normalize=0[out0]'


SILENCE = 'anullsrc=channel_layout=mono:sample_rate=48000,atrim=duration=3'


def recording(path, tracks, seconds=3, names=None, defaults=None, layouts=None, codec=('aac', '-b:a', '128k')):
    """A clip with one audio track per lavfi source. names -> handler_name; defaults -> disposition flags;
    layouts -> 'mono'/'stereo' per track (stereo by default, like game audio)."""
    args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size=160x90:rate=15:duration={seconds}']
    for source in tracks:
        args += ['-f', 'lavfi', '-i', source]
    args += ['-map', '0:v']
    for t in range(len(tracks)):
        args += ['-map', f'{t+1}:a']
        args += [f'-ac:a:{t}', '1' if layouts and layouts[t] == 'mono' else '2']
        if names: args += [f'-metadata:s:a:{t}', f'handler_name={names[t]}']
        if defaults is not None: args += [f'-disposition:a:{t}', 'default' if defaults[t] else '0']
    command(args + ['-c:v', 'libx264', '-g', '15', '-c:a', *codec, '-shortest', path])
    return path


def samples(path, rate=16000):
    """Mono 16-bit samples of a file's audio."""
    wav = Path(path).with_suffix('.check.wav')
    command(['ffmpeg', '-v', 'error', '-y', '-i', path, '-vn', '-ac', '1', '-ar', rate, wav])
    with wave.open(str(wav)) as reader:
        frames = reader.readframes(reader.getnframes())
    return [int.from_bytes(frames[i:i+2], 'little', signed=True) for i in range(0, len(frames), 2)]


def level(pcm, frequency, rate=16000):
    """Amplitude of one frequency (Goertzel) over the middle second, away from fades and padding."""
    middle = pcm[len(pcm)//2-rate//2:len(pcm)//2+rate//2]
    k = 2*math.cos(2*math.pi*frequency/rate); a = b = 0.
    for x in middle: a, b = x + k*a - b, a
    return math.sqrt(max(a*a + b*b - k*a*b, 0))/len(middle)*2


def streams(path):
    return [s['index'] for s in probe(path)['audio_streams']]


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def plan(self, name, tracks, **kw):
        return streams_and_plan(recording(self.root/name, tracks, **kw))

    def test_single_track_plays_as_is_without_any_analysis(self):
        src = clip(self.root/'one.mp4', seconds=2)
        with patch('clipcompiler.media.track_samples', side_effect=AssertionError('analysed')):
            all_streams, plan = streams_and_plan(src)
        self.assertEqual(plan, all_streams); self.assertEqual(len(plan), 1)

    def test_no_audio_is_an_empty_plan(self):
        src = self.root/'mute.mp4'
        command(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=15:duration=2',
                 '-c:v', 'libx264', src])
        self.assertEqual(audio_plan(src)[1], [])

    def test_game_and_mic_tracks_are_mixed(self):
        all_streams, plan = self.plan('game-mic.mp4', [noise(1), noise(2, color='brown')], layouts=['stereo', 'mono'])
        self.assertEqual(plan, all_streams)

    def test_default_flag_no_longer_picks_one_track(self):
        """The friend's bug: track 1 flagged default used to play alone (no mic)."""
        all_streams, plan = self.plan('flagged.mp4', [tone(200), tone(330)], defaults=[True, False])
        self.assertEqual(plan, all_streams)

    def test_no_default_flag_no_longer_mutes(self):
        all_streams, plan = self.plan('unflagged.mp4', [tone(200), tone(330)], defaults=[False, False])
        self.assertEqual(plan, all_streams)

    def test_two_default_flags_no_longer_mute(self):
        """1.1.1 found no single default track here and rendered silence (reproduced 2026-10-15)."""
        all_streams, plan = self.plan('both-flagged.mp4', [tone(200), tone(330)], defaults=[True, True])
        self.assertEqual(plan, all_streams)

    def test_premix_first_plays_alone(self):
        game, mic = noise(1), noise(2, color='brown')
        all_streams, plan = self.plan('premix-first.mp4', [mixed(game, mic), game, mic])
        self.assertEqual(plan, all_streams[:1])

    def test_premix_last_plays_alone(self):
        game, mic, chat = noise(1), noise(2, color='brown'), tone(310)
        all_streams, plan = self.plan('premix-last.mp4', [game, mic, chat, mixed(game, mic, chat)])
        self.assertEqual(plan, all_streams[3:])

    def test_premix_with_quieter_mic_still_detected(self):
        game, mic = noise(1), noise(2, color='brown')
        quiet = noise(2, color='brown') + ',volume=0.4'
        all_streams, plan = self.plan('premix-quiet.mp4', [mixed(game, quiet), game, mic])
        self.assertEqual(plan, all_streams[:1])

    def test_duplicate_track_is_dropped_not_doubled(self):
        game, mic = noise(1), noise(2, color='brown')
        all_streams, plan = self.plan('dupe.mp4', [game, mic, game])
        self.assertEqual(plan, all_streams[:2])

    def test_mix_with_a_silent_mic_keeps_the_mic_track(self):
        """Mix == game while nobody talks: the game copy is dropped, the mix and the (silent) mic stay."""
        game = noise(1)
        all_streams, plan = self.plan('quiet-mic.mp4', [game, game, SILENCE])
        self.assertEqual(plan, [all_streams[0], all_streams[2]])

    # Review findings (Codex, 2026-10-18): each would have dropped real sound or cancelled it.
    def test_quiet_mic_under_game_is_not_a_duplicate(self):
        game = noise(1)
        all_streams, plan = self.plan('game-plus-quiet-mic.mp4', [game, mixed(game, noise(2, color='white') + ',volume=0.2')])
        self.assertEqual(plan, all_streams)

    def test_high_pitched_extra_is_not_a_duplicate(self):
        game = noise(1, color='brown')
        all_streams, plan = self.plan('game-plus-1500.mp4', [game, mixed(game, tone(1500, volume=.3))])
        self.assertEqual(plan, all_streams)

    def test_partial_premix_does_not_replace_a_track_with_unique_sound(self):
        """[G + 0.5C, G, C + 0.3M]: track 1 looks like a mix of the others but lacks M."""
        g, c, m = noise(1), noise(3, color='white'), noise(2, color='brown')
        all_streams, plan = self.plan('partial-premix.mp4', [mixed(g, c + ',volume=0.5'), g, mixed(c, m + ',volume=0.3')])
        self.assertEqual(plan, all_streams)

    def test_inverted_copy_is_dropped_instead_of_cancelling(self):
        game = noise(1)
        all_streams, plan = self.plan('inverted.mp4', [game, game + ',aeval=-val(0)'])
        self.assertEqual(plan, all_streams[:1])

    def test_named_mix_track_wins_without_analysis(self):
        src = recording(self.root/'named.mp4', [tone(200), tone(330), tone(200)], names=['Game', 'Microphone', 'Mix'])
        with patch('clipcompiler.media.track_samples', side_effect=AssertionError('analysed')):
            all_streams, plan = streams_and_plan(src)
        self.assertEqual(plan, all_streams[2:])

    def test_generic_handler_names_are_not_names(self):
        self.assertEqual(track_name({'tags': {'handler_name': 'SoundHandler'}}), '')
        self.assertEqual(track_name({'tags': {'title': '', 'HANDLER_NAME': 'Game/Microphone'}}), 'Game/Microphone')
        self.assertEqual(track_name({'tags': {'title': 'All Audio'}}), 'All Audio')
        self.assertTrue(media.MIX_NAME.search(track_name({'tags': {'handler_name': 'Master Mix'}})))

    def test_failed_analysis_mixes_everything(self):
        src = recording(self.root/'broken-analysis.mp4', [tone(200), tone(330)])
        with patch('clipcompiler.media.track_samples', side_effect=RuntimeError('ffmpeg exit 1')):
            all_streams, plan = streams_and_plan(src)
        self.assertEqual(plan, all_streams)

    def test_plan_is_remembered_per_file(self):
        src = recording(self.root/'memo.mp4', [tone(200), tone(330)])
        first = audio_plan(src)[1]
        with patch('clipcompiler.media.track_samples', side_effect=AssertionError('analysed twice')):
            self.assertEqual(audio_plan(src)[1], first)


def streams_and_plan(path):
    return streams(path), audio_plan(path)[1]


class PipelineTests(unittest.TestCase):
    """Judge upload, skill close-up and render carry the plan's sound."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.pair = recording(cls.root/'game-mic.mp4', [tone(200), tone(330)], defaults=[True, False])
        cls.premix = recording(cls.root/'premix.mp4', [mixed(tone(200), tone(330)), tone(200), tone(330)])
        cls.single = recording(cls.root/'single.mp4', [mixed(tone(200), tone(330))])

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def item(self, src, **extra):
        return dict(id=src.stem, source=src.stem, source_path=str(src), source_hash=None, start=0, end=2,
                    duration=probe(src)['duration'], audio_stream=None, **extra)

    def render(self, *items, **settings):
        out = publish(list(items), self.root/'out', dict(SETTINGS, workers=1, **settings))
        return out, samples(out/'Combined_highlights.mp4')

    def test_render_of_two_tracks_has_both(self):
        _, pcm = self.render(self.item(self.pair))
        self.assertGreater(level(pcm, 200), 300); self.assertGreater(level(pcm, 330), 300)

    def test_premix_is_not_doubled(self):
        """The premix render matches a single-track render of the same mix within 1 dB."""
        _, mix = self.render(self.item(self.premix)); _, ref = self.render(self.item(self.single))
        for f in (200, 330):
            self.assertAlmostEqual(20*math.log10(level(mix, f)/level(ref, f)), 0, delta=1)

    def test_mixing_does_not_lower_either_track(self):
        """amix normalize=0: each source keeps its level (a normalized mix would halve both)."""
        _, pair = self.render(self.item(self.pair)); _, ref = self.render(self.item(self.single))
        for f in (200, 330):
            self.assertAlmostEqual(20*math.log10(level(pair, f)/level(ref, f)), 0, delta=1)

    def test_saved_item_from_an_older_version_renders_with_sound(self):
        """Compilations saved by 1.1.1 carry audio_stream=None for these clips; render decides again."""
        out, pcm = self.render(self.item(self.pair))
        self.assertGreater(level(pcm, 330), 300)
        self.assertEqual(json.loads((out/'render.json').read_text())['silent_segments'], [])

    def test_loudnorm_still_applies_to_a_mix(self):
        _, pcm = self.render(self.item(self.pair), loudnorm=True)
        self.assertGreater(level(pcm, 200), 300); self.assertGreater(level(pcm, 330), 300)

    def test_mixed_and_single_and_silent_segments_join(self):
        silent = self.root/'no-audio.mp4'
        command(['ffmpeg', '-v', 'error', '-y', '-i', self.single, '-an', '-c:v', 'copy', silent])
        out, _ = self.render(self.item(self.pair), self.item(self.single), self.item(silent))
        self.assertAlmostEqual(probe(out/'Combined_highlights.mp4')['duration'], 6, delta=.2)

    def test_fcpxml_lists_every_mixed_channel(self):
        out, _ = self.render(self.item(self.pair), resolve=True)
        xml = ET.parse(out/'Combined.fcpxml'); asset = xml.find('.//asset')
        self.assertEqual((asset.attrib['hasAudio'], asset.attrib['audioSources'], asset.attrib['audioChannels']), ('1', '2', '4'))
        self.assertEqual(xml.find('.//audio-channel-source').attrib['srcCh'], '1, 2, 3, 4')

    def test_silent_segment_is_reported(self):
        quiet = recording(self.root/'digital-silence.mp4', [SILENCE, SILENCE])
        out, _ = self.render(self.item(quiet))
        self.assertEqual(json.loads((out/'render.json').read_text())['silent_segments'], ['digital-silence'])

    def test_judge_upload_hears_both_tracks(self):
        small = gemini.proxy(self.pair, self.root, 'ffmpeg')
        pcm = samples(small)
        self.assertGreater(level(pcm, 200), 300); self.assertGreater(level(pcm, 330), 300)

    def test_skill_close_up_hears_both_tracks(self):
        panel = {'x': .7, 'y': .7, 'w': .3, 'h': .3}
        small = media.window(self.pair, .5, 2.5, panel, self.root, 'ffmpeg', fps=12)
        pcm = samples(small)
        self.assertGreater(level(pcm, 200), 300); self.assertGreater(level(pcm, 330), 300)
        self.assertAlmostEqual(probe(small)['duration'], 2, delta=.2)

    def test_review_preview_hears_both_tracks(self):
        import threading
        from types import SimpleNamespace
        from clipcompiler.ui.api import Api
        m = {'id': 'pair-preview', 'source_path': str(self.pair)}
        stub = SimpleNamespace(_run={'moments': [m]}, _ffmpeg=('ffmpeg', 'ffprobe'), _server=None,
                               _media_gate=threading.Lock(), _preview_gate=threading.Lock(),
                               _view=lambda m: {'window_start': 0, 'window_end': 2.5})
        with patch('clipcompiler.ui.api.paths.previews_dir', return_value=self.root/'previews'):
            path = Api._media(stub, 'pair-preview', 'mp4')
        self.assertTrue(path.endswith('a1.mp4'))
        pcm = samples(path)
        self.assertGreater(level(pcm, 200), 300); self.assertGreater(level(pcm, 330), 300)

    def test_mix_that_cancels_falls_back_to_one_track(self):
        """Even a pinned mix of a track and its exact inverse (lossless audio) never ships silence."""
        src = recording(self.root/'cancelling.mkv', [noise(1), noise(1) + ',aeval=-val(0)'], codec=('pcm_s16le',))
        with self.assertLogs('clipcompiler.render', 'WARNING') as logs:
            out, pcm = self.render(self.item(src, audio_streams=streams(src)))
        self.assertIn('used track', logs.output[0])
        self.assertGreater(max(map(abs, pcm)), 1000)
        self.assertEqual(json.loads((out/'render.json').read_text())['silent_segments'], [])

    def test_saved_channel_selection_follows_the_new_plan(self):
        out, _ = self.render(self.item(self.pair, audio_src_channels='1, 2', audio_sources=1), resolve=True)
        xml = ET.parse(out/'Combined.fcpxml')
        self.assertEqual(xml.find('.//audio-channel-source').attrib['srcCh'], '1, 2, 3, 4')
        self.assertEqual(xml.find('.//asset').attrib['audioSources'], '2')

    def test_cancel_stops_audio_planning(self):
        import threading
        from clipcompiler.media import Cancelled
        stop = threading.Event(); stop.set()
        with patch('clipcompiler.render.audio_plan', side_effect=AssertionError('planned after cancel')),                 self.assertRaises(Cancelled):
            publish([self.item(self.pair), self.item(self.premix)], self.root/'cancelled', dict(SETTINGS, workers=1), cancel=stop)

    def test_opposite_phase_stereo_is_not_silent(self):
        src = recording(self.root/'phase.mp4', [noise(1) + ',aeval=val(0)|-val(0):c=stereo'])
        self.assertFalse(is_silent(src))

    def test_is_silent(self):
        self.assertFalse(is_silent(self.single))
        self.assertTrue(is_silent(recording(self.root/'hush.mp4', [SILENCE])))


if __name__ == '__main__':
    unittest.main()
