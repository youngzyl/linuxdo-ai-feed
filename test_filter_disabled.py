import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import config
from pipeline import Pipeline
from singleton import CycleLock
from store import Store
class DisabledFilterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);self.s=Store(p/'state.json',p/'failures.jsonl');self.s.upsert_topics([{'id':1,'title':'pending','created_at':'2026-01-01T00:00:00Z'}]);self.s.queue_add(1);self.s.save();self.cfg=config.load_config();self.cfg['filter']['enabled']=False;self.pipe=Pipeline(self.cfg,self.s,lambda _:None);self.pipe.cycle_lock=CycleLock(p/'.cycle')
  class Collector:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[{'id':2,'title':'new','created_at':'2026-01-02T00:00:00Z'}],'pages':1,'page_errors':[],'list_source':'fixture'}
  self.pipe.collector=Collector()
 def tearDown(self):self.tmp.cleanup()
 def test_disabled_skips_all_model_calls_while_fetch_and_bookmark_work(self):
  self.s.health_update(filter_consecutive_errors=2,filter_last_success_at='2026-01-01T00:00:00Z')
  with patch.object(self.pipe.filter,'run',side_effect=AssertionError('model called')),patch('pipeline.resolve_api_key',side_effect=AssertionError('key accessed')):
   result=self.pipe.run_cycle(do_detail=False)
  self.assertTrue(result['ok']);self.assertEqual(result['fetch']['topics_seen'],1);self.assertFalse(result['filter']['enabled']);self.assertIsNone(result['filter']['ok']);self.assertEqual(result['filter']['skipped'],'disabled')
  self.assertEqual(len(self.s.pending()),2);self.assertIn(1,self.s.queue());self.s.queue_add(2);self.s.save();self.assertEqual(self.s.queue(),[1,2])
  health=self.s.health_payload(cfg=self.cfg,uptime_s=0);self.assertEqual(health['filter']['enabled'],False);self.assertIsNone(health['filter']['ok']);self.assertEqual(health['filter_consecutive_errors'] if 'filter_consecutive_errors' in health else health['summary']['filter_consecutive_errors'],2);self.assertEqual(health['attention']['kind'],'filter_disabled');self.assertNotEqual(health['status'],'ok')
 def test_default_enabled(self):self.assertIs(config.DEFAULTS['filter']['enabled'],True)
 def test_invalid_non_boolean_fails_closed(self):
  self.cfg['filter']['enabled']='false'
  with self.assertRaises(ValueError):Pipeline(self.cfg,self.s,lambda _:None)
 def test_reenabled_resets_disable_marker(self):
  with patch.object(self.pipe.filter,'run',return_value={'ok':True,'key_present':True,'judged':0}):
   with patch('pipeline.resolve_api_key',return_value=('fixture','fixture')):self.pipe.run_cycle(do_detail=False)
  self.cfg['filter']['enabled']=True
  with patch.object(self.pipe.filter,'run',return_value={'ok':True,'key_present':True,'judged':0}),patch('pipeline.resolve_api_key',return_value=('fixture','fixture')):
   r=self.pipe.run_cycle(do_detail=False)
  self.assertTrue(r['filter']['ok']);self.assertTrue(self.s.section('filter').get('enabled',True));self.assertNotEqual(self.s.section('filter').get('skipped'),'disabled')
if __name__=='__main__':unittest.main()
