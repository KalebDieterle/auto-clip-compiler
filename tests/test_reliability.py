import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clipcompiler.judge import Ollama, validate_judgment
from clipcompiler.store import Store, identity
from clipcompiler.selection import boundaries, select, validate_override
from clipcompiler.media import windows
from clipcompiler.overlap import visual_alignment, timelines


def moment(**changes):
    return dict(dict(start=2.,end=8.,setup='Friend walks up',event='Unexpected joke',payoff='Surprise',
        reaction='Laughter',payoff_time=6.,score=7.,confidence='medium',recommendation='keep',reason='Visible surprise',
        boundary_uncertain=False),**changes)


class JudgeTests(unittest.TestCase):
    def test_malformed_and_partial_are_failures(self):
        for data in ({},[],{'id':'wrong'}, {'id':'w','coverage':'','moments':[]},
                     {'id':'w','coverage':'all'}, {'id':'w','coverage':'all','moments':[{'score':5}]}):
            with self.subTest(data=data),self.assertRaises(ValueError): validate_judgment(data,'w',0,10)

    def test_scores_and_timestamps_strict(self):
        for changes in ({'score':math.nan},{'score':math.inf},{'score':0},{'score':11},{'score':True},
                        {'start':-1},{'end':11},{'end':1},{'payoff_time':9},{'confidence':'sure'},
                        {'recommendation':'failed'},{'boundary_uncertain':'false'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_judgment({'id':'w','coverage':'all','moments':[moment(**changes)]},'w',0,10)

    def test_explicit_rejection_is_valid_not_failed(self):
        self.assertEqual(validate_judgment({'id':'w','coverage':'Routine play, no event','moments':[]},'w',0,10)['moments'],[])

    def test_empty_malformed_timeout_and_truncated_response(self):
        c=Ollama()
        for response in ({'done':True,'message':{'content':''}}, {'done':True,'message':{'content':'not json'}},
                         {'done':False,'message':{'content':'{}'}}, {'done':True,'done_reason':'length','message':{'content':'{}'}}):
            with patch.object(c,'request',return_value=response),self.assertRaises((ValueError,json.JSONDecodeError)):
                c.chat('prompt',{})
        with patch.object(c,'request',side_effect=TimeoutError('timeout')),self.assertRaises(TimeoutError): c.chat('prompt',{})

    def test_local_only(self):
        for url in ('https://127.0.0.1:11434','http://example.com','http://127.0.0.1.evil','http://user@localhost','http://localhost/x'):
            with self.subTest(url=url),self.assertRaises(ValueError): Ollama(url=url)
        with self.assertRaises(ValueError): Ollama(model='qwen3-vl:235b-cloud')
        self.assertEqual(Ollama(url='http://localhost:11434').url,'http://127.0.0.1:11434')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=Store(self.temp.name)
    def tearDown(self): self.store.close(); self.temp.cleanup()

    def test_retry_twice_then_explicit_failure(self):
        attempts=[]
        def fail(): attempts.append(1); raise ValueError('bad judge')
        with patch('clipcompiler.store.time.sleep'),self.assertRaises(ValueError): self.store.stage('s','judge','k',fail)
        row=self.store.db.execute('SELECT * FROM stages').fetchone()
        self.assertEqual((len(attempts),row['status'],row['attempts']),(3,'failed',3))

    def test_resume_reuses_complete_stage_and_settings_invalidate(self):
        calls=[]
        def compute(): calls.append(1); return {'ok':True}
        self.store.stage('s','transcription','source-v1',compute)
        self.store.close(); self.store=Store(self.temp.name)
        self.store.stage('s','transcription','source-v1',compute)
        self.store.stage('s','judgment','model-v1',compute)
        self.store.stage('s','judgment','model-v2',compute)
        self.store.stage('s','transcription','source-v2',compute)
        self.assertEqual(len(calls),4)

    def test_interrupted_stage_retries(self):
        with self.assertRaises(KeyboardInterrupt):
            self.store.stage('s','judge','v',lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(self.store.stage('s','judge','v',lambda:42),42)

    def add(self,sid='s',digest='h',status='complete',duration=30):
        self.store.source(sid,Path(self.temp.name)/(sid+'.mp4'),digest,status,{'duration':duration,'recorded_at':0,'audio_stream':0})
        item=moment(id=sid+'-c',source=sid,source_hash=digest,transcript='hi')
        self.store.replace_candidates(sid,[item]); return item

    def test_transcript_length_never_selects_rejected_or_failed(self):
        item=self.add()
        for transcript in ('','a','short','a very long transcript with many words'):
            item.update(transcript=transcript,recommendation='reject')
            self.store.replace_candidates('s',[item]); self.assertEqual(select(self.store),[])
        self.add(status='failed'); self.assertEqual(select(self.store),[])

    def test_override_survives_reanalysis_and_removed_candidate(self):
        item=self.add(); self.store.override(item,{'recommendation':'reject'})
        self.store.replace_candidates('s',[item]); self.assertEqual(select(self.store),[])
        self.store.override(item,{'recommendation':'keep','start':1,'end':10})
        self.store.replace_candidates('s',[])
        self.assertEqual([(c['start'],c['end']) for c in select(self.store)],[(1,10)])
        self.store.source('s',Path(self.temp.name)/'s.mp4','changed','complete',{'duration':30,'recorded_at':0})
        self.assertEqual(select(self.store),[])

    def test_overlap_union_keeps_unique_tails(self):
        a=self.add('a'); b=self.add('b')
        a.update(start=0,end=12); b.update(start=0,end=12)
        self.store.replace_candidates('a',[a]); self.store.replace_candidates('b',[b])
        self.align('a','b',8,8,11)
        result=select(self.store)
        self.assertEqual(sum(c['end']-c['start'] for c in result),20)
        spans=sorted((c['timeline_start'],c['timeline_end']) for c in result)
        self.assertEqual(spans,[(0,12),(12,20)])

    def align(self,a,b,offset,first,last,status='confirmed'):
        data={'offset':offset,'first_match':first,'last_match':last,'method':'frames_and_audio'}
        with self.store.db: self.store.db.execute('INSERT INTO alignments VALUES (?,?,?,?)',(a,b,status,json.dumps(data)))

    def test_only_matched_frames_are_treated_as_shared(self):
        # 90% matching accepted the pair, but only 8..10 (a-time) was seen to match.
        a=self.add('a'); b=self.add('b')
        a.update(start=0,end=12); b.update(start=0,end=12)
        self.store.replace_candidates('a',[a]); self.store.replace_candidates('b',[b])
        self.align('a','b',8,8,9)
        spans=sorted((c['timeline_start'],c['timeline_end']) for c in select(self.store))
        # b's unverified 10..12 (timeline) is kept rather than assumed identical to a's.
        self.assertEqual(spans,[(0,12),(10,20)])

    def test_legacy_alignment_without_match_span_keeps_footage(self):
        a=self.add('a'); b=self.add('b')
        with self.store.db: self.store.db.execute('INSERT INTO alignments VALUES (?,?,?,?)',('a','b','confirmed',json.dumps({'offset':0})))
        self.assertEqual(len(select(self.store)),2)

    def test_reaction_never_precedes_setup_across_recordings(self):
        # b is a later-named replay of the same event; its filename time is EARLIER than a's
        # (clock skew), which used to sort b's reaction tail ahead of a's setup.
        self.store.source('a',Path(self.temp.name)/'a.mp4','ha','complete',{'duration':30,'recorded_at':100,'audio_stream':0})
        self.store.source('b',Path(self.temp.name)/'b.mp4','hb','complete',{'duration':30,'recorded_at':50,'audio_stream':0})
        self.store.replace_candidates('a',[moment(id='a-c',source='a',source_hash='ha',start=0,end=12,payoff_time=6)])
        self.store.replace_candidates('b',[moment(id='b-c',source='b',source_hash='hb',start=0,end=12,payoff_time=6)])
        self.align('a','b',8,8,11)
        result=select(self.store)
        self.assertEqual([c['source'] for c in result],['a','b'])
        self.assertEqual([c['timeline_start'] for c in result],[0,12])

    def test_renamed_candidate_cannot_bypass_review(self):
        item=self.add(); self.store.override(item,{'recommendation':'reject'})
        # Re-analysis words the same event differently, so its ID changes.
        renamed=dict(item,id='new-id',event='A joke, described anew',start=2.5,end=8.5,payoff_time=6)
        self.store.replace_candidates('s',[renamed])
        self.assertEqual(select(self.store),[])
        self.assertEqual([c['id'] for c in self.store.candidates()],[item['id']])

    def test_trimmed_review_survives_reanalysis(self):
        item=self.add(); self.store.override(item,{'start':3,'end':7})
        self.store.replace_candidates('s',[dict(item,id='new-id',start=1,end=9)])
        self.assertEqual([(c['start'],c['end']) for c in select(self.store)],[(3,7)])
        # Editing the decision again keeps the original anchor interval.
        decision=self.store.candidates()[0]; self.store.override(decision,{'start':4,'end':7})
        self.assertEqual((self.store.candidates()[0]['anchor_start'],self.store.candidates()[0]['anchor_end']),(2,8))

    def test_distinct_new_event_is_not_suppressed(self):
        item=self.add(); self.store.override(item,{'recommendation':'reject'})
        other=dict(item,id='later',start=20,end=26,payoff_time=24)
        self.store.replace_candidates('s',[item,other])
        self.assertEqual([c['id'] for c in select(self.store)],['later'])

    def test_ambiguous_match_does_not_remove_content(self):
        self.add('a'); self.add('b')
        with self.store.db: self.store.db.execute('INSERT INTO alignments VALUES (?,?,?,?)',('a','b','possible','{"offset":0}'))
        self.assertEqual(len(select(self.store)),2)


class ContextTests(unittest.TestCase):
    def test_full_short_and_complete_long_coverage(self):
        self.assertEqual(windows(59),[(0,59)])
        self.assertEqual(windows(75),[(0,30),(20,50),(40,70),(60,75)])
        for second in range(75): self.assertTrue(any(a<=second<b for a,b in windows(75)))

    def test_uncertain_short_preserves_entire_replay(self):
        c=boundaries(moment(boundary_uncertain=True),[],45)
        self.assertEqual((c['start'],c['end']),(0,45))

    def test_setup_reaction_expand_to_speech(self):
        c=boundaries(moment(start=3,end=8),[{'start':1,'end':4},{'start':7,'end':10}],20)
        self.assertEqual((c['start'],c['end']),(1,10))

    def test_override_bounds(self):
        for change in ({'start':-1},{'end':99},{'end':math.nan},{'score':10},{'recommendation':'failed'}):
            with self.assertRaises(ValueError): validate_override(moment(),change,30)

    def test_static_frames_cannot_prove_overlap(self):
        import numpy as np
        static=[(float(i),123,np.arange(1024,dtype=float).reshape(32,32)%256) for i in range(12)]
        self.assertIsNone(visual_alignment(static,static))

    def test_distinct_aligned_frames_and_different_gameplay(self):
        import numpy as np
        rng=np.random.default_rng(42)
        a=[(float(i),int(rng.integers(0,2**62)),rng.uniform(0,255,(32,32))) for i in range(15)]
        b=[(t-5,h,p.copy()) for t,h,p in a[5:]]
        self.assertEqual(visual_alignment(a,b)['offset'],5)
        other=[(t,h^0xffff,p[::-1].copy()) for t,h,p in b]
        self.assertIsNone(visual_alignment(a,other))

    def test_inconsistent_alignment_is_preserved(self):
        result=timelines([{'id':s} for s in 'abc'],[('a','b','confirmed',{'offset':3}),
            ('b','c','confirmed',{'offset':4}),('a','c','confirmed',{'offset':19})])
        self.assertEqual(result,{'a':('a',0),'b':('b',0),'c':('c',0)})


if __name__=='__main__': unittest.main()
