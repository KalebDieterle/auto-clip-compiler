"""Re-render legacy/current CSV without any model; always publish a new directory."""
import argparse
import csv
from pathlib import Path
from clipcompiler.media import probe, recording_time
from clipcompiler.render import publish
from clipcompiler.store import file_hash


def parse_ts(value):
    h, m, s = value.split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--timestamp-overlay', action='store_true')
    p.add_argument('--resolve', action='store_true')
    p.add_argument('--render-height', type=int, default=1080)
    p.add_argument('--fps', type=int, default=60)
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--ffprobe', default='ffprobe')
    p.add_argument('--audio-track', type=int)
    p.add_argument('--combine', action='store_true')
    a = p.parse_args()
    items, sources = [], {}
    with Path(a.manifest).open(encoding='utf-8-sig', newline='') as stream:
        for i, r in enumerate(csv.DictReader(stream)):
            source = Path(r.get('copied_path') or r['source_path']).resolve()
            if source not in sources:
                meta = probe(source, a.ffprobe, a.audio_track)
                if meta['audio_warning']:
                    raise ValueError(meta['audio_warning'])
                sources[source] = (meta, file_hash(source))
            meta, digest = sources[source]
            if r.get('source_hash') and r['source_hash'] != digest:
                raise ValueError(f'Changed source: {source}')
            items.append(dict(id=str(i), source=str(source), source_path=str(source), source_hash=digest,
                start=float(r['in_seconds']) if r.get('in_seconds') else parse_ts(r['in_timestamp']),
                end=float(r['out_seconds']) if r.get('out_seconds') else parse_ts(r['out_timestamp']),
                duration=meta['duration'], audio_stream=int(r['audio_stream']) if r.get('audio_stream') else meta['audio_stream'],
                score=float(r.get('llm_score') or 0), game=r.get('game', 'unknown'), transcript=r.get('transcript', ''),
                recorded_at=recording_time(source)[0]))
    settings = vars(a)
    settings['render'] = True
    print(publish(items, Path(a.output), settings))


if __name__ == '__main__':
    main()
