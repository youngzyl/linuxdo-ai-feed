import json, tempfile, unittest, threading, os
from pathlib import Path
from unittest.mock import patch
from store import Store
import canary

class CanaryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.p=Path(self.tmp.name); self.s=Store(self.p/'state.json',self.p/'failures.jsonl')
  self.s.upsert_topics([{'id':i,'title':'title '+str(i),'body_text':'body','created_at':f'2026-01-{(i%28)+1:02}T00:00:00Z'} for i in range(1,49)])
  self.s.save(); self.run='run_001'
 def tearDown(self): self.tmp.cleanup()
 def snapshot(self): return canary.snapshot(self.s,self.run)
 def payload(self,snap):
  return {'run_id':self.run,'batch':0,'model':'gpt-6.1-terra','provider':'openai-codex','items':[{'i':i+1,'topic_id':v['id'],'source_version':v['source_version'],'valuable':True,'score':80,'category':'test','reason':'reason','summary':'summary'} for i,v in enumerate(snap['topics'][:8])]}
 def test_snapshot_bound_and_copy(self):
  x=self.snapshot(); self.assertEqual(len(x['topics']),40); self.assertEqual(x['batch_size'],8); x['topics'][0]['title']='changed'; self.assertNotEqual(self.s.get(x['topics'][0]['id'])['title'],'changed')
 def test_apply_replay_restart_and_preserve(self):
  x=self.snapshot(); p=self.payload(x); self.s.queue_add(3); self.s.set_manual_override(4,'skip'); r=canary.apply(self.s,p); self.assertEqual(r['accepted'],8); self.assertEqual(canary.apply(self.s,p),r); self.assertIn(3,self.s.queue()); self.assertEqual(self.s.get(4).get('manual_override'),'skip'); s2=Store(self.p/'state.json',self.p/'failures.jsonl'); self.assertEqual(canary.apply(s2,p),r); p['items'][0]['score']=1
  with self.assertRaises(canary.Conflict): canary.apply(s2,p)
 def test_source_change_refuses_entire_batch(self):
  x=self.snapshot(); p=self.payload(x); tid=p['items'][0]['topic_id']; self.s.set_fields(tid,body_text='new')
  with self.assertRaises(canary.Conflict): canary.apply(self.s,p)
  self.assertTrue(all(self.s.model_value(self.s.get(it['topic_id'])) is None for it in p['items']))
 def test_competing_verdict_same_version(self):
  x=self.snapshot(); p=self.payload(x); it=p['items'][0]; self.s.set_verdict(it['topic_id'],{'valuable':False},source_version=it['source_version'])
  with self.assertRaises(canary.Conflict): canary.apply(self.s,p)
 def test_invalid_partial_no_write(self):
  x=self.snapshot(); p=self.payload(x); p['items'].pop()
  with self.assertRaises(canary.Invalid): canary.apply(self.s,p)
 def test_failed_write_does_not_apply(self):
  x=self.snapshot(); p=self.payload(x)
  with patch('canary.os.fsync',side_effect=OSError('fault')):
   with self.assertRaises(canary.PersistenceFailure): canary.apply(self.s,p)
  self.assertTrue(all(self.s.model_value(self.s.get(it['topic_id'])) is None for it in p['items']))
 def test_concurrent_duplicate(self):
  x=self.snapshot(); p=self.payload(x); results=[]
  def run(): results.append(canary.apply(self.s,p))
  ts=[threading.Thread(target=run) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]; self.assertEqual(len(results),2); self.assertEqual(results[0],results[1])
 def test_disabled_by_default(self): self.assertFalse(canary.enabled({}))
 def test_inflight_topic_reference_survives_snapshot(self):
  ref=self.s.pending()[0]; tid=ref['id']; self.snapshot()
  self.assertIs(ref,self.s.get(tid), 'active scheduler references must remain live')

 def test_snapshot_keeps_nested_references(self):
  t=self.s.pending()[0]; tags=['old']; t['tags']=tags; self.snapshot()
  self.assertIs(self.s.get(t['id'])['tags'],tags)
 def test_concurrent_bookmark_preserved(self):
  x=self.snapshot(); p=self.payload(x); errors=[]; barrier=threading.Barrier(2)
  def commit():
   try: barrier.wait(); canary.apply(self.s,p)
   except Exception as e: errors.append(type(e).__name__)
  def bookmark():
   try: barrier.wait(); self.s.queue_add(9); self.s.save()
   except Exception as e: errors.append(type(e).__name__)
  ts=[threading.Thread(target=commit),threading.Thread(target=bookmark)]
  [t.start() for t in ts]; [t.join() for t in ts]
  self.assertEqual(errors,[]); self.assertIn(9,Store(self.p/'state.json',self.p/'failures.jsonl').queue())
if __name__=='__main__': unittest.main()
