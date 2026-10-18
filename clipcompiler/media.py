from array import array
import datetime as dt
import json
import logging
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

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
    # Installed layout: {app}\python\pythonw.exe and {app}\ffmpeg\ sit side by side.
    roots = [Path(sys.executable).parent.parent, Path(sys.executable).parent, Path(__file__).resolve().parent.parent]
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
        combined = [s for s in audio if MIX_NAME.search(track_name(s))]
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


def source_channels(data, plan=None):
    """1-based positions of the chosen tracks' channels among all of the file's audio channels.
    `plan` is a list of stream indexes; without it, the probe's single chosen track."""
    chosen = set(plan) if plan is not None else {data.get('audio_stream')}
    position, found = 0, []
    for s in data.get('audio_streams', []):
        count = s.get('channels', 2)
        if s.get('index') in chosen:
            found += [str(position+i+1) for i in range(count)]
        position += count
    return ', '.join(found) or None


# --- multi-track audio ------------------------------------------------------------------------------
# OBS, SteelSeries Moments, ShadowPlay and others can record game, mic and Discord as separate tracks.
# Every judge upload and render plays one "audio plan": the stream indexes whose sum is the clip's sound.

MIX_NAME = re.compile(r'\b(mix|mixed|combined|all|master)\b', re.I)
# Muxers stamp these on unnamed tracks; they say nothing about the content.
GENERIC_NAME = re.compile(r'^(sound ?handler|sound media handler|core media audio|iso media file produced by google.*|)$', re.I)
# Keeping a track wrongly only makes the mix louder (loudnorm and the limiter absorb it); dropping one
# wrongly loses a voice. Every threshold below leans towards keeping.
ANALYSIS_RATE, ANALYSIS_SECONDS = 4000, 60   # Mono samples per second (voice formants survive) and seconds compared.
SILENT_RMS = 33                              # About -60 dBFS on 16-bit samples. Silent tracks are always kept.
DUPLICATE_CORR = .999                        # Only a near-exact copy of an earlier track is dropped...
INVERTED_CORR = -.9                          # ...or an inverted one, which would cancel it in the mix.
PREMIX_FIT, PREMIX_GAIN = .97, .2            # A track the others explain this well, each audibly, may be their mix,
PREMIX_RESIDUAL = .05                        # if what it lacks is small next to the quietest track it would replace.
CONTAIN_GAIN = (.99, 1.01)                   # A track found inside another at its own level ([mix, mic])...
CONTAIN_SHARE = .1                           # ...making up an audible part of it (>= 1% of its energy)...
CONTAIN_POOL_GAIN, CONTAIN_POOL_SHARE = (.95, 1.05), .6  # ...and near that level in its loud and quiet moments
CONTAIN_POOL_MIN = .5                        # (where too faint to measure that, mostly there) is dropped.
CONTAIN_MIN_SECONDS = 15                     # Shorter, chance overlap of unrelated sounds hides ~5% of extra sound.
CONTAIN_WINDOW = .25                         # Seconds per moment. With 1 s, AAC dropping some quiet mic under loud
# game in the mix pulled real clips' quiet half to .93-.99; at .25 s it reads ~1.00, hence the wider band.
# Real [mix, mic] clips measured .999-1.001 overall; one that clips, or whose quiet mic AAC thins in the mix
# (synthetic noise 'mics' read .98-.99), falls back to mixing both, as 1.1.2 did. Limits: two
# tracks sharing a source, each with its own extra sound, look contained when the extra is under ~1% of
# the track (~20 dB below the shared sound), or when the shared source sits louder in the kept track by
# about what the extra adds (e.g. [1.1 game + chat, game + mic 10 dB down]). OBS can't record the second:
# a source's volume applies to every track.
_plans, _plans_lock = {}, threading.Lock()


def track_name(stream):
    """A track's real name: MP4 keeps it in handler_name more often than in title."""
    tags = {k.lower(): v for k, v in (stream.get('tags') or {}).items()}
    for key in ('title', 'handler_name'):
        name = str(tags.get(key) or '').strip()
        if not GENERIC_NAME.match(name): return name
    return ''


def ffprobe_beside(ffmpeg):
    """The ffprobe that ships next to `ffmpeg` (the bundled copy, or the one on PATH)."""
    path = Path(ffmpeg)
    return str(path.with_name(path.name.replace('ffmpeg', 'ffprobe'))) if path.parent != Path('.') else 'ffprobe'


