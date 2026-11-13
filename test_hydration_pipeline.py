import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
from pipeline import Pipeline
from singleton import CycleLock
from store import Store


class HydrationPipeline(unittest.TestCase):
    def test_reinspect_rotation_only_advances_attempted_on_early_stop(self):
        with tempfile.TemporaryDirectory() as d:
            pth=pathlib.Path(d); key=pth/'key'; key.write_text('fake'); key.chmod(0o600)
            store=Store(pth/'state.json',pth/'failures'); cfg=config.load_config()
            cfg['filter']['enabled']=True
            cfg['future_flex']={'enabled':True,'images_enabled':True,'activation_t0':1794512882,'key_file':str(key),'detail_backoff_seconds':1}
            cfg['fetch'].update(rss_delay_s=0,detail_delay_s=0,jina_delay_s=0,detail_max_per_cycle=8,detail_max_consecutive_failures=1)
            for tid in (1,2):
                store.upsert_topics([{'id':tid,'title':str(tid),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z'}])
                store.set_fields(tid,detail_fetched=True,image_inspection_pending=True,images_inspected=False)
            pipe=Pipeline(cfg,store,lambda _:None);pipe.cycle_lock=CycleLock(pth/'cycle');pipe.filter.clock=lambda:1794513000
            c=pipe.collector;c.categories={1:'x'};c.fetch_list=lambda pages=None:{'topics':[],'pages':1,'page_errors':[]};c.fetch_rss_bodies=lambda:{}
            attempts=[]
            def fail(tid):
                attempts.append(tid);raise RuntimeError('early stop fixture')
            c.detail_direct=fail;c.detail_jina=lambda tid:(_ for _ in ()).throw(RuntimeError('early stop fixture'))
            with patch('pipeline.time.sleep'),patch('collector.time.sleep'),patch('collector.http_util.cooldown_remaining',return_value=0):
                for now in (1000,1061,1122,1183):
                    with patch('collector.time.time',return_value=now),patch('pipeline.time.time',return_value=now):pipe.run_cycle()
            self.assertEqual(attempts,[1,2,1,2])

    def test_pending_then_real_collector_inspection_and_fake_provider(self):
        with tempfile.TemporaryDirectory() as d:
            pth = pathlib.Path(d); key = pth/'key'; key.write_text('fake'); key.chmod(0o600)
            store = Store(pth/'state.json', pth/'failures')
            cfg = config.load_config(); cfg['filter']['enabled'] = True
            cfg['future_flex'] = {'enabled': True, 'images_enabled': True, 'activation_t0': 1794512882, 'key_file': str(key), 'detail_backoff_seconds': 60}
            cfg['fetch'].update(rss_delay_s=0, detail_delay_s=0, jina_delay_s=0, detail_max_per_cycle=4, detail_max_consecutive_failures=1)
            for tid in (1, 2):
                store.upsert_topics([{'id':tid, 'title':str(tid), 'created_at':'2026-11-12T19:49:00Z', 'first_seen_at':'2026-11-12T19:49:00Z'}])
            store.set_fields(1, detail_fetched=True, image_inspection_pending=True, images_inspected=False)
            pipe = Pipeline(cfg, store, lambda _: None); pipe.cycle_lock = CycleLock(pth/'cycle')
            pipe.filter.clock = lambda: 1794513000
            calls=[]; attempts=[]
            def provider(rows, taste, key, parts):
                calls.extend(x['id'] for x in rows)
                return {'status':'completed', 'model':'gpt-6-luna', 'service_tier':'flex', 'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'results':[{'i':i+1,'valuable':True,'score':80,'category':'x','reason':'x','summary':'x'} for i in range(len(rows))]})}]}]}
            pipe.filter.transport = provider
            c = pipe.collector; c.categories={1:'x'}
            c.fetch_list=lambda pages=None:{'topics':[], 'pages':1, 'page_errors':[]}
            c.fetch_rss_bodies=lambda:{}
            def fail(tid):
                attempts.append(tid)
                raise RuntimeError('fixture failure')
            c.detail_direct=fail; c.detail_jina=lambda tid: (_ for _ in ()).throw(RuntimeError('fixture failure'))
            with patch('pipeline.time.sleep'), patch('collector.time.sleep'), patch('collector.time.time', return_value=1000), patch('pipeline.time.time', return_value=1000), patch('collector.http_util.cooldown_remaining', return_value=0):
                for _ in range(3):
                    pipe.run_cycle()
                    self.assertNotIn('future_flex_image_retry',store.get(1))
                    self.assertEqual(calls, [])
                self.assertEqual(attempts, [1, 2])
                self.assertEqual(store.get(1)['future_flex_detail_retry']['attempts'],1)
            def success(tid):
                attempts.append(tid)
                return c._body_from_topic_json({'post_stream':{'posts':[{'post_number':1,'cooked':'<p>fixture</p><img src="https://linux.do/uploads/a.jpg">'}]}})
            c.detail_direct=success
            with patch('pipeline.time.sleep'), patch('collector.time.sleep'), patch('collector.time.time', return_value=1061), patch('pipeline.time.time', return_value=1061), patch('collector.http_util.cooldown_remaining', return_value=0), patch('trusted_images.fetch', return_value=b'fake'):
                pipe.run_cycle()
                self.assertTrue(store.get(1)['images_inspected'])
                pipe.run_cycle()
            self.assertIn(1,calls)
            self.assertIsNotNone(store.get(1)['filter'])
            self.assertIsNone(store.get(1).get('future_flex_detail_retry'))
            self.assertNotIn('future_flex_image_retry',store.get(1))

if __name__ == '__main__':
    unittest.main()
