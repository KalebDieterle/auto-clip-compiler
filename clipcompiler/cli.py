import argparse
import logging
import sys
from pathlib import Path
from .store import Store, unique_dir
from .judge import DEFAULT_TIMEOUT


def parser():
    p=argparse.ArgumentParser(description='Fully local, resumable compilations of moments with friends.')
    mode=p.add_mutually_exclusive_group()
    for name in ('resume','review','render-from'): mode.add_argument('--'+name)
    for name in ('source','source-list','output','judge-model','ollama-url','whisper-model','yamnet-path','ffmpeg','ffprobe'):
        p.add_argument('--'+name)
    for name in ('analyze-only','allow-partial','cpu','chronological','ranked','render','resolve','combine','timestamp-overlay'):
        p.add_argument('--'+name,action='store_true',default=None)
    for name in ('workers','audio-track','frame-batch','limit','render-height','fps'):
        p.add_argument('--'+name,type=int)
    for name in ('short-seconds','llm-timeout','min-score','min-dur','max-dur','min-free-gb'):
        p.add_argument('--'+name,type=float)
    p.add_argument('--model-concurrency',type=int,choices=[1],help='Inference is serialized.')
    p.add_argument('--judge-mode',choices=['visual','text'])
    p.add_argument('--include',action='append',help='Additional source path; repeat as needed.')
    p.add_argument('--preflight',nargs='?',const=5,type=int,help='Analyze K clips (default 5) without rendering.')
    p.add_argument('-n','--top',type=int,help='Explicit candidate limit; default unlimited.')
    p.add_argument('--port',type=int,default=8765)
    for name in ('all-games','every-clip','smart-rank'):
        p.add_argument('--'+name,action='store_true',help='Compatibility alias: every source now gets full-context judging.')
    return p


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv); p=parser()
    legacy={'--llm-command','--llm-base-url','--llm-api-key','--llm-model','--smart-pool','--smart-batch',
            '--frames','--silence-db','--silence-dur','--conf-threshold','--other-floor','--laugh-threshold',
            '--energy-percentile','--wide-laughter','--redetect','--tesseract-path'}
    rejected=[a.split('=')[0] for a in argv if a.split('=')[0] in legacy]
    if rejected: p.error('Retired arguments: '+', '.join(rejected)+'. Use local Ollama --judge-model qwen3-vl:8b-instruct; '
                        'command/hosted judges and laughter-gated selection are removed. See README migration.')
    args=p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.review:
            if not (Path(args.review)/'run.sqlite').is_file(): raise ValueError('Review requires an existing run directory')
            from .review import serve
            serve(args.review,args.port); return 0
        existing=args.resume or args.render_from
        provided={k:v for k,v in vars(args).items() if v is not None and k not in
                  ('resume','review','render_from','port','all_games','every_clip','smart_rank','preflight')}
        if existing:
            directory=Path(existing).resolve()
            if not (directory/'run.sqlite').is_file(): raise ValueError('Existing run directory required')
            store=Store(directory)
            try: settings=store.get('settings')
            finally: store.close()
            if any(k in provided for k in ('source','source_list','output','include','limit')):
                raise ValueError('Resume/render uses its saved source inventory/output; start a new run to change them')
            if args.render_from:
                forbidden=set(provided)-{'allow_partial','render','resolve','fps','render_height','workers','ffmpeg','ffprobe',
                                        'timestamp_overlay','top','min_score','ranked','chronological','min_free_gb'}
                if forbidden: raise ValueError('Render-only cannot change analysis settings: '+', '.join(sorted(forbidden)))
                from .pipeline import render_saved
                provided.update(render=True,allow_partial=bool(args.allow_partial))
                print(render_saved(directory,provided)); return 0
            settings.update(provided); settings['allow_partial']=bool(args.allow_partial)
        else:
            settings=dict(source='clips',output='highlights',judge_model='qwen3-vl:8b-instruct',judge_mode='visual',
                ollama_url='http://127.0.0.1:11434',cpu=False,whisper_model='small',yamnet_path=None,
                audio_track=None,llm_timeout=DEFAULT_TIMEOUT,frame_batch=12,model_concurrency=1,workers=2,
                short_seconds=60,limit=None,include=[],top=None,min_score=None,min_dur=0.,max_dur=None,
                chronological=True,ranked=False,render=True,resolve=False,timestamp_overlay=False,
                render_height=1080,fps=60,ffmpeg='ffmpeg',ffprobe='ffprobe',min_free_gb=2,
                analyze_only=False,allow_partial=False)
            settings.update(provided)
            import json
            local_models=Path(__file__).resolve().parent.parent/'models'/'local-models.json'
            if local_models.is_file():
                config=json.loads(local_models.read_text(encoding='utf-8'))
                for key in ('whisper_model','yamnet_path'):
                    if key not in provided and key in config: settings[key]=config[key]
            source=Path(settings['source']).resolve(); output=Path(settings['output']).resolve()
            if output==source or source in output.parents:
                raise ValueError('Output must be outside the source tree, so outputs cannot become inputs')
            settings.update(source=str(source),output=str(output))
            directory=None; settings['artifact_root']=str(output/'.cache')
        for key in ('workers','frame_batch','fps','render_height','llm_timeout','short_seconds'):
            if settings[key]<=0: raise ValueError(f'{key} must be positive')
        if settings['short_seconds']>120: raise ValueError('--short-seconds must be <=120')
        if settings['render_height']%2: raise ValueError('--render-height must be even')
        if settings['frame_batch']>12: raise ValueError('--frame-batch must be 1..12 for the 32K context budget')
        for key in ('top','limit','max_dur'):
            if settings.get(key) is not None and settings[key]<=0: raise ValueError(f'{key} must be positive')
        if settings['min_dur']<0 or (settings.get('max_dur') and settings['min_dur']>settings['max_dur']):
            raise ValueError('Invalid duration bounds')
        if settings.get('min_score') is not None and not 1<=settings['min_score']<=10: raise ValueError('--min-score must be 1..10')
        if args.chronological: settings['ranked']=False
        if args.preflight is not None:
            if args.preflight<=0: raise ValueError('--preflight must be positive')
            settings.update(limit=args.preflight,analyze_only=True)
        # Only a validated new run gets a directory, so bad arguments leave nothing behind.
        if directory is None: directory=unique_dir(settings['output'])
        if settings['judge_mode']=='text': logging.warning('Text baseline cannot evaluate unseen visual jokes; use only for comparison.')
        if not settings.get('yamnet_path'): logging.warning('YAMNet not configured; laughter evidence is explicitly unavailable. See setup_models.py.')
        print('Run directory: '+str(directory),flush=True)
        from .pipeline import analyze
        summary=analyze(directory,settings)
        print(f'{summary["status"]}: {summary["sources"]} sources, {len(summary["failed"])} failed, '
              f'{summary["keep"]} keep / {summary["maybe"]} maybe / {summary["reject"]} rejected')
        return 0 if summary['status']=='complete' else 2
    except KeyboardInterrupt:
        print('Interrupted. Completed stages are saved; use --resume <run-dir>.',file=sys.stderr); return 130
    except Exception as exc:
        logging.error('%s',exc); return 2
