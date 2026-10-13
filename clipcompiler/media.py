import datetime as dt
import json
import math
import re
import shutil
import subprocess
import wave
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.flv', '.m4v', '.avi', '.webm'}
MEDIA_VERSION = 2


def command(args, timeout=3600):
    cp = subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout)
    if cp.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} exit {cp.returncode}: {cp.stderr.decode("utf-8", "replace")[-2000:]}')
    return cp.stdout


def probe(path, ffprobe='ffprobe', audio_track=None):
    data = json.loads(command([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path], 60))
    videos = [s for s in data['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not videos:
        raise ValueError('No video stream')
    video = videos[0]
    duration = float(data.get('format', {}).get('duration') or video.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid video duration')
    audio = [s for s in data['streams'] if s['codec_type'] == 'audio']
    warning = None
    if audio_track is not None:
        if not 0 <= audio_track < len(audio):
            raise ValueError(f'Audio track {audio_track} does not exist')
        selected = audio[audio_track]
    elif len(audio) <= 1:
        selected = audio[0] if audio else None
    else:
        combined = [s for s in audio if re.search(r'\b(mix|mixed|combined|all)\b', s.get('tags', {}).get('title', ''), re.I)]
        defaults = [s for s in audio if s.get('disposition', {}).get('default')]
        options = combined if len(combined) == 1 else defaults
        selected = options[0] if len(options) == 1 else None
        if selected is None:
            warning = 'Ambiguous audio tracks: configure --audio-track (zero-based audio index)'
    rate = video.get('avg_frame_rate', '30/1').split('/')
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 30
    return {'duration': duration, 'width': video['width'], 'height': video['height'], 'fps': fps,
            'video_stream': video['index'], 'audio_stream': selected['index'] if selected else None,
            'audio_count': len(audio), 'audio_warning': warning, 'audio_streams': audio}


def recording_time(path):
    match = re.search(r'(\d{4}-\d{2}-\d{2})[ _](\d{1,2})[-.](\d{2})[-.](\d{2})(?:[ _]?(AM|PM))?', path.stem, re.I)
    if match:
        date, hour, minute, second, ampm = match.groups()
        hour = int(hour)
        if ampm:
            hour = hour % 12 + (12 if ampm.upper() == 'PM' else 0)
        try:
            return dt.datetime.fromisoformat(f'{date} {hour:02}:{minute}:{second}').timestamp(), True
        except ValueError:
            pass
    return path.stat().st_mtime, False


def windows(duration, short=60, size=30, overlap=10):
    if duration <= short:
        return [(0., duration)]
    result = []
    start = 0.
    while start < duration:
        result.append((start, min(duration, start + size)))
        if start + size >= duration:
            break
        start += size - overlap
    return result


def frames(path, start, end, fps, directory, ffmpeg='ffmpeg'):
    """Cache images independently of model/prompt. Relative PTS are explicit."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / 'frames.json'
    if marker.exists():
        result = json.loads(marker.read_text())
        if all(Path(f['path']).is_file() for f in result):
            return result
    # fps filter emits frame centres at the requested sampling interval.
    command([ffmpeg, '-v', 'error', '-y', '-ss', start, '-i', path, '-t', end-start,
             '-vf', f'fps={fps}:start_time=0,scale=640:640:force_original_aspect_ratio=decrease',
             '-q:v', '3', directory / '%06d.jpg'])
    result = [{'time': start + i / fps, 'path': str(p.resolve())}
              for i, p in enumerate(sorted(directory.glob('*.jpg'))) if start+i/fps < end]
    if not result:
        raise RuntimeError('Frame extraction produced no images')
    from .store import atomic_json
    atomic_json(marker, result)
    return result


def extract_audio(path, start, end, stream, dest, ffmpeg='ffmpeg'):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        temp = dest.with_suffix('.tmp.wav')
        command([ffmpeg, '-v', 'error', '-y', '-ss', start, '-i', path, '-t', end-start,
                 '-map', f'0:{stream}', '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp])
        temp.replace(dest)
    return dest


def read_audio(path):
    import numpy as np
    with wave.open(str(path)) as reader:
        if reader.getframerate() != 16000 or reader.getnchannels() != 1:
            raise ValueError('Expected 16k mono WAV')
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype(np.float32) / 32768


class AudioAnalyzer:
    def __init__(self, model='small', yamnet=None):
        self.model_name = model
        self.whisper = None
        self.yamnet_path = yamnet
        self.yamnet = None

    def load(self):
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from .store import file_hash, identity
        model_path=Path(self.model_name)
        if not model_path.is_dir(): model_path=Path(download_model(self.model_name,local_files_only=True))
        self.model_digest=identity([(p.name,file_hash(p)) for p in sorted(model_path.iterdir()) if p.is_file()])
        self.yamnet_digest=None
        self.whisper = WhisperModel(str(model_path), device='cpu', compute_type='int8', local_files_only=True)
        if self.yamnet_path:
            if not Path(self.yamnet_path).is_dir(): raise ValueError('YAMNet requires a downloaded local model directory')
            self.yamnet_digest=identity([(str(p.relative_to(self.yamnet_path)),file_hash(p))
                                        for p in sorted(Path(self.yamnet_path).rglob('*')) if p.is_file()])
            import tensorflow_hub as hub
            self.yamnet = hub.load(self.yamnet_path)

    def transcribe(self, wav, offset):
        if self.whisper is None:
            self.load()
        audio = read_audio(wav)
        segments, info = self.whisper.transcribe(audio, beam_size=5, word_timestamps=True, vad_filter=True)
        speech = [{'start': offset+s.start, 'end': offset+s.end, 'text': s.text,
                   'words': [{'start': offset+w.start, 'end': offset+w.end, 'text': w.word} for w in (s.words or [])]}
                  for s in segments]
        return {'status': 'speech' if speech else 'empty_speech', 'segments': speech, 'language': info.language}

    def evidence(self, wav, offset):
        import numpy as np
        audio = read_audio(wav)
        energy = [{'time': offset+i/16000, 'rms': float(np.sqrt(np.mean(audio[i:i+16000] ** 2)))}
                  for i in range(0, len(audio), 16000)]
        laughs = []
        if self.yamnet_path:
            if self.yamnet is None:
                import tensorflow_hub as hub
                self.yamnet = hub.load(self.yamnet_path)
            scores, _, _ = self.yamnet(audio)
            laughs = [{'time': offset+i*.48, 'probability': float(row[[13,15,18]].max())}
                      for i, row in enumerate(scores.numpy())]
        return {'energy': energy, 'laughter': laughs, 'yamnet': 'complete' if self.yamnet_path else 'not_configured'}


def media_preflight(output, ffmpeg, ffprobe, min_free_gb=2):
    output.mkdir(parents=True, exist_ok=True)
    command([ffmpeg, '-version'], 15)
    command([ffprobe, '-version'], 15)
    if shutil.disk_usage(output).free < min_free_gb * 1024**3:
        raise RuntimeError(f'Need at least {min_free_gb:g} GiB free on output volume')
    from .store import atomic_json
    test = output / '.permission-check.json'
    atomic_json(test, {'ok': True})
    test.unlink()
