import datetime,json,pathlib,tempfile,unittest
from store import Store
from future_flex import FutureFlex
class SevenDayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=pathlib.Path(self.tmp.name);self.s=Store(p/'state.json',p/'failure');key=p/'key';key.write_text('synthetic');key.chmod(0o600);self.t=1794512882;self.now=self.t+50;self.calls=[]
  self.cfg={'future_flex':{'enabled':True,'activation_t0':self.t,'backfill_enabled':True,'backfill_t0':self.t,'backfill_prior_run_id':'old','key_file':str(key),'max_batch':8}}
  self.s.data['luna']={'run_id':'old','status':'cancelled','groups':{},'receipts':{}}
 def tearDown(self):self.tmp.cleanup()
 def stamp(self,n):return datetime.datetime.fromtimestamp(n,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
 def add(self,tid,created,first=None,verdict=None):
  self.s.upsert_topics([{'id':tid,'title':str(tid),'created_at':self.stamp(created),'first_seen_at':self.stamp(first if first is not None else created),'filter':verdict}])
 def provider(self,rows,taste,key):
  self.calls.append([r['id'] for r in rows]);return {'status':'completed','model':'gpt-6-luna','service_tier':'flex','usage':{'input_tokens':1,'output_tokens':1},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'results':[{'i':i+1,'valuable':True,'score':90,'category':'x','reason':'x','summary':'x'} for i in range(len(rows))]})}]}]}
 def test_window_settlement_reservations_and_owner_state(self):
  self.add(1,self.t-604800);self.add(2,self.t-604801);self.add(3,self.t-1);self.add(4,self.t-2);self.add(5,self.t-3,verdict={'valuable':False,'source_version':'fixture'});self.add(6,self.t+1,first=self.t+1)
  self.s.queue_add(3);self.s.set_manual_override(3,'skip');self.s.data['luna']={'run_id':'old','status':'active','groups':{'a':[{'id':4}]},'receipts':{}}
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now)
  r=f.run();self.assertEqual(r['judged'],1);self.assertEqual(self.calls,[[6]]);self.assertIsNone(self.s.get(3)['filter']);self.assertEqual(self.s.queue(),[3]);self.assertEqual(self.s.get(3)['manual_override'],'skip')
  self.s.data['luna']['status']='cancelled';r=f.run();self.assertEqual(r['judged'],2);self.assertEqual(set(self.calls[-1]),{1,3});self.assertIsNone(self.s.get(4)['filter']);self.assertIsNone(self.s.get(2)['filter']);self.assertFalse(self.s.get(5)['filter']['valuable']);self.assertEqual(self.s.queue(),[3]);self.assertEqual(self.s.get(3)['manual_override'],'skip')
  self.assertEqual(self.s.get(3)['filter']['model'],'gpt-6-luna');self.assertEqual(self.s.get(3)['filter']['service_tier'],'flex')
 def test_backfill_watermark_survives_restart_and_is_immutable(self):
  self.add(1,self.t-1);FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now);s=Store(self.s.path,self.s.failure_log);self.assertEqual(s.data['future_flex']['backfill_t0'],self.t)
  self.cfg['future_flex']['backfill_t0']+=1
  with self.assertRaises(ValueError):FutureFlex(self.cfg,s,lambda _:None,transport=self.provider,clock=lambda:self.now)
 def test_missing_or_wrong_old_run_blocks_backfill_but_not_future(self):
  self.add(1,self.t-10);self.add(2,self.t+1,first=self.t+1);self.s.data['luna']={}
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now);r=f.run()
  self.assertTrue(r['backfill_waiting']);self.assertEqual(self.calls,[[2]]);self.assertIsNone(self.s.get(1)['filter'])
  self.s.data['luna']={'run_id':'wrong','status':'completed','groups':{},'receipts':{}}
  self.assertTrue(f.run()['backfill_waiting']);self.assertIsNone(self.s.get(1)['filter'])
 def test_backfill_reinspects_legacy_text_only_after_old_run_terminal(self):
  from pipeline import Pipeline
  from singleton import CycleLock
  from unittest.mock import patch
  import config
  self.add(1,self.t-10);self.add(2,self.t-11);self.add(3,self.t-604801)
  for tid in (1,2,3):self.s.data['topics'][str(tid)]['detail_fetched']=True
  self.s.data['luna']={'run_id':'old','status':'active','groups':{'a':[{'id':2}]},'receipts':{}}
  self.cfg['future_flex']['images_enabled']=True
  cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']=self.cfg['future_flex'];cfg['fetch']['rss_delay_s']=0
  p=Pipeline(cfg,self.s,lambda _:None);p.cycle_lock=CycleLock(pathlib.Path(self.tmp.name)/'cycle');p.filter.run=lambda:{'ok':True,'key_present':True,'judged':0};calls=[]
  class C:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[],'pages':1,'page_errors':[]}
   def fetch_rss_bodies(self):return {}
   def apply_rss_bodies(self,x):return {}
   def ensure_details(self,ids):calls.extend(ids);return {}
  p.collector=C()
  with patch('pipeline.time.sleep'):p.run_cycle(do_detail=True)
  self.assertEqual(calls,[])
  self.s.data['luna']['status']='cancelled'
  with patch('pipeline.time.sleep'):p.run_cycle(do_detail=True)
  self.assertEqual(calls,[1]);self.assertTrue(self.s.get(1)['image_inspection_pending']);self.assertIsNone(self.s.get(1)['filter'])
 def test_backfill_dispatches_actual_image_bytes_with_high_flex(self):
  from unittest.mock import patch
  import base64
  self.add(1,self.t-10);topic=self.s.data['topics']['1'];topic.update(images_inspected=True,image_inspection_pending=False,images=['https://linux.do/uploads/real.jpg'],images_omitted=0)
  self.cfg['future_flex']['images_enabled']=True;seen=[]
  def provider(rows,taste,key,parts):
   seen.extend(parts);return self.provider(rows,taste,key)
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=provider,clock=lambda:self.now)
  with patch('trusted_images.fetch',return_value=b'\xff\xd8fixture\xff\xd9'):r=f.run()
  self.assertEqual(r['judged'],1);part=next(x for x in seen if x['type']=='input_image');self.assertEqual(base64.b64decode(part['image_url'].split(',',1)[1]),b'\xff\xd8fixture\xff\xd9');self.assertEqual(self.s.get(1)['filter']['model'],'gpt-6-luna')
 def test_cancelled_unprocessed_only_release_and_preserve_history(self):
  for i in range(1,5):self.add(i,self.t-i)
  self.s.data['luna']={'run_id':'old','status':'active','groups':{'realtime':[{'id':1}],'done':[{'id':2}],'unprocessed':[{'id':3}],'unknown':[{'id':4}]},'receipts':{'done':{'result':{}}}}
  self.cfg['future_flex']['backfill_release']={'run_id':'old','provider_status':'cancelled','outputs_reconciled':True,'unprocessed_groups':['unprocessed','done','realtime']}
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now)
  self.assertEqual(f.run()['judged'],0);self.assertFalse(self.calls)
  self.s.data['luna']['status']='cancelled';self.assertEqual(f.run()['judged'],1);self.assertEqual(self.calls,[[3]])
  self.assertEqual(set(self.s.data['luna']['groups']),{'realtime','done','unprocessed','unknown'});self.assertEqual(set(self.s.data['luna']['receipts']),{'done'})
  self.assertIsNone(self.s.get(1)['filter']);self.assertIsNone(self.s.get(2)['filter']);self.assertIsNone(self.s.get(4)['filter'])
  self.cfg['future_flex']['backfill_release']['unprocessed_groups']=['unknown']
  with self.assertRaises(ValueError):FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now)
 def test_release_blocks_overlaps_and_unattested_or_unknown(self):
  from future_flex import backfill_exclusions
  self.s.data['luna']={'run_id':'old','status':'cancelled','groups':{'a':[{'id':1},{'id':2}],'b':[{'id':2},{'id':3}]},'receipts':{}}
  self.s.data['luna_history']={'other':{'run_id':'other','groups':{'g':[{'id':1}]}}}
  m={'excluded_ids':[1,2,3],'backfill_prior_run_id':'old','backfill_release':{'unprocessed_groups':['a']}}
  self.assertEqual(backfill_exclusions(self.s.data,m),{1,2,3})
  del self.s.data['luna_history']['other'];self.assertEqual(backfill_exclusions(self.s.data,m),{2,3})
 def test_attested_all_71_outstanding_only_after_cancel(self):
  from future_flex import backfill_exclusions
  groups={'realtime':[{'id':i} for i in range(1,9)]};groups.update({'g%02d'%i:[{'id':100+i*8+j} for j in range(8)] for i in range(71)})
  self.s.data['luna']={'run_id':'old','status':'active','groups':groups,'receipts':{'realtime':{'result':{'accepted':7,'stale':[8]}}}}
  self.cfg['future_flex']['backfill_release']={'run_id':'old','provider_status':'cancelled','outputs_reconciled':True,'unprocessed_groups':'all_outstanding','expected_unprocessed_groups':71,'completed_output_groups':0}
  with self.assertRaises(ValueError):FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now)
  self.cfg['future_flex']['backfill_release']={'run_id':'old','provider_status':'cancelled','outputs_reconciled':True,'unprocessed_groups':['g%02d'%i for i in range(71)]}
  for i in range(100,668):self.add(i,self.t-10)
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now);self.assertEqual(f.run()['judged'],0)
  m=self.s.data['future_flex'];self.assertEqual(len(backfill_exclusions(self.s.data,m)),576)
  self.s.data['luna']['status']='cancelled';self.assertEqual(backfill_exclusions(self.s.data,m),set(range(1,9)))
  self.assertEqual(f.run()['judged'],8);self.assertEqual(len(self.calls[0]),8);self.assertTrue(all(100<=x<668 for x in self.calls[0]))
  self.s.data['luna']['receipts']['g00']={'result':{}};self.assertEqual(len(backfill_exclusions(self.s.data,m)),16)
 def test_backfill_t0_is_frozen_separately_at_execution(self):
  self.cfg['future_flex']['backfill_t0']=self.t+25
  self.add(1,self.t-604800);self.add(2,self.t-604800+25);self.add(3,self.t+5,first=self.t-10)
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now);r=f.run()
  self.assertEqual(r['judged'],2);self.assertEqual(set(self.calls[0]),{2,3});self.assertIsNone(self.s.get(1)['filter'])
 def test_max_eight_and_no_outside_week(self):
  for i in range(20):self.add(i+1,self.t-i-1)
  f=FutureFlex(self.cfg,self.s,lambda _:None,transport=self.provider,clock=lambda:self.now);self.assertEqual(f.run()['judged'],8);self.assertEqual(len(self.calls[0]),8)
if __name__=='__main__':unittest.main()
