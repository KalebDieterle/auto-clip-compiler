"""Prepare a 40-source grouped evaluation, then score human-labeled event intervals.

No quality result is emitted while labels or reviewed setup/payoff checks are missing.
"""
import argparse
import csv
import json
import random
from pathlib import Path
from clipcompiler.media import probe, recording_time, VIDEO_EXTS
from clipcompiler.store import file_hash, identity, atomic_json, Store

LABEL_FIELDS=['source_path','source_hash','group','split','category','known_favorite','start','end','label',
              'essential_start','essential_end','setup_payoff_preserved','correction_seconds','notes']


def split_groups(groups, favorites, size=20):
    """Calibration group ids for an exact size/size split that keeps duplicate groups intact.

    Favorite groups are dealt out first, held-out first, so recall on unseen favorites is
    measurable at all; the remaining groups fill calibration by exact subset-sum.
    """
    favored=sorted((g for g,m in groups.items() if any(p['source_path'] in favorites for p in m)),
                   key=lambda g:(-len(groups[g]),str(g)))
    if len(favored)<2: raise ValueError('Need favorites in at least two distinct groups to hold one out')
    calibration=[]; held=[]
    for i,g in enumerate(favored):
        (held if i%2==0 else calibration).append(g)
    taken=sum(len(groups[g]) for g in calibration)
    if taken>size or sum(len(groups[g]) for g in held)>size:
        raise ValueError('Favorite groups are too large for a 20/20 split')
    reachable={taken:list(calibration)}
    for gid,members in groups.items():
        if gid in favored: continue
        for count,group_ids in list(reachable.items()):
            if count+len(members)<=size: reachable.setdefault(count+len(members),group_ids+[gid])
    if size not in reachable: raise ValueError('Duplicate groups prevent 20/20 split; use a different --seed')
    return set(reachable[size])


def prepare(sources, favorites, output, seed=42):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    favorites={str(Path(p).resolve()) for p in favorites}
    paths=set(favorites)
    for folder in sources:
        paths.update(str(p.resolve()) for p in Path(folder).rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    if len(paths)<40: raise ValueError(f'Need 40 source videos; found {len(paths)}')
    shuffled=sorted(paths-favorites); random.Random(seed).shuffle(shuffled)
    # Probe a deterministic pool and stratify mechanically. "Weak" and "silent joke"
    # require human labels; absence of speech does not establish either category.
    pool=[]
    for path in sorted(favorites)+shuffled[:160]:
        try:
            meta=probe(Path(path)); when,hint=recording_time(Path(path))
            pool.append(dict(source_path=path,meta=meta,recorded_at=when,hint=hint))
        except Exception as exc:
            print(f'Skip unreadable source {path}: {exc}')
    chosen=[p for p in pool if p['source_path'] in favorites]
    for predicate,count in ((lambda p:p['meta']['audio_count']==0,4),(lambda p:p['meta']['duration']>60,6)):
        chosen.extend([p for p in pool if p not in chosen and predicate(p)][:count])
    chosen.extend([p for p in pool if p not in chosen][:40-len(chosen)])
    if len(chosen)!=40: raise ValueError('Could not prepare 40 readable clips')
    for p in chosen: p['source_hash']=file_hash(Path(p['source_path']))
    parent=list(range(40))
    def root(i):
        while parent[i]!=i: i=parent[i]
        return i
    for i,a in enumerate(chosen):
        for j,b in enumerate(chosen[:i]):
            same=a['source_hash']==b['source_hash']
            possible=a['hint'] and b['hint'] and abs(a['recorded_at']-b['recorded_at'])<=max(a['meta']['duration'],b['meta']['duration'])+5
            if same or possible: parent[root(i)]=root(j)
    groups={}
    for i,p in enumerate(chosen): groups.setdefault(root(i),[]).append(p)
    calibration=split_groups(groups,favorites); rows=[]
    for gid,members in groups.items():
        for p in members:
            category='known_favorite' if p['source_path'] in favorites else ('audio_free' if not p['meta']['audio_count'] else ('long' if p['meta']['duration']>60 else 'unlabeled'))
            if len(members)>1: category+=';possible_overlap'
            rows.append(dict(source_path=p['source_path'],source_hash=p['source_hash'],group=str(gid),
                split='calibration' if gid in calibration else 'held_out',category=category,
                known_favorite='yes' if p['source_path'] in favorites else '',start='',end='',label='',
                essential_start='',essential_end='',setup_payoff_preserved='',correction_seconds='',notes=''))
    label_path=output/'labels.csv'
    if label_path.exists(): raise ValueError('Refusing to overwrite labels.csv; use a new evaluation directory')
    with label_path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=LABEL_FIELDS); writer.writeheader(); writer.writerows(rows)
    atomic_json(output/'sources.json',[r['source_path'] for r in rows])
    atomic_json(output/'evaluation.json',{'seed':seed,'sources':len(rows),'split':'20 calibration / 20 held_out',
        'status':'awaiting_human_labels','categories':[r['category'] for r in rows],
        'limitations':['Weak clips and silent jokes require human confirmation.',
                      'Timestamp hints conservatively group potential overlaps; content confirmation is still required.']})
    return rows


