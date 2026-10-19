"""Validated, atomic publishing; MP4, CSV and FCPXML share one cut list."""
import csv
import datetime
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import threading
import uuid
import xml.etree.ElementTree as ET

from .media import Cancelled, audio_plan, command, is_silent, media_preflight, mix_filter, probe, source_channels

log=logging.getLogger(__name__)
from .reel import length
from .store import atomic_json, file_hash


def timestamp(seconds):
    millis=round(seconds*1000)
    h,millis=divmod(millis,3600000); m,millis=divmod(millis,60000); s,ms=divmod(millis,1000)
    return f'{h:02}:{m:02}:{s:02}.{ms:03}'


def snap(item,fps):
    """A cut widened to whole frames, kept inside its source."""
    c=dict(item)
    c['start']=math.floor(c['start']*fps+1e-6)/fps
    c['end']=min(math.ceil(c['end']*fps-1e-6),math.floor(c['duration']*fps))/fps
    if not 0<=c['start']<c['end']<=c['duration']:
        raise ValueError(f'Invalid render cut: {c}')
    return c


def slices_of(items):
    """Every source slice in play order: each moment's setup slices, then the moment."""
    return [s for c in items for s in (c.get('lead') or [])+[c]]


def flat(items):
    """One row per source slice for the CSV and FCPXML; setup slices carry their moment's id and score."""
    out=[]
    for c in items:
        for s in c.get('lead') or []:
            out.append(dict(c,**{k:v for k,v in s.items() if k not in ('evidence','id')},recommendation='setup',lead=None))
        out.append(dict(c,lead=None))
    return out


def follow(p,q,fps):
    """Snapped slice q plays right after p. Only when q starts inside what p showed (the outward rounding
    at a join), move q's start to p's last shown instant; earlier or later footage is left alone."""
    if p['source']==q['source']:
        if p['start']<=q['start']<p['end']: q['start']=p['end']
    elif p.get('clip_wall') is not None and q.get('clip_wall') is not None:
        shown=(p['start']+p['clip_wall'],p['end']+p['clip_wall']); at=q['start']+q['clip_wall']
        if shown[0]<=at<shown[1]: q['start']=math.ceil((shown[1]-q['clip_wall'])*fps-1e-6)/fps


def reconcile(shown,chain,fps):
    """The slices of `chain` (a setup, then its moment) checked in play order against every slice already
    shown, so none starts on an instant shown before; a setup slice wholly inside one is dropped."""
    kept=[]
    for i,q in enumerate(chain):
        for p in shown: follow(p,q,fps)
        if q['end']>q['start'] or i==len(chain)-1:
            kept.append(q); shown=shown+[q]
    return kept


def frame_cuts(items,fps):
    result=[]
    for item in items:
        c=snap(item,fps)
        if c.get('lead'):
            # A setup and its moment: snapped slices must not show the same instant twice where they meet.
            c['lead']=reconcile([],[snap(s,fps) for s in c['lead']]+[c],fps)[:-1]
            if c['end']<=c['start']: raise ValueError(f'Invalid render cut: {c}')
        result.append(c)
    # Rounding outward can duplicate a frame at shared joins. Use the prior end.
    for previous,current in zip(result,result[1:]):
        if current.get('lead'):
            # The setup plays first: check it, and everything after it, against all the previous moment showed.
            current['lead']=reconcile((previous.get('lead') or [])+[previous],current['lead']+[current],fps)[:-1]
        elif previous['source']==current['source'] and previous['end']>current['start']:
            current['start']=previous['end']
        elif joined(previous,current):
            # A different recording continues the event: start on its first frame at or after
            # the prior slice's last shown instant, so no moment plays twice (gap < 1 frame).
            shown=previous['end']+previous['timeline_offset']
            current['start']=max(current['start'],math.ceil((shown-current['timeline_offset'])*fps-1e-6)/fps)
    result=[c for c in result if c['end']>c['start']]
    for c in result:
        if 'timeline_offset' in c:
            c['timeline_start']=c['start']+c['timeline_offset']; c['timeline_end']=c['end']+c['timeline_offset']
    return result


def joined(previous, current):
    """Consecutive slices of one shared-timeline event that meet or overlap after rounding."""
    if 'timeline_offset' not in previous or 'timeline_offset' not in current:
        return False
    if previous.get('event_group')!=current.get('event_group'):
        return False
    return previous['end']+previous['timeline_offset']>current['start']+current['timeline_offset']-1e-6


