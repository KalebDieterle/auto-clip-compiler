import csv
import importlib.metadata
import json
import logging
from pathlib import Path
import time

from .store import Store, identity, file_hash, atomic_json, awake, run_lock
from .media import (VIDEO_EXTS, MEDIA_VERSION, AudioAnalyzer, probe, recording_time,
                    windows, frames, extract_audio, media_preflight)
from .judge import Ollama, PROMPT_VERSION
from .selection import boundaries, select
from .overlap import align_sources
from .render import manifest, frame_cuts, publish

log=logging.getLogger(__name__)


def discover(source, include=(), limit=None):
    source=Path(source)
    if not source.exists(): raise FileNotFoundError(source)
    found=[source] if source.is_file() else sorted(p for p in source.rglob('*') if p.suffix.lower() in VIDEO_EXTS and p.is_file())
    if limit: found=found[:limit]
    for path in include:
        path=Path(path)
        if not path.is_file(): raise FileNotFoundError(path)
        if path.suffix.lower() not in VIDEO_EXTS: raise ValueError(f'Unsupported video: {path}')
        found.append(path)
    return list(dict.fromkeys(str(p.resolve()) for p in found))


def versions():
    names=['faster-whisper','ctranslate2','tensorflow','tensorflow-hub','numpy','Pillow']
    result={}
    for name in names:
        try: result[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: result[name]=None
    return result


def report(store):
    sources=store.sources(); candidates=store.candidates()
    failed=[{'path':s['path'],'stage':'source','error':s['error']} for s in sources if s['status']!='complete']
    # Failed attempts from obsolete configurations remain in SQLite for audit; summary
    # reports current source outcomes only, so repaired retries can complete normally.
    summary={'status':'incomplete' if failed else 'complete','sources':len(sources),'failed':failed,
             'candidates':len(candidates),'keep':sum(c['recommendation']=='keep' for c in candidates),
             'maybe':sum(c['recommendation']=='maybe' for c in candidates),
             'reject':sum(c['recommendation']=='reject' for c in candidates)}
    atomic_json(store.directory/'summary.json',summary); store.set('summary',summary)
    fields=['source_path','source_status','source_error','id','start','end','score','confidence','recommendation',
            'reason','setup','event','payoff','reaction','boundary_uncertain','reviewed']
    with (store.directory/'candidate_report.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore'); writer.writeheader()
        for s in sources:
            items=[c for c in candidates if c['source']==s['id']] or [{}]
            for c in items:
                writer.writerow(dict(c,source_path=s['path'],source_status=s['status'],source_error=s['error']))
    return summary


def analyze(directory, settings):
    started=time.monotonic()
    directory=Path(directory)
    with run_lock(directory), awake():
        store=Store(directory)
        try:
            store.set('settings',settings); store.set('state','running'); store.set('run_error',None)
            paths=store.get('source_paths')
            if paths is None:
                if settings.get('source_list'):
                    paths=json.loads(Path(settings['source_list']).read_text(encoding='utf-8'))
                    if not isinstance(paths,list) or not all(isinstance(p,str) for p in paths):
                        raise ValueError('--source-list must contain a JSON array of paths')
                    paths=list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
                    if settings.get('limit'): paths=paths[:settings['limit']]
                else:
                    paths=discover(settings['source'],settings.get('include',[]),settings.get('limit'))
                if not paths: raise ValueError('No supported source videos discovered')
                store.set('source_paths',paths)
                for path in paths:
                    sid=identity(str(Path(path).resolve()).casefold())
                    store.source(sid,path,None,'pending',{})
            media_preflight(directory,settings['ffmpeg'],settings['ffprobe'],settings['min_free_gb'])
            client=Ollama(settings['judge_model'],settings['ollama_url'],settings['llm_timeout'],settings['cpu'],audit_dir=directory/'model-responses')
            model=store.stage('_run','model-preflight',identity([settings['judge_model'],time.time()]),client.preflight)
            store.set('model',model); store.set('versions',versions())
            audio=AudioAnalyzer(settings['whisper_model'],settings.get('yamnet_path'))
            # Fail before scanning the library when dependencies/models are missing.
            audio.load()
            artifact_root=Path(settings['artifact_root']); artifact_root.mkdir(parents=True,exist_ok=True)
            for i,path_string in enumerate(paths):
                path=Path(path_string); sid=identity(str(path.resolve()).casefold())
                log.info('[%d/%d] %s',i+1,len(paths),path.name)
                digest=None; meta={}; errors=[]
                try:
                    digest=file_hash(path)
                    meta=store.stage(sid,'probe',identity([digest,settings.get('audio_track'),MEDIA_VERSION]),
                                     lambda:probe(path,settings['ffprobe'],settings.get('audio_track')))
                    recorded,hint=recording_time(path); meta.update(recorded_at=recorded,timestamp_hint=hint)
                    store.source(sid,path,digest,'running',meta)
                    if meta['audio_warning']: errors.append(meta['audio_warning'])
                    source_artifacts=artifact_root/digest
                    observation_key=[digest,client.digest,PROMPT_VERSION,settings['judge_mode'],settings['frame_batch']]
                    all_speech=[]; window_results=[]
                    for wi,(start,end) in enumerate(windows(meta['duration'],settings['short_seconds'])):
                        wid=f'{sid}:{wi}'
                        media_key=identity([digest,start,end,meta['audio_stream'],MEDIA_VERSION])
                        speech={'status':'no_audio','segments':[]}; evidence={'energy':[],'laughter':[]}
                        if meta['audio_stream'] is not None:
                            try:
                                wav=extract_audio(path,start,end,meta['audio_stream'],source_artifacts/media_key/'audio.wav',settings['ffmpeg'])
                                speech=store.stage(sid,'transcription:'+str(wi),
                                    identity([media_key,audio.model_digest,versions().get('faster-whisper')]),
                                    lambda:audio.transcribe(wav,start))
                                evidence=store.stage(sid,'audio-evidence:'+str(wi),identity([media_key,audio.yamnet_digest,'audio-v1']),
                                                     lambda:audio.evidence(wav,start))
                            except Exception as exc:
                                speech={'status':'failed','segments':[],'error':str(exc)}
                                errors.append(f'Transcription/audio window {wi}: {exc}')
                        elif meta['audio_warning']:
                            speech={'status':'ambiguous_audio','segments':[]}
                        store.set('transcription:'+wid,speech)
                        all_speech.extend(speech['segments'])
                        observations=[]
                        def observe_batch(batch, name):
                            key=identity([observation_key,name,[f['time'] for f in batch],speech,evidence])
                            def infer():
                                try:
                                    return [client.observe(name,batch,speech,evidence)]
                                except Exception as exc:
                                    # Only resource exhaustion/context errors justify splitting;
                                    # malformed outputs follow the ordinary two-retry failure path.
                                    if len(batch)>1 and any(t in str(exc).lower() for t in ('memory','context','500','timeout','timed out')):
                                        middle=len(batch)//2
                                        return observe_batch(batch[:middle],name+'a')+observe_batch(batch[middle:],name+'b')
                                    raise
                            return store.stage(sid,'observation:'+name,key,infer)
                        if settings['judge_mode']=='visual':
                            ff=frames(path,start,end,1,source_artifacts/media_key/'frames-1fps',settings['ffmpeg'])
                            for fi in range(0,len(ff),settings['frame_batch']):
                                observations.extend(observe_batch(ff[fi:fi+settings['frame_batch']],f'{wid}:coarse:{fi}'))
                        judge_key=identity([observation_key,start,end,speech,observations,evidence])
                        result=store.stage(sid,'judgment:'+str(wi),judge_key,
                                           lambda:client.judge(wid,start,end,speech,observations,evidence))
                        refined=list(observations)
                        if settings['judge_mode']=='visual':
                            regions=[]
                            for c in result['moments']:
                                if c['recommendation'] in ('keep','maybe') or c['boundary_uncertain']:
                                    a=max(start,c['payoff_time']-5); b=min(end,a+10)
                                    if not any(a>=x and b<=y for x,y in regions): regions.append((a,b))
                            for ri,(a,b) in enumerate(regions):
                                ff=frames(path,a,b,4,source_artifacts/identity([a,b,4,MEDIA_VERSION])/'frames',settings['ffmpeg'])
                                for fi in range(0,len(ff),settings['frame_batch']):
                                    refined.extend(observe_batch(ff[fi:fi+settings['frame_batch']],f'{wid}:refine:{ri}:{fi}'))
                            if regions:
                                result=store.stage(sid,'refined-judgment:'+str(wi),identity([judge_key,refined]),
                                                   lambda:client.judge(wid,start,end,speech,refined,evidence))
                        window_results.append({'start':start,'end':end,'speech':speech,'observations':refined,'result':result})
                    # Reconcile each overlapping pair with its combined context before cuts.
                    # Single-window decisions remain as recall-preserving candidates.
                    combined=list(window_results)
                    for wi,(left,right) in enumerate(zip(window_results,window_results[1:])):
                        speech={'segments':left['speech']['segments']+right['speech']['segments']}
                        obs=left['observations']+right['observations']
                        joint_id=f'{sid}:join:{wi}'
                        result=store.stage(sid,'join:'+str(wi),identity([observation_key,speech,obs,'join-v1']),
                            lambda:client.judge(joint_id,left['start'],right['end'],speech,obs,{}))
                        combined.append(dict(start=left['start'],end=right['end'],result=result))
                    candidates=[]
                    for wi,window in enumerate(combined):
                        result=window['result']
                        if not result['moments']:
                            # An explicit rejection record makes whole-window rejection reviewable.
                            result=dict(result,moments=[dict(start=window['start'],end=window['end'],score=1,
                                payoff_time=window['start'],confidence='high',recommendation='reject',
                                reason=result['coverage'],setup='not observed',event='No qualifying event',
                                payoff='not observed',reaction='not observed',boundary_uncertain=False)])
                        for ci,c in enumerate(result['moments']):
                            c=dict(c,id=identity([sid,digest,round(c['start'],2),round(c['end'],2),c['event']]),source=sid,source_hash=digest,
                                window_start=window['start'],window_end=window['end'],coverage=result['coverage'],
                                transcript=' '.join(s['text'] for s in all_speech if s['end']>c['start'] and s['start']<c['end']))
                            candidates.append(boundaries(c,all_speech,meta['duration'],settings['short_seconds'],settings['min_dur'],settings.get('max_dur')))
                    # Do not bind decisions to a source that changed during extraction/inference.
                    if file_hash(path)!=digest: raise RuntimeError('Source changed during analysis')
                    store.replace_candidates(sid,list({c['id']:c for c in candidates}.values()))
                    store.source(sid,path,digest,'failed' if errors else 'complete',meta,'; '.join(errors) or None)
                except Exception as exc:
                    log.exception('Analysis failed: %s',path)
                    store.source(sid,path,digest,'failed',meta,str(exc))
            align_sources(store,artifact_root,settings['ffmpeg'])
            summary=report(store)
            summary.update(elapsed_seconds=time.monotonic()-started)
            try:
                import psutil
                memory=psutil.Process().memory_info()
                summary['peak_process_memory_bytes']=getattr(memory,'peak_wset',memory.rss)
            except ImportError:
                summary['peak_process_memory_bytes']=None
            summary['memory_scope']='Python process only; Ollama VRAM recorded in model preflight, total peak unmeasured'
            store.set('summary',summary); atomic_json(directory/'summary.json',summary)
            items=frame_cuts(select(store,settings.get('top'),settings.get('min_score'),settings.get('ranked',False)),settings['fps'])
            manifest(directory/'manifest.csv',items); atomic_json(directory/'selections.json',items)
            store.set('state',summary['status'])
        except BaseException as exc:
            store.set('state','incomplete'); store.set('run_error',str(exc))
            summary=report(store); summary.update(status='incomplete',run_error=str(exc))
            store.set('summary',summary); atomic_json(directory/'summary.json',summary)
            store.close()
            raise
        # Analysis is final here. A render failure is recorded separately so the saved
        # decisions stay renderable with --render-from instead of forcing re-analysis.
        try:
            if not settings['analyze_only'] and (summary['status']=='complete' or settings['allow_partial']) and items:
                output=publish(items,directory,settings,summary['status']!='complete',summary['failed'])
                store.set('last_render',str(output)); store.set('render_error',None); log.info('Published %s',output)
            return summary
        except BaseException as exc:
            store.set('render_error',str(exc))
            raise
        finally:
            store.close()


def render_saved(directory, settings_override=None):
    with run_lock(directory), awake():
        store=Store(directory)
        try:
            settings=store.get('settings'); settings.update(settings_override or {})
            summary=store.get('summary',{})
            if (summary.get('status')!='complete' or store.get('state')!='complete') and not settings.get('allow_partial'):
                raise ValueError('Run incomplete: resume analysis, or explicitly use --allow-partial; see summary.json')
            selections=select(store,settings.get('top'),settings.get('min_score'),settings.get('ranked',False))
            output=publish(selections,directory,settings,summary.get('status')!='complete',summary.get('failed',[]))
            store.set('last_render',str(output)); return output
        finally: store.close()
