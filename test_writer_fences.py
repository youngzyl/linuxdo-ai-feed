import tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from store import Store
from filter import Filter

class WriterFenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);self.s=Store(p/'state.json',p/'failure.jsonl');self.s.upsert_topics([{'id':1,'title':'synthetic','body_text':'body','created_at':'2026-01-01T00:00:00Z'}]);self.s.save()
 def tearDown(self):self.tmp.cleanup()
 def test_delayed_existing_filter_does_not_overwrite_luna_same_version(self):
  version=self.s.get(1)['source_version'];start=threading.Event();resume=threading.Event()
  f=Filter({'filter':{'model':'ordinary','prompt_version':'v1','batch_size':8}},self.s,lambda *x:None)
  def judge(key,batch,snapshot=None):
   start.set();self.assertTrue(resume.wait(5));return ([{'topic_id':1,'source_version':version,'valuable':False,'model':'ordinary'}],[])
  f.judge_batch=judge;result=[]
  def worker():
   with patch('filter.resolve_api_key',return_value=('dummy','fixture')),patch('filter.time.sleep',return_value=None):result.append(f.run())
  th=threading.Thread(target=worker);th.start();self.assertTrue(start.wait(5))
  self.s.set_verdict(1,{'valuable':True,'model':'gpt-6-luna'},source_version=version);resume.set();th.join(5)
  self.assertFalse(th.is_alive());self.assertEqual(self.s.get(1)['filter']['model'],'gpt-6-luna');self.assertEqual(result[0]['stale'],1)
 def test_active_reservation_blocks_new_dispatch_and_inflight_apply(self):
  version=self.s.get(1)['source_version'];batch,expected=self.s.dispatch_pending()
  self.assertEqual(len(batch),1)
  with self.s.lock:self.s.data['luna']={'status':'active','groups':{'realtime':[{'id':1,'source_version':version}]}}
  self.assertEqual(self.s.dispatch_pending()[0],[])
  result=self.s.set_verdict(1,{'valuable':False},source_version=version,expected_filter=expected[1],avoid_luna_reserved=True)
  self.assertEqual(result,'stale');self.assertIsNone(self.s.model_value(self.s.get(1)))
 def test_manual_override_does_not_break_compare_and_set(self):
  version=self.s.get(1)['source_version'];batch,expected=self.s.dispatch_pending();self.s.set_manual_override(1,'skip')
  status=self.s.set_verdict(1,{'valuable':True},source_version=version,expected_filter=expected[1],avoid_luna_reserved=True)
  self.assertEqual(status,'applied');self.assertEqual(self.s.get(1)['manual_override'],'skip');self.assertEqual(self.s.get(1)['state'],'rejected')
if __name__=='__main__':unittest.main()