def manifest(path, items):
    fields=['rank','filename','game','classification_confidence','funny_score','llm_score','transcript',
            'in_timestamp','out_timestamp','clip_duration','source_path','copied_path','candidate_id',
            'recommendation','reason','source_hash','audio_stream','in_seconds','out_seconds']
    with Path(path).open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for i,c in enumerate(items,1):
            writer.writerow(dict(rank=i,filename=Path(c['source_path']).name,game=c.get('game','unknown'),
                classification_confidence='',funny_score=c.get('laughter_score',0),llm_score=c.get('score',''),
                transcript=c.get('transcript',''),in_timestamp=timestamp(c['start']),out_timestamp=timestamp(c['end']),
                clip_duration=c['duration'],source_path=c['source_path'],copied_path='',candidate_id=c['id'],
                recommendation=c.get('recommendation','keep'),reason=c.get('reason',''),
                source_hash=c.get('source_hash',''),audio_stream='+'.join(map(str,c.get('audio_streams') or [])),
                in_seconds=repr(c['start']),out_seconds=repr(c['end'])))


def frame_duration(fps):
    """FCPXML rational frame duration; NTSC rates use the exact 1001 base."""
    if not fps or fps<=0: return None
    for nominal in (24,30,60,120):
        if abs(fps-nominal*1000/1001)<.01: return f'1001/{nominal*1000}s'
    return f'1/{round(fps)}s' if abs(fps-round(fps))<.01 else f'1000/{round(fps*1000)}s'


def fcpxml(path, items, fps, height):
    width=round(height*16/9/2)*2
    def t(s): return f'{round(s*fps)}/{fps}s'
    root=ET.Element('fcpxml',version='1.8'); resources=ET.SubElement(root,'resources')
    ET.SubElement(resources,'format',id='r1',name=f'FFVideoFormat{fps}p',frameDuration=f'1/{fps}s',width=str(width),height=str(height))
    assets={}; formats={}
    for c in items:
        key=c['source_path']
        if key in assets: continue
        aid='a'+str(len(assets)+1); assets[key]=aid
        # Describe the source as recorded; Resolve conforms it to the r1 timeline.
        shape=(c.get('source_width'),c.get('source_height'),frame_duration(c.get('source_fps')))
        if None in shape: fid='r1'
        else:
            if shape not in formats:
                formats[shape]='f'+str(len(formats)+1)
                ET.SubElement(resources,'format',id=formats[shape],frameDuration=shape[2],width=str(shape[0]),height=str(shape[1]))
            fid=formats[shape]
        attrs=dict(id=aid,name=Path(key).stem,src=Path(key).resolve().as_uri(),start='0s',duration=t(c['duration']),
                   hasVideo='1',hasAudio='1' if c.get('audio_streams') else '0',format=fid)
        if c.get('audio_streams'):
            # The asset is the whole file (every track); the clip's channel source picks the plan's tracks.
            attrs.update(audioSources=str(c.get('audio_sources') or 1),
                         audioChannels=str(c.get('audio_total_channels') or c.get('audio_channels') or 2))
        ET.SubElement(resources,'asset',attrs)
    lib=ET.SubElement(root,'library'); event=ET.SubElement(lib,'event',name='Auto Clip Compiler')
    project=ET.SubElement(event,'project',name='Combined')
    sequence=ET.SubElement(project,'sequence',format='r1',duration=t(sum(c['end']-c['start'] for c in items)),
                           tcStart='0s',tcFormat='NDF',audioLayout='stereo',audioRate='48k')
    spine=ET.SubElement(sequence,'spine'); offset=0
    for c in items:
        node=ET.SubElement(spine,'asset-clip',ref=assets[c['source_path']],name=Path(c['source_path']).stem,
            offset=t(offset),start=t(c['start']),duration=t(c['end']-c['start']),format='r1',tcFormat='NDF')
        if c.get('audio_src_channels'):
            ET.SubElement(node,'audio-channel-source',srcCh=c['audio_src_channels'],role='dialogue')
        offset+=c['end']-c['start']
    ET.indent(root)
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)


def verify_output(path, expected, ffprobe, fps):
    meta=probe(path,ffprobe)
    if not path.is_file() or path.stat().st_size==0 or meta['audio_count']!=1:
        raise RuntimeError(f'Missing/invalid rendered streams: {path}')
    if abs(meta['duration']-expected)>max(.15,2/fps):
        raise RuntimeError(f'Render duration mismatch: expected {expected:.3f}, got {meta["duration"]:.3f}')
    return meta


