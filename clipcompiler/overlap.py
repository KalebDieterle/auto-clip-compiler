"""Conservative content alignment. Timestamps only propose pairs."""
from collections import Counter
import json
from pathlib import Path


def fingerprints(frame_list):
    import numpy as np
    from PIL import Image
    result = []
    for frame in frame_list:
        with Image.open(frame['path']) as image:
            pixels = np.asarray(image.convert('L').resize((32,32)), dtype=np.float32)
            tiny = np.asarray(image.convert('L').resize((9,8)), dtype=np.float32)
        bits = (tiny[:,1:] > tiny[:,:-1]).flatten()
        value = sum(int(v) << i for i,v in enumerate(bits))
        result.append((frame['time'], value, pixels))
    return result


def visual_alignment(a, b):
    import numpy as np
    matches = []
    for ta, ha, pa in a:
        if pa.std() < 10:  # Black/loading/static low-information screens cannot prove a match.
            continue
        for tb, hb, pb in b:
            if (ha ^ hb).bit_count() <= 6 and np.mean(np.abs(pa-pb)) < 8:
                matches.append((round((ta-tb)*4)/4, ta, tb, ha))
    if not matches:
        return None
    offset, _ = Counter(m[0] for m in matches).most_common(1)[0]
    hits = [m for m in matches if abs(m[0]-offset) <= .1]
    # A still menu may match thousands of times. Require distinct moving scenes,
    # unique source timestamps, a sustained span and near-complete overlap coverage.
    if len({m[3] for m in hits}) < 5 or len({m[1] for m in hits}) < 5:
        return None
    span = max(m[1] for m in hits) - min(m[1] for m in hits)
    overlap = [x for x in a if b[0][0]+offset-.1 <= x[0] <= b[-1][0]+offset+.1]
    if span < 4 or len({m[1] for m in hits}) < max(5, len(overlap)*.90):
        return None
    return {'offset':offset,'matching_frames':len(hits),'distinct_frames':len({m[3] for m in hits}),
            'span':span, 'first_match':min(m[1] for m in hits),'last_match':max(m[1] for m in hits)}


def audio_agrees(a_path, b_path, offset):
    """Normalized waveform correlation over overlap; silence is not confirmation."""
    import numpy as np
    from scipy.signal import correlate
    from .media import read_audio
    a, b = read_audio(a_path)[::8], read_audio(b_path)[::8]
    rate = 2000
    a = a[max(0, round(offset*rate)):]
    b = b[max(0, round(-offset*rate)):]
    n = min(len(a),len(b),rate*30)
    if n < rate*4:
        return False
    a,b = a[:n],b[:n]
    if min(float(a.std()),float(b.std())) < .001:
        return False
    a,b = a-a.mean(),b-b.mean()
    correlation = correlate(a,b,mode='full',method='fft')
    center = n-1
    peak = correlation[center-400:center+401].max()
    return bool(peak / (np.linalg.norm(a)*np.linalg.norm(b)) > .97)


def align_sources(store, artifacts, ffmpeg):
    from .media import frames, extract_audio
    from .store import identity
    sources = [s for s in store.sources() if s['hash'] and s['data'].get('duration')]
    results = []
    for i,a in enumerate(sources):
        for b in sources[i+1:]:
            if a['hash'] == b['hash']:
                results.append((a['id'],b['id'],'confirmed',{'offset':0.,'method':'exact_hash'}))
                continue
            da,db = a['data'],b['data']
            if not (da.get('timestamp_hint') and db.get('timestamp_hint')):
                continue
            if abs(da['recorded_at']-db['recorded_at']) > max(da['duration'],db['duration'])+5:
                continue
            key = identity([a['hash'],b['hash'],'alignment-v2'])
            def compute():
                fa = frames(Path(a['path']),0,da['duration'],1,artifacts/a['hash']/'align',ffmpeg)
                fb = frames(Path(b['path']),0,db['duration'],1,artifacts/b['hash']/'align',ffmpeg)
                found = visual_alignment(fingerprints(fa),fingerprints(fb))
                if found is None:
                    return {'status':'unconfirmed','reason':'No sustained distinct-frame alignment'}
                found['method'] = 'frames_and_audio'
                if da.get('audio_stream') is None or db.get('audio_stream') is None:
                    return dict(found,status='possible',reason='Visual match, no unambiguous audio corroboration')
                # Only bounded buffers, even for long recordings.
                off = found['offset']
                begin_a,begin_b = max(0,off),max(0,-off)
                length = min(30, da['duration']-begin_a,db['duration']-begin_b)
                wa = extract_audio(Path(a['path']),begin_a,begin_a+length,da['audio_stream'],artifacts/key/'a.wav',ffmpeg)
                wb = extract_audio(Path(b['path']),begin_b,begin_b+length,db['audio_stream'],artifacts/key/'b.wav',ffmpeg)
                if not audio_agrees(wa,wb,0):
                    return dict(found,status='possible',reason='Visual match but audio did not confirm')
                return dict(found,status='confirmed')
            try:
                result = store.stage(a['id'],'alignment:'+b['id'],key,compute)
            except Exception as exc:
                result = {'status':'possible','reason':f'Alignment failed: {exc}'}
            results.append((a['id'],b['id'],result['status'],result))
    with store.db:
        store.db.execute('DELETE FROM alignments')
        store.db.executemany('INSERT INTO alignments VALUES (?,?,?,?)',
            [(a,b,s,json.dumps(d)) for a,b,s,d in results])
    return results


def timelines(sources, alignments):
    """Consistent weighted graph; contradictions disable the affected component."""
    adjacency = {s['id']:[] for s in sources}
    for a,b,status,data in alignments:
        if status == 'confirmed':
            adjacency[a].append((b,data['offset']))
            adjacency[b].append((a,-data['offset']))
    answer = {}
    for root in sorted(adjacency):
        if root in answer:
            continue
        offsets = {root:0.}
        todo = [root]
        inconsistent = False
        while todo:
            current = todo.pop()
            for neighbor,delta in adjacency[current]:
                expected = offsets[current]+delta
                if neighbor in offsets:
                    inconsistent |= abs(offsets[neighbor]-expected) > .25
                else:
                    offsets[neighbor]=expected; todo.append(neighbor)
        for sid, offset in offsets.items():
            answer[sid] = (sid,0.) if inconsistent else (root,offset)
    return answer
