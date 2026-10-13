import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from clipcompiler.media import command,probe
from clipcompiler.render import publish,manifest
from clipcompiler.store import file_hash,Store
from clipcompiler.review import create_app


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.root=Path(cls.temp.name)
        cls.src=cls.root/"friend's video.mp4"; cls.silent=cls.root/'silent.mp4'
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=15:duration=3',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3','-c:v','libx264','-c:a','aac',cls.src])
        command(['ffmpeg','-v','error','-y','-i',cls.src,'-an','-c:v','copy',cls.silent])
        cls.settings={'ffmpeg':'ffmpeg','ffprobe':'ffprobe','fps':15,'render_height':90,'workers':2,
                      'resolve':True,'render':True,'min_free_gb':0}
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def items(self):
        return [dict(id=str(i),source=str(i),source_path=str(p),source_hash=file_hash(p),start=0,end=2,
                     duration=probe(p)['duration'],audio_stream=probe(p)['audio_stream']) for i,p in enumerate((self.src,self.silent))]

    def test_real_render_audio_and_silent_csv_xml_agree_and_previous_survives(self):
        items=self.items(); before=[file_hash(self.src),file_hash(self.silent)]
        a=publish(items,self.root,self.settings); original=file_hash(a/'Combined_highlights.mp4')
        b=publish(items,self.root,self.settings)
        self.assertNotEqual(a,b); self.assertEqual(file_hash(a/'Combined_highlights.mp4'),original)
        self.assertAlmostEqual(probe(b/'Combined_highlights.mp4')['duration'],4,delta=.15)
        clips=ET.parse(b/'Combined.fcpxml').findall('.//asset-clip')
        self.assertEqual([c.attrib['duration'] for c in clips],['30/15s','30/15s'])
        self.assertEqual([file_hash(self.src),file_hash(self.silent)],before)
        self.assertEqual(len((b/'manifest.csv').read_text().splitlines()),3)

    def test_failed_render_never_publishes(self):
        output=self.root/'failure'
        with patch('clipcompiler.render.command',side_effect=RuntimeError('encoder failed')),self.assertRaises(RuntimeError):
            publish(self.items(),output,self.settings)
        self.assertFalse(any(p.name.startswith('20') for p in output.rglob('*') if p.is_dir()))
        reports=[json.loads(p.read_text()) for p in output.rglob('render.json')]
        self.assertTrue(reports)
        self.assertTrue(all(r['status']=='failed' for r in reports))
        self.assertFalse(list(output.glob('render-/*/Combined_highlights.mp4')))

    def test_changed_source_is_rejected(self):
        items=self.items(); items[0]['source_hash']='not-the-content'
        with self.assertRaises(ValueError): publish(items,self.root,self.settings)


class ReviewTests(unittest.TestCase):
    def test_token_origin_validation_and_override(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            store.source('s',Path(root)/'source.mp4','hash','complete',{'duration':10})
            store.replace_candidates('s',[dict(id='c',source='s',source_hash='hash',start=1,end=5,recommendation='maybe')])
            store.close()
            client=TestClient(create_app(root,'secret'),base_url='http://127.0.0.1')
            self.assertEqual(client.get('/api/items').status_code,403)
            headers={'X-Review-Token':'secret'}
            self.assertEqual(client.get('/api/items',headers=headers).status_code,200)
            self.assertEqual(client.post('/api/override/c',headers=dict(headers,Origin='http://evil'),json={'recommendation':'keep'}).status_code,403)
            self.assertEqual(client.post('/api/override/c',headers=headers,json={'end':99}).status_code,422)
            self.assertEqual(client.post('/api/override/c',headers=headers,json={'recommendation':'keep'}).status_code,200)
            self.assertEqual(client.get('/api/items',headers=headers).json()['candidates'][0]['recommendation'],'keep')


if __name__=='__main__': unittest.main()