def publish(items, root, settings, partial=False, omissions=(), on_progress=None, cancel=None):
    """Render to a new unique folder. on_progress(done, total) fires per encoded segment; setting the
    `cancel` Event kills ffmpeg and raises media.Cancelled, leaving only a `.pending-*` folder marked failed."""
    if not items: raise ValueError('No selected moments to render')
    root=Path(root); media_preflight(root,settings['ffmpeg'],settings['ffprobe'],settings.get('min_free_gb',2))
    fps,height=settings['fps'],settings['render_height']
    items=frame_cuts(items,fps)
    for c in items:
        source=Path(c['source_path'])
        if not source.is_file(): raise FileNotFoundError(source)
    # Validate each unique content identity immediately before rendering saved decisions.
    hasher=settings.get('hasher') or file_hash
    checked=set()
    for c in slices_of(items):
        if not Path(c['source_path']).is_file(): raise FileNotFoundError(c['source_path'])
        if c['source_path'] in checked: continue
        if c.get('source_hash') and hasher(Path(c['source_path'])) != c['source_hash']:
            raise ValueError(f'Source changed since analysis: {c["source_path"]}')
        checked.add(c['source_path'])
    # The audio plan is decided now, from the file, so compilations saved by older versions (which
    # muted or dropped tracks of multi-track clips) render with every track without re-judging.
    sources=sorted(checked)
    def plan_for(source):
        if cancel is not None and cancel.is_set(): raise Cancelled()
        return audio_plan(source,settings['ffmpeg'],settings['ffprobe'],cancel=cancel)
    pool=ThreadPoolExecutor(max_workers=settings.get('workers',2))
    try:
        plans=dict(zip(sources,pool.map(plan_for,sources)))
    finally:
        pool.shutdown(cancel_futures=True)
    for c in slices_of(items):
        data,plan=plans[c['source_path']]
        # A caller may pin tracks with audio_streams; otherwise every audio field follows the fresh plan,
        # never values saved alongside an older plan.
        if 'audio_streams' not in c:
            c['audio_streams']=plan
            for key in ('audio_src_channels','audio_sources','audio_total_channels'): c.pop(key,None)
        c['audio_stream']=c['audio_streams'][0] if len(c['audio_streams'])==1 else None
        c.setdefault('audio_sources',len(data['audio_streams']))
        c.setdefault('audio_total_channels',sum(s.get('channels',2) for s in data['audio_streams']))
        if len(data['audio_streams'])>1: c.setdefault('audio_src_channels',source_channels(data,c['audio_streams']))
    silent=[]
    token=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    final=root/('partial-' if partial else 'render-')/token
    final.parent.mkdir(parents=True,exist_ok=True)
    staging=final.parent/('.pending-'+token); staging.mkdir()
    work=staging/'segments'; work.mkdir()
    atomic_json(staging/'render.json',{'status':'rendering','partial':partial,'omissions':list(omissions)})
    width=round(height*16/9/2)*2
    def encode(pair):
        i,c=pair; dest=work/f'{i:06}.mp4'; duration=c['end']-c['start']
        if c.get('lead'):
            joined_run(c,dest); plan=None
            bad=hushed(c,dest)
            if bad:
                # Same rescue as a single cut, slice by slice: a silent multi-track slice tries each of its
                # tracks alone while every other slice keeps what already sounds.
                slices=c['lead']+[c]; picks={}; made={}
                for k in bad:
                    for t in range(len(slices[k]['audio_streams']) if len(slices[k]['audio_streams'])>1 else 0):
                        picks[k]=t; joined_run(c,dest,picks); made=dict(picks)
                        if k not in hushed(c,dest): break
                    else: picks.pop(k,None)
                if made!=picks: joined_run(c,dest,picks)
                if picks: log.warning('%s: setup + moment mix came out silent; used tracks %s alone',c['source_path'],picks)
                if hushed(c,dest):
                    log.warning('%s: segment %d (with its setup) came out silent',c['source_path'],i)
                    with progress_lock: silent.append(c['id'])
        else:
            plan=c['audio_streams']; run(c,plan,dest,duration)
        if plan and is_silent(dest,settings['ffmpeg']):
            # A mix should never cancel out, but if it does, a single sounding track beats silence.
            for track in plan if len(plan)>1 else []:
                run(c,[track],dest,duration)
                if not is_silent(dest,settings['ffmpeg']):
                    log.warning('%s: mix of %s came out silent; used track %s alone',c['source_path'],plan,track); break
            else:
                log.warning('%s: segment %d came out silent although the source has audio %s',c['source_path'],i,plan)
                with progress_lock: silent.append(c['id'])
        if on_progress:
            with progress_lock:
                done[0]+=1; on_progress(done[0],len(items))
        return dest
    def run(c,plan,dest,duration):
        # Optional per-segment loudness normalization, so a long reel holds one volume.
        post='loudnorm=I=-16:TP=-1.5:LRA=11,apad' if settings.get('loudnorm') else 'apad'
        args=[settings['ffmpeg'],'-v','error','-y','-ss',c['start'],'-i',c['source_path']]
        if not plan:
            args += ['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']
        args += ['-t',duration,'-map','0:v:0']
        if len(plan)>1: args += ['-filter_complex',mix_filter(plan,post),'-map','[aout]']
        else: args += ['-map',f'0:{plan[0]}' if plan else '1:a:0','-af',post]
        args += ['-vf',picture(c),'-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p',
                 '-c:a','aac','-ar','48000','-ac','2',
                 '-movflags','+faststart',dest]
        command(args,cancel=cancel); verify_output(dest,duration,settings['ffprobe'],fps)
    def picture(c):
        vf=f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}'
        if settings.get('timestamp_overlay'):
            label=datetime.datetime.fromtimestamp(c.get('recorded_at',Path(c['source_path']).stat().st_mtime)).strftime('%Y-%m-%d %I.%M %p')
            font='C\\\\:/Windows/Fonts/arial.ttf'
            vf+=f",drawtext=fontfile='{font}':text='{label}':x=20:y=h-th-20:fontsize={max(16,height//40)}:fontcolor=white:box=1:boxcolor=black@0.6"
        return vf
    def hushed(c,dest):
        # Slices with sound in their source that came out silent (an audible setup must not hide a silent payoff).
        at=0.; out=[]
        for k,s in enumerate(c['lead']+[c]):
            d=s['end']-s['start']; edge=min(.1,d/4)   # AAC smears a few ms of a neighbour across each join.
            if s['audio_streams'] and is_silent(dest,settings['ffmpeg'],at+edge,d-2*edge): out.append(k)
            at+=d
        return out
    def joined_run(c,dest,picks=None):
        # A moment with its setup: every slice is decoded and joined first, then gets ONE loudness pass,
        # so the volume cannot jump where the setup meets the moment. picks: {slice: track position} to use alone.
        slices=c['lead']+[c]; total=length(c); args=[settings['ffmpeg'],'-v','error','-y']; graph=[]
        for k,s in enumerate(slices):
            d=s['end']-s['start']; plan=s['audio_streams']; fit=f'apad,atrim=duration={d:.6f}'
            if picks and k in picks: plan=[plan[picks[k]]]
            args+=['-ss',s['start'],'-t',d,'-i',s['source_path']]
            graph.append(f'[{k}:v:0]{picture(s)}[v{k}]')   # Each slice shows its own recording's time.
            if not plan: graph.append(f'anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={d:.6f}[a{k}]')
            elif len(plan)==1: graph.append(f'[{k}:{plan[0]}]aresample=48000,aformat=channel_layouts=stereo,{fit}[a{k}]')
            else: graph.append(mix_filter(plan,fit,label=f'a{k}',src=k))
        post='loudnorm=I=-16:TP=-1.5:LRA=11,apad' if settings.get('loudnorm') else 'apad'
        graph.append(''.join(f'[v{k}][a{k}]' for k in range(len(slices)))+f'concat=n={len(slices)}:v=1:a=1[vout][ac]')
        graph.append(f'[ac]{post}[aout]')
        args+=['-filter_complex',';'.join(graph),'-map','[vout]','-map','[aout]','-t',total,
               '-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p',
               '-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',dest]
        command(args,cancel=cancel); verify_output(dest,total,settings['ffprobe'],fps)
    progress_lock=threading.Lock(); done=[0]
    try:
        if settings.get('render',True):
            executor=ThreadPoolExecutor(max_workers=settings.get('workers',2))
            try:
                segments=list(executor.map(encode,enumerate(items)))
            finally:
                executor.shutdown(cancel_futures=True)  # One failure must not encode the whole queue first.
            listing=work/'concat.txt'
            # Relative numeric paths avoid drive/apostrophe escaping entirely.
            listing.write_text(''.join(f"file '{p.name}'\n" for p in segments),encoding='utf-8')
            destination=staging/('Partial_highlights.mp4' if partial else 'Combined_highlights.mp4')
            command([settings['ffmpeg'],'-v','error','-y','-f','concat','-safe','1','-i',listing,
                     '-c','copy','-movflags','+faststart',destination],cancel=cancel)
            verify_output(destination,sum(length(c) for c in items),settings['ffprobe'],fps)
        manifest(staging/'manifest.csv',flat(items))
        if settings.get('resolve'): fcpxml(staging/'Combined.fcpxml',flat(items),fps,height)
        atomic_json(staging/'selections.json',items)
        atomic_json(staging/'render.json',{'status':'partial' if partial else 'complete','partial':partial,
                                         'omissions':list(omissions),'selected':len(items),'silent_segments':silent})
        shutil.rmtree(work)
        os.rename(staging,final)  # Same-volume atomic directory publication; unique target.
        return final
    except BaseException as exc:
        atomic_json(staging/'render.json',{'status':'failed','error':str(exc) or type(exc).__name__,'partial':partial})
        shutil.rmtree(work,ignore_errors=True)  # Encoded segments can be gigabytes; never strand them.
        raise
