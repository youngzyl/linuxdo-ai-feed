import copy,time,tempfile,unittest
from pathlib import Path
from store import Store
import luna_writer as l
class LifecycleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);self.p=p;self.s=Store(p/'s.json',p/'f.jsonl');self.now=int(time.time());self.s.upsert_topics([{'id':i,'title':f'x{i}','body_text':'text','created_at':'2026-01-01T00:00:00Z'} for i in range(1,4)]);self.s.save()
 def tearDown(self):self.tmp.cleanup()
 def payload(self,x):return {'run_id':x['run_id'],'group_id':'realtime','model':'gpt-6-luna','provider':'openai','items':[{'i':i+1,'topic_id':v['id'],'source_version':v['source_version'],'valuable':True,'score':70,'category':'a','reason':'b','summary':'c'} for i,v in enumerate(x['groups']['realtime'])]}
 def test_short_group_stale_rows_receipts_new_run(self):
  x=l.snapshot(self.s,'r1',self.now);self.assertEqual(len(x['groups']['realtime']),3);p=self.payload(x)
  stale=p['items'][0]['topic_id'];self.s.set_fields(stale,body_text='new');self.assertEqual(x,l.snapshot(self.s,'r1',self.now))
  result=l.apply(self.s,p);self.assertEqual(result['accepted'],2);self.assertEqual(result['stale'],[stale]);self.assertEqual(l.apply(self.s,p),result)
  self.assertEqual(self.s.data['luna']['status'],'reconciled');self.assertEqual(l.snapshot(self.s,'r1',self.now),x)
  new=l.snapshot(self.s,'r2',self.now);self.assertEqual(len(new['groups']['realtime']),1)
  self.assertEqual(l.apply(self.s,p),result)
  reopened=Store(self.p/'s.json',self.p/'f.jsonl');self.assertEqual(l.apply(reopened,p),result)
 def test_cancel_releases_and_archives(self):
  x=l.snapshot(self.s,'r1',self.now);l.finish(self.s,'r1','cancelled');y=l.snapshot(self.s,'r2',self.now);self.assertEqual(len(y['groups']['realtime']),3);self.assertEqual(l.snapshot(self.s,'r1',self.now),x)
 def test_future_t0_rejected_and_empty_allowed(self):
  with self.assertRaises(l.Invalid):l.snapshot(self.s,'r1',self.now+3600)
  self.s.data['topics'].clear();x=l.snapshot(self.s,'r0',self.now);self.assertEqual(x['groups']['realtime'],[]);self.assertEqual(self.s.data['luna']['status'],'completed')
if __name__=='__main__':unittest.main()
