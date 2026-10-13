import json
import math


def boundaries(item, segments, duration, short=60, min_dur=0, max_dur=None):
    item = dict(item)
    start,end = item['start'],item['end']
    if item['boundary_uncertain']:
        start,end = (0.,duration) if duration <= short else (item['window_start'],item['window_end'])
    else:
        # Expand to nearby speech edges; never trim setup based on pauses.
        for speech in segments:
            if speech['start'] <= start < speech['end'] or 0 <= start-speech['start'] <= 1.5:
                start = min(start,speech['start'])
            if speech['start'] < end <= speech['end'] or 0 <= speech['end']-end <= 1.5:
                end = max(end,speech['end'])
    if end-start < min_dur:
        pad = (min_dur-(end-start))/2
        start,end = max(0,start-pad),min(duration,end+pad)
    if max_dur and end-start > max_dur:
        item['duration_limited'] = True
        start = max(start,min(item['payoff_time']-max_dur*.65,end-max_dur))
        end = min(end,start+max_dur)
    item.update(start=max(0,start),end=min(duration,end))
    return item


def subtract(interval, covered):
    pieces = [interval]
    for a,b in covered:
        next_pieces=[]
        for start,end in pieces:
            if b <= start or a >= end:
                next_pieces.append((start,end))
            else:
                if start < a: next_pieces.append((start,a))
                if b < end: next_pieces.append((b,end))
        pieces = next_pieces
    return pieces


def shared_regions(alignments, timeline, sources):
    """Timeline spans each confirmed pair verifiably shares, keyed both ways."""
    regions={}
    for a,b,status,data in alignments:
        if status!='confirmed' or timeline[a][0]!=timeline[b][0]:
            continue  # Contradictory components were split apart; nothing is shared.
        if data.get('method')=='exact_hash':
            x,y=0.,sources[a]['data']['duration']
        elif data.get('first_match') is None or data.get('last_match') is None:
            continue  # No matched span recorded: keep both recordings' footage.
        else:
            # Frames are sampled at 1 fps; each match vouches for the second it starts.
            x,y=data['first_match'],data['last_match']+1
        offset=timeline[a][1]
        for key in ((a,b),(b,a)):
            regions.setdefault(key,[]).append((x+offset,y+offset))
    return regions


def channels(data):
    stream=next((s for s in data.get('audio_streams',[]) if s.get('index')==data.get('audio_stream')),None)
    return stream.get('channels',2) if stream else None


def source_channels(data):
    """1-based positions of the chosen track's channels among all of the file's audio channels."""
    position=0
    for s in data.get('audio_streams',[]):
        count=s.get('channels',2)
        if s.get('index')==data.get('audio_stream'):
            return ', '.join(str(position+i+1) for i in range(count))
        position+=count
    return None


def select(store, top=None, min_score=None, ranked=False):
    from .overlap import timelines
    sources = {s['id']:s for s in store.sources()}
    alignments=[(r['a'],r['b'],r['status'],json.loads(r['data'])) for r in store.db.execute('SELECT * FROM alignments')]
    timeline=timelines(list(sources.values()),alignments)
    # Reviewed decisions are already merged and take precedence over renamed candidates.
    items=store.candidates()
    items=[c for c in items if sources[c['source']]['status']=='complete'
           and c['recommendation'] in ('keep','maybe')
           and (min_score is None or c['score']>=min_score or c.get('reviewed'))]
    items.sort(key=lambda c:(-c['score'],c['id']))
    if top: items=items[:top]
    if not ranked:
        items.sort(key=lambda c:(sources[c['source']]['data']['recorded_at']+c['start'],c['id']))
    verified=shared_regions(alignments,timeline,sources)
    # Union intervals on shared timeline then choose source slices covering the union.
    # This preserves unique tails; dropping an entire overlapping candidate does not.
    groups={}
    for c in items:
        group,offset=timeline[c['source']]
        groups.setdefault(group,[]).append((c['start']+offset,c['end']+offset,c,offset))
    selected=[]
    for group,entries in groups.items():
        entries.sort(key=lambda e:(e[0],-e[1],e[2]['id']))
        covered=[]
        for start,end,c,offset in entries:
            # Another recording's footage only stands in for this one where frames were
            # actually matched; elsewhere in the overlap it is kept, never extrapolated.
            removable=[]
            for a,b,other in covered:
                if other==c['source']:
                    removable.append((a,b))
                else:
                    removable.extend((max(a,x),min(b,y)) for x,y in verified.get((c['source'],other),())
                                     if min(b,y)>max(a,x))
            for a,b in subtract((start,end),removable):
                if b-a < .001: continue
                data=sources[c['source']]['data']
                selected.append(dict(c,start=a-offset,end=b-offset,event_group=group,
                                     timeline_start=a,timeline_end=b,timeline_offset=offset,
                                     source_path=sources[c['source']]['path'],
                                     audio_stream=data.get('audio_stream'),audio_channels=channels(data),
                                     audio_src_channels=source_channels(data),
                                     audio_sources=len(data.get('audio_streams',[])),
                                     audio_total_channels=sum(s.get('channels',2) for s in data.get('audio_streams',[])),
                                     source_width=data.get('width'),source_height=data.get('height'),
                                     source_fps=data.get('fps'),
                                     duration=data['duration'],recorded_at=data['recorded_at']))
            covered.append((start,end,c['source']))
    # Order whole events, then play each event in timeline order: a duplicate recording's
    # slice must never jump ahead of the setup it continues.
    events={}
    for c in selected:
        key=-c['score'] if ranked else c['recorded_at']+c['start']
        events[c['event_group']]=min(events.get(c['event_group'],key),key)
    selected.sort(key=lambda c:(events[c['event_group']],c['event_group'],c['timeline_start']))
    # Fuse adjacent slices of the same recording for a plain continuous cut.
    fused=[]
    for item in selected:
        if fused and fused[-1]['source']==item['source'] and abs(fused[-1]['end']-item['start'])<.001:
            fused[-1]['end']=item['end']; fused[-1]['timeline_end']=item['timeline_end']
        else: fused.append(dict(item))
    return fused


def validate_override(item, data, duration):
    allowed={'recommendation','start','end'}
    if not data or set(data)-allowed:
        raise ValueError('Only recommendation/start/end can be edited')
    result=dict(item,**data)
    if result['recommendation'] not in ('keep','maybe','reject'):
        raise ValueError('Invalid recommendation')
    if (any(type(result[k]) not in (int,float) or not math.isfinite(result[k]) for k in ('start','end'))
            or not 0<=result['start']<result['end']<=duration):
        raise ValueError('Invalid cut bounds')
    return data