def published_cuts(store, run_dir):
    """The cut list a viewer actually saw: the last render's, else the run's saved selection.

    Recomputing select() here would ignore the run's top/min-score/ranked settings and
    any later --render-from changes, so evaluation could score footage never rendered.
    """
    last=store.get('last_render')
    path=Path(last)/'selections.json' if last else Path(run_dir)/'selections.json'
    if not path.is_file(): raise ValueError(f'No saved cut list to evaluate: {path}')
    items=json.loads(path.read_text(encoding='utf-8'))
    if any('timeline_start' not in c or 'event_group' not in c for c in items):
        raise ValueError('Cut list predates shared-timeline fields; re-render with --render-from')
    return items


def measure(labels,run_dir,split):
    with Path(labels).open(encoding='utf-8-sig',newline='') as stream:
        rows=[r for r in csv.DictReader(stream) if r['split']==split]
    if not rows or any(r['label'] not in ('keep','maybe','reject') or not r['start'] or not r['end'] for r in rows):
        raise ValueError('All event intervals and keep/maybe/reject labels must be filled before measuring')
    store=Store(run_dir)
    try:
        summary=store.get('summary',{})
        if summary.get('status')!='complete': raise ValueError('Cannot score an incomplete analysis as quality success')
        items=published_cuts(store,run_dir)
        source_map={s['path']:s for s in store.sources()}
        for r in rows:
            if r['source_path'] not in source_map or source_map[r['source_path']]['hash']!=r['source_hash']:
                raise ValueError('Evaluation sources do not match the run')
        # Refuse leakage if content alignment subsequently merges split groups.
        splits={r['source_path']:r['split'] for r in csv.DictReader(Path(labels).open(encoding='utf-8-sig'))}
        sources={s['id']:s for s in store.sources()}
        for a in store.db.execute("SELECT * FROM alignments WHERE status='confirmed'"):
            if splits.get(sources[a['a']]['path'])!=splits.get(sources[a['b']]['path']):
                raise ValueError('Confirmed duplicate spans evaluation splits; regroup before scoring')
        favorites=[r for r in rows if r['known_favorite']=='yes']
        # Map labels and output to confirmed shared-event coordinates, so footage counts
        # wherever it plays, including from a duplicate recording of the same moment.
        from clipcompiler.overlap import timelines
        align=[(x['a'],x['b'],x['status'],json.loads(x['data'])) for x in store.db.execute('SELECT * FROM alignments')]
        mapping=timelines(list(sources.values()),align)
        def shared(r,start_key='start',end_key='end'):
            group,offset=mapping[source_map[r['source_path']]['id']]
            return group,float(r[start_key])+offset,float(r[end_key])+offset
        def covered(r,start_key='start',end_key='end'):
            group,a,b=shared(r,start_key,end_key)
            spans=sorted((c['timeline_start'],c['timeline_end']) for c in items if c['event_group']==group)
            cursor=a
            for x,y in spans:
                if x>cursor+.05: break
                if y>cursor: cursor=y
                if cursor>=b-.05: return True
            return False
        def included(r):
            group,a,b=shared(r)
            return any(c['event_group']==group and c['timeline_start']<b and c['timeline_end']>a for c in items)
        recalled=sum(covered(r) for r in favorites)
        missing_essential=any(not r['essential_start'] or not r['essential_end'] for r in favorites)
        preserved=None if missing_essential else all(covered(r,'essential_start','essential_end') for r in favorites)
        unwanted=sum(included(r) for r in rows if r['label']=='reject')
        repeated=0.
        for i,c in enumerate(items):
            for other in items[:i]:
                if c['event_group']==other['event_group']:
                    repeated+=max(0,min(c['timeline_end'],other['timeline_end'])-max(c['timeline_start'],other['timeline_start']))
        recall=recalled/len(favorites) if favorites else None
        manual_verified=bool(favorites) and all(r['setup_payoff_preserved']=='yes' for r in favorites)
        return {'split':split,'favorite_recall':recall,'favorites':len(favorites),'unwanted_inclusions':unwanted,
                'essential_intervals_preserved':preserved,'verified_repeated_seconds':repeated,
                'correction_seconds':sum(float(r['correction_seconds'] or 0) for r in rows),
                'runtime_seconds':summary.get('elapsed_seconds'),'peak_process_memory_bytes':summary.get('peak_process_memory_bytes'),
                'targets_met':bool(recall is not None and recall>=.9 and preserved and repeated==0 and manual_verified),
                'manual_setup_payoff_check_complete':manual_verified,
                'limitation':'Repeated seconds cover confirmed alignments; ambiguous duplicates require human review.'}
    finally: store.close()


def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare'); prep.add_argument('--source',action='append',required=True)
    prep.add_argument('--favorite',action='append',default=[]); prep.add_argument('--output',required=True); prep.add_argument('--seed',type=int,default=42)
    score=sub.add_parser('score'); score.add_argument('--labels',required=True); score.add_argument('--run',required=True)
    score.add_argument('--split',choices=['calibration','held_out'],default='held_out')
    args=p.parse_args()
    if args.command=='prepare': print(f'Prepared {len(prepare(args.source,args.favorite,args.output,args.seed))} clips in {args.output}')
    else: print(json.dumps(measure(args.labels,args.run,args.split),indent=2))


if __name__=='__main__': main()