def audio_plan(path, ffmpeg='ffmpeg', ffprobe=None, data=None, cancel=None):
    """(probe data, stream indexes to play). [] = no sound. One track plays as is. Several play as one mix,
    unless a track is named as the mix or the sound shows it already holds the others, so nothing is muted
    and nothing is doubled. Remembered per file (path, size, mtime) for the life of the process."""
    path = Path(path); stat = path.stat(); key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _plans_lock:
        if key in _plans: return _plans[key]
    data = data or probe(path, ffprobe or ffprobe_beside(ffmpeg))
    audio = data['audio_streams']
    named = [s for s in audio if MIX_NAME.search(track_name(s))]
    if len(audio) <= 1: plan = [s['index'] for s in audio]
    elif len(named) == 1: plan = [named[0]['index']]
    else:
        try:
            plan = plan_by_sound(path, audio, ffmpeg, cancel)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            log.warning('%s: could not compare audio tracks (%s); mixing all of them', path.name, exc)
            plan = [s['index'] for s in audio]
        log.info('%s: %d audio tracks, playing %s', path.name, len(audio), plan)
    with _plans_lock: _plans[key] = (data, plan)
    return data, plan


def track_samples(path, audio, ffmpeg, cancel=None):
    """Each track as mono 16-bit samples at ANALYSIS_RATE, aligned, from one ffmpeg pass. Every track goes
    through stereo first, as playback and the mix do, so a mono mic compares at the level it plays at
    (ffmpeg spreads mono to stereo at -3 dB a side; straight to mono it would read 3 dB louder)."""
    labels = ''.join(f'[0:{s["index"]}]aresample={ANALYSIS_RATE},aformat=channel_layouts=stereo,'
                     f'aformat=sample_fmts=s16:channel_layouts=mono[t{n}];'
                     for n, s in enumerate(audio))
    graph = labels + ''.join(f'[t{n}]' for n in range(len(audio))) + f'amerge=inputs={len(audio)}[out]'
    raw = command([ffmpeg, '-v', 'error', '-t', ANALYSIS_SECONDS, '-i', path, '-filter_complex', graph,
                   '-map', '[out]', '-f', 's16le', '-'], 120, cancel=cancel)
    n = len(audio); pcm = array('h'); pcm.frombytes(raw[:len(raw)//(2*n)*2*n])
    if len(pcm) < n*ANALYSIS_RATE//2: raise ValueError('under half a second of audio')
    return [pcm[i::n] for i in range(n)]


def _centered(samples):
    mean = sum(samples)/len(samples)
    return [x-mean for x in samples]


def _dot(a, b): return math.fsum(map(operator.mul, a, b))


def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting; None when the system is singular."""
    n = len(vector); rows = [list(r)+[v] for r, v in zip(matrix, vector)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-9: return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(n):
            if r != col:
                factor = rows[r][col]/rows[col][col]
                rows[r] = [a-factor*b for a, b in zip(rows[r], rows[col])]
    return [rows[i][n]/rows[i][i] for i in range(n)]


def _inside(tracks, energy, j, i):
    """Whether all of track j's sound is in track i at j's own level (i = j + other sound), so playing i
    alone loses nothing of j. A recorder's [mix, mic] layout is the case: the mix holds the mic and the game.
    Checked over the whole sample, then again over the half of the seconds where j is loudest and
    the half where it is quietest, so two tracks that share a source at different levels and only match
    on average are not taken for a track and its mix."""
    if energy[i] < energy[j]: return False  # j plus other sound can't be quieter than j.
    if len(tracks[i]) < CONTAIN_MIN_SECONDS*ANALYSIS_RATE: return False  # Too little to tell.
    a, b, step = tracks[i], tracks[j], int(ANALYSIS_RATE*CONTAIN_WINDOW)
    seconds = [(_dot(a[s:s+step], a[s:s+step]), _dot(b[s:s+step], b[s:s+step]), _dot(a[s:s+step], b[s:s+step]),
                len(a[s:s+step])) for s in range(0, len(a), step)]
    # By j's own loudness: sorting on i's would pick seconds by the j-i cross term and bias both halves.
    seconds.sort(key=lambda t: t[1]/t[3])
    half = len(seconds)//2
    for pool in (seconds, seconds[:half], seconds[half:]):
        ea, eb, x, n = (math.fsum(t[k] for t in pool) for k in range(4))
        whole = pool is seconds
        if not whole and (not n or eb < SILENT_RMS**2*n): continue  # j is silent here: nothing of it to lose.
        share = x/math.sqrt(ea*eb) if ea and eb else 0.
        if whole and share < CONTAIN_SHARE: return False
        if share >= (CONTAIN_SHARE if whole else CONTAIN_POOL_SHARE):
            low, high = CONTAIN_GAIN if whole else CONTAIN_POOL_GAIN
            if not low <= x/eb <= high: return False
        elif x/eb < CONTAIN_POOL_MIN: return False  # j sounds here but most of it is not in i.
    return True


def plan_by_sound(path, audio, ffmpeg='ffmpeg', cancel=None):
    """Decide from the waveforms: drop tracks that copy (or invert) an earlier one; if one remaining track
    is a least-squares mix of all the other sounding tracks, each audibly in it and nothing of theirs
    missing, play it alone; else drop tracks whose sound another track already holds at the same level
    (a mix next to the mic it contains) and mix the rest."""
    tracks = [_centered(t) for t in track_samples(path, audio, ffmpeg, cancel)]
    energy = [_dot(t, t) for t in tracks]
    sounding = [e/len(t) >= SILENT_RMS**2 for t, e in zip(tracks, energy)]
    def corr(i, j): return _dot(tracks[i], tracks[j])/math.sqrt(energy[i]*energy[j])
    def copies(i, k): return sounding[k] and not DUPLICATE_CORR > corr(i, k) > INVERTED_CORR
    kept = []
    for i in range(len(audio)):
        if sounding[i] and any(copies(i, k) for k in kept): continue
        kept.append(i)
    active = [i for i in kept if sounding[i]]
    if len(active) >= 3:
        for i in active:
            others = [j for j in active if j != i]
            gram = [[_dot(tracks[j], tracks[k]) for k in others] for j in others]
            cross = [_dot(tracks[j], tracks[i]) for j in others]
            gains = _solve(gram, cross)
            if gains is None or min(gains) < PREMIX_GAIN: continue
            residual = energy[i] - sum(g*c for g, c in zip(gains, cross))
            quietest = min(g*g*energy[j] for g, j in zip(gains, others))
            if residual <= (1-PREMIX_FIT)*energy[i] and residual <= PREMIX_RESIDUAL*quietest:
                # Tracks too quiet to compare were never tested against the premix: they stay.
                return [audio[k]['index'] for k in kept if k == i or not sounding[k]]
    for j in list(active):  # In order, against what is still kept, so of two equal tracks one stays.
        if any(i != j and i in kept and _inside(tracks, energy, j, i) for i in active): kept.remove(j)
    return [audio[i]['index'] for i in kept]


def mix_filter(plan, post=None, label='aout'):
    """filter_complex text that sums the plan's streams (input 0) into [label] without lowering any of
    them; a limiter catches the peaks two loud tracks can make. `post` appends filters to the mix."""
    inputs = ''.join(f'[0:{i}]aresample=48000,aformat=channel_layouts=stereo[m{n}];' for n, i in enumerate(plan))
    return (inputs + ''.join(f'[m{n}]' for n in range(len(plan))) +
            f'amix=inputs={len(plan)}:normalize=0:duration=longest,alimiter=limit=0.95:level=0:latency=1' +
            (f',{post}' if post else '') + f'[{label}]')


def audio_args(plan):
    """ffmpeg output args that put the plan's sound on the output (input 0); [] when there is none."""
    if not plan: return []
    if len(plan) == 1: return ['-map', f'0:{plan[0]}']
    return ['-filter_complex', mix_filter(plan), '-map', '[aout]']


def is_silent(path, ffmpeg='ffmpeg'):
    """True when a file's audio is digital silence (every sample of every channel within a hair of zero)."""
    raw = command([ffmpeg, '-v', 'error', '-i', path, '-vn', '-ar', '8000', '-f', 's16le', '-'], 300)
    pcm = array('h'); pcm.frombytes(raw[:len(raw)//2*2])
    return max(map(abs, pcm), default=0) < 4


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


WINDOW_SIZE = (1280, 480)   # Composite: zoomed center 854x480 | panel 426x240 over full frame 426x240.


def window_filter(panel, fps):
    """Three views of the same moment side by side, so one upload carries aim detail and context."""
    x, y, w, h = (panel[k] for k in ('x', 'y', 'w', 'h'))
    return (f'[0:v]split=3[a][b][c];'
            f'[a]crop=trunc(iw*0.4/2)*2:trunc(ih*0.4/2)*2:trunc(iw*0.3/2)*2:trunc(ih*0.3/2)*2,scale=854:480,setsar=1[center];'
            f'[b]crop=trunc(iw*{w}/2)*2:trunc(ih*{h}/2)*2:trunc(iw*{x}/2)*2:trunc(ih*{y}/2)*2,scale=426:240,setsar=1[panel];'
            f'[c]scale=426:240,setsar=1[full];[panel][full]vstack[right];[center][right]hstack,fps={fps}[out]')


def window(path, start, end, panel, directory, ffmpeg='ffmpeg', fps=12, cancel=None):
    """A short composite clip of [start, end] from the original recording, for the skill close-up.
    Times inside it start at 0; the caller adds `start` back."""
    from .store import identity
    target = Path(directory)/(identity([str(path), start, end, panel, fps])[:16]+'.mp4')
    _, plan = audio_plan(path, ffmpeg)
    graph, sound = window_filter(panel, fps), ['-map', f'0:{plan[0]}'] if len(plan) == 1 else []
    if len(plan) > 1: graph, sound = graph+';'+mix_filter(plan), ['-map', '[aout]']
    command([ffmpeg, '-v', 'error', '-y', '-ss', f'{start:.3f}', '-t', f'{end-start:.3f}', '-i', path,
             '-filter_complex', graph, '-map', '[out]', *sound,
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-b:a', '96k', '-ac', '2', target], timeout=600, cancel=cancel)
    return target


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
