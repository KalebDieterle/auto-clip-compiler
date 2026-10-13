"""Bounded live diagnostic against a cached observation (no media recomputation)."""
import argparse
import json
import logging
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clipcompiler.judge import Ollama
from clipcompiler.store import Store

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('run'); p.add_argument('--frames',type=int,default=6); p.add_argument('--timeout',type=float,default=90)
a=p.parse_args(); logging.basicConfig(level=logging.INFO)
store=Store(a.run)
settings=store.get('settings')
source=next(s for s in store.sources() if Path(s['path']).name=='gahgah.mp4')
speech=store.get('transcription:'+source['id']+':0')
frame_file=next((Path(settings['artifact_root'])/source['hash']).rglob('frames.json'))
frames=json.loads(frame_file.read_text())[:a.frames]
client=Ollama(timeout=a.timeout,audit_dir=Path(a.run)/'diagnostic-responses')
print(json.dumps(client.observe('probe',frames,speech,{}),indent=2))
store.close()
