import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.flv', '.m4v', '.avi', '.webm'}
# A windowed (no-console) app would otherwise flash a console for every ffmpeg call.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class Cancelled(Exception):
    """The user cancelled; whatever was running was stopped cleanly."""


def command(args, timeout=3600, cancel=None):
    """Run a tool and return stdout; with a `cancel` Event, kill it as soon as the event is set."""
    args = [str(x) for x in args]
    if cancel is None:
        cp = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        code, stdout, stderr = cp.returncode, cp.stdout, cp.stderr
    else:
        if cancel.is_set(): raise Cancelled()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=.2); break
            except subprocess.TimeoutExpired:
                if cancel.is_set() or time.monotonic() > deadline:
                    proc.kill(); proc.communicate()
                    if cancel.is_set(): raise Cancelled()
                    raise subprocess.TimeoutExpired(args, timeout)
        code = proc.returncode
    if code:
        raise RuntimeError(f'{Path(args[0]).name} exit {code}: {stderr.decode("utf-8", "replace")[-2000:]}')
    return stdout


def find_ffmpeg():
    """(ffmpeg, ffprobe): the copy bundled beside the app wins, then PATH. None when missing."""
    roots = [Path(getattr(sys, '_MEIPASS', '')), Path(sys.executable).parent, Path(__file__).resolve().parent.parent]
    for root in roots:
        bundled = root/'ffmpeg'
        if (bundled/'ffmpeg.exe').is_file() and (bundled/'ffprobe.exe').is_file():
            return str(bundled/'ffmpeg.exe'), str(bundled/'ffprobe.exe')
    found = shutil.which('ffmpeg'), shutil.which('ffprobe')
    return found if all(found) else None


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


def channels(data):
    stream = next((s for s in data.get('audio_streams', []) if s.get('index') == data.get('audio_stream')), None)
    return stream.get('channels', 2) if stream else None


def source_channels(data):
    """1-based positions of the chosen track's channels among all of the file's audio channels."""
    position = 0
    for s in data.get('audio_streams', []):
        count = s.get('channels', 2)
        if s.get('index') == data.get('audio_stream'):
            return ', '.join(str(position+i+1) for i in range(count))
        position += count
    return None


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
