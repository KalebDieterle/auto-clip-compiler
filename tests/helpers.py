"""Shared test fixtures: tiny synthetic clips made with ffmpeg (no real footage, no network)."""
from clipcompiler.media import command


def clip(path, seconds=4, offset=0, tracks=1, frequency=440):
    """Moving test pattern; distinct sines per track. `offset` trims the head like a later replay."""
    args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size=160x90:rate=15:duration={seconds+offset}']
    for t in range(tracks):
        args += ['-f', 'lavfi', '-i', f'sine=frequency={frequency*(t+1)}:sample_rate=48000:duration={seconds+offset}']
    args += ['-ss', offset, '-map', '0:v']
    for t in range(tracks):
        args += ['-map', f'{t+1}:a']
    command(args + ['-c:v', 'libx264', '-g', '15', '-c:a', 'aac', path])
    return path


def judgment(*moments, summary='test'):
    """A cached-judgment dict in the shape Gemini returns: moments are (start, end, score)."""
    return {'summary': summary, 'moments': [dict(start=s, end=e, score=score, setup='', payoff='', reaction='',
                                                  why_funny=f'bit {s}') for s, e, score in moments],
            '_meta': {'model': 'fake', 'input_tokens': 2850, 'output_tokens': 200, 'thinking_tokens': 450}}
