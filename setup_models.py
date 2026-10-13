"""Explicit one-time downloads. Normal analysis never downloads models."""
import argparse
import json
import shutil
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--whisper-model',default='small'); p.add_argument('--yamnet',action='store_true')
    p.add_argument('--model-dir',default='models'); args=p.parse_args()
    from huggingface_hub import snapshot_download
    whisper=snapshot_download('Systran/faster-whisper-'+args.whisper_model)
    config={'whisper_model':whisper}
    if args.yamnet:
        import tensorflow_hub as hub
        cached=hub.resolve('https://tfhub.dev/google/yamnet/1')
        target=Path(args.model_dir).resolve()/'yamnet'
        if not target.exists(): shutil.copytree(cached,target)
        config['yamnet_path']=str(target)
    Path(args.model_dir).mkdir(parents=True,exist_ok=True)
    (Path(args.model_dir)/'local-models.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
    print(json.dumps(config,indent=2))
    print('Install vision model separately: ollama pull qwen3-vl:8b')


if __name__=='__main__': main()
