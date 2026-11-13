import datetime,json,pathlib,tempfile,threading,unittest
from unittest.mock import patch
from store import Store,source_fingerprint
from collector import Collector
from future_flex import FutureFlex
class MultimodalTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=pathlib.Path(self.tmp.name);self.s=Store(self.p/'state.json',self.p/'failure');self.key=self.p/'key';self.key.write_text('fake');self.key.chmod(0o600);self.t0=1794512882;self.now=1794513000
  self.cfg={'site_url':'https://linux.do','tag_page_url':'https://linux.do/tag/ai','future_flex':{'enabled':True,'images_enabled':True,'activation_t0':self.t0,'key_file':str(self.key),'max_batch':8,'backoff_seconds':60},'fetch':{'body_cap':3000,'use_rss':True},'request_timeout_s':4}
 def tearDown(self):self.tmp.cleanup()
 def add(self):self.s.upsert_topics([{'id':1,'title':'one','created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z'}])
 def provider(self,rows,taste,key,*parts):return {'status':'completed','model':'gpt-6-luna','service_tier':'flex','usage':{'input_tokens':10,'output_tokens':5},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'results':[{'i':i+1,'valuable':True,'score':80,'category':'x','reason':'x','summary':'x'} for i in range(len(rows))]})}]}]}
 def test_real_rss_extraction_and_persist(self):
  self.add();c=Collector(self.cfg,self.s,lambda x:None);rss=b'<rss><channel><item><link>https://linux.do/t/topic/1</link><description><![CDATA[<p>text</p><img src="https://linux.do/uploads/a.jpg">]]></description></item></channel></rss>'
  with patch('collector.http_util.get',return_value=(200,rss,{})):items=c.fetch_rss_bodies()
  c.apply_rss_bodies(items);self.assertEqual(self.s.get(1)['images'],['https://linux.do/uploads/a.jpg']);self.assertTrue(self.s.get(1)['images_inspected']);self.s.save();other=Store(self.s.path,self.s.failure_log);self.assertEqual(other.get(1)['images'],['https://linux.do/uploads/a.jpg'])
 def test_actual_provider_body_contains_sanitized_image_and_index(self):
  import io,base64,future_flex,trusted_images
  from PIL import Image
  from unittest.mock import MagicMock
  b=io.BytesIO();Image.new('RGB',(240,180),'red').save(b,format='PNG');image=trusted_images.sanitize(b.getvalue(),'image/png')
  from multimodal_payload import build_image_parts
  parts=build_image_parts([{'images':['x'],'images_omitted':0}],lambda _:image)['parts'];c=MagicMock();c.getresponse.return_value.status=200;c.getresponse.return_value.read.return_value=b'{}'
  with patch('future_flex.http.client.HTTPSConnection',return_value=c):future_flex.request([{'id':1}], '', 'fake',parts)
  body=json.loads(c.request.call_args.kwargs['body']);arr=body['input'][0]['content'];self.assertEqual(body['model'],'gpt-6-luna');self.assertEqual(body['service_tier'],'flex');self.assertIn('index 1',arr[1]['text']);self.assertEqual(base64.b64decode(arr[2]['image_url'].split(',',1)[1]),image)
 def test_future_pipeline_refuses_without_explicit_images(self):
  import config
  from pipeline import Pipeline
  cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']={'enabled':True,'activation_t0':self.t0,'key_file':str(self.key)}
  with self.assertRaises(ValueError):Pipeline(cfg,self.s,lambda _:None)
  cfg['future_flex']['images_enabled']=False
  with self.assertRaises(ValueError):Pipeline(cfg,self.s,lambda _:None)
 def test_pipeline_reinspects_future_pending_but_not_old_or_reserved(self):
  import config
  from pipeline import Pipeline
  from singleton import CycleLock
  cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']=self.cfg['future_flex'];cfg['fetch']['rss_delay_s']=0
  for tid,ts in [(1,'2026-11-12T19:49:00Z'),(2,'2026-11-11T19:49:00Z'),(3,'2026-11-12T19:49:00Z')]:
   self.s.upsert_topics([{'id':tid,'title':str(tid),'created_at':ts,'first_seen_at':ts,'detail_fetched':True,'image_inspection_pending':True}])
  self.s.data['luna']={'status':'active','groups':{'g':[{'id':3}]},'receipts':{}}
  p=Pipeline(cfg,self.s,lambda _:None);p.cycle_lock=CycleLock(self.p/'.cycle');p.filter.run=lambda:{'ok':True,'key_present':True,'judged':0};calls=[]
  class CollectorFixture:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[],'pages':1,'page_errors':[]}
   def fetch_rss_bodies(self):return {}
   def apply_rss_bodies(self,x):return {}
   def ensure_details(self,ids):calls.extend(ids);return {}
  p.collector=CollectorFixture()
  with patch('pipeline.time.sleep'):p.run_cycle(do_detail=True)
  self.assertEqual(calls,[1])
 def test_rss_and_cooked_persist_without_sv1_change(self):
  self.add();before=source_fingerprint(self.s.get(1));c=Collector(self.cfg,self.s,lambda x:None);c.apply_rss_bodies({1:{'body_text':'','images':['https://linux.do/uploads/a.jpg'],'images_omitted':0,'images_inspected':True}});self.assertEqual(self.s.get(1)['images'],['https://linux.do/uploads/a.jpg']);self.assertEqual(before,source_fingerprint(self.s.get(1)))
  x=c._body_from_topic_json({'post_stream':{'posts':[{'post_number':1,'cooked':'<p>hi</p><img src="https://linux.do/uploads/b.jpg">'}]}});self.assertEqual(x['images'],['https://linux.do/uploads/b.jpg']);self.assertTrue(x['images_inspected'])
 def test_batch_image_limit_defers_without_rejecting(self):
  for i in (1,2,3):self.s.upsert_topics([{'id':i,'title':str(i),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z','images':['https://linux.do/uploads/%s-%s.jpg'%(i,j) for j in range(4)],'images_omitted':0,'images_inspected':True}])
  lengths=[]
  def provider(rows,taste,key,parts):lengths.append(sum(x['type']=='input_image' for x in parts));return self.provider(rows,taste,key,parts)
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=provider,clock=lambda:self.now)
  with patch('trusted_images.fetch',return_value=b'fixture-jpeg'):r=f.run()
  self.assertEqual(lengths,[8]);self.assertEqual(r['judged'],2);self.assertTrue(r['ok']);self.assertFalse(r['partial']);self.assertEqual(r['image_incomplete'],0);self.assertEqual(r['image_deferred'],1);self.assertEqual(sum(self.s.get(i).get('filter') is None for i in (1,2,3)),1)
 def test_two_instances_cannot_both_dispatch_after_image_fetch(self):
  self.add();self.s.set_fields(1,images=['https://linux.do/uploads/a.jpg'],images_omitted=0,images_inspected=True,detail_fetched=True)
  barrier=threading.Barrier(2);entered=threading.Event();release=threading.Event();calls=[];out=[]
  def fetch(_):barrier.wait(timeout=3);return b'fake'
  def provider(*args):calls.append(1);entered.set();release.wait(3);return self.provider(*args)
  a=FutureFlex(self.cfg,self.s,lambda x:None,transport=provider,clock=lambda:self.now);b=FutureFlex(self.cfg,self.s,lambda x:None,transport=provider,clock=lambda:self.now)
  with patch('trusted_images.fetch',side_effect=fetch):
   ts=[threading.Thread(target=lambda f=f:out.append(f.run())) for f in (a,b)]
   for t in ts:t.start()
   self.assertTrue(entered.wait(3));release.set()
   for t in ts:t.join(4);self.assertFalse(t.is_alive())
  self.assertEqual(len(calls),1)
 def test_byte_budget_defers_and_oversize_quarantines_individually(self):
  for i in (1,2,3):
   self.s.upsert_topics([{'id':i,'title':str(i),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z','images':['https://linux.do/%s.jpg'%i],'images_omitted':0,'images_inspected':True}])
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=self.provider,clock=lambda:self.now)
  def fetch(url):return b'x'*(11 if url.endswith('/3.jpg') else 6)
  with patch('multimodal_payload.MAX_TOTAL_BYTES',10),patch('trusted_images.fetch',side_effect=fetch):r=f.run()
  self.assertEqual(r['judged'],1);self.assertEqual(r['image_deferred'],1);self.assertTrue(r['partial'])
  self.assertEqual(self.s.get(3)['future_flex_image_retry']['reason'],'individual_oversize')
  self.assertNotIn('future_flex_image_retry',self.s.get(1));self.assertNotIn('future_flex_image_retry',self.s.get(2))
  with patch('multimodal_payload.MAX_TOTAL_BYTES',10),patch('trusted_images.fetch',side_effect=fetch):r=f.run()
  self.assertEqual(r['judged'],1)
 def test_reinspection_is_bounded_fair_and_respects_retry(self):
  import config,time
  from pipeline import Pipeline
  from singleton import CycleLock
  from future_flex import image_signature
  cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']=self.cfg['future_flex'];cfg['fetch'].update(rss_delay_s=0,detail_max_per_cycle=4)
  for i in range(1,9):
   self.s.upsert_topics([{'id':i,'title':str(i),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z','detail_fetched':i<=5,'image_inspection_pending':i<=5}])
  for i in (4,5):
   t=self.s.get(i);t['future_flex_detail_retry']={'attempts':1,'next_retry':time.time()+1000}
  p=Pipeline(cfg,self.s,lambda x:None);p.cycle_lock=CycleLock(self.p/'cycle');p.filter.run=lambda:{'ok':True,'key_present':True,'judged':0};calls=[]
  class C:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[],'pages':1,'page_errors':[]}
   def fetch_rss_bodies(self):return {}
   def apply_rss_bodies(self,x):return {}
   def ensure_details(self,ids):
    calls.append(ids)
    for tid in ids:
     topic=self_store.get(tid)
     if topic.get('image_inspection_pending'):
      meta=self_store.data['future_flex'];meta['future_flex_reinspection_seq']=meta.get('future_flex_reinspection_seq',0)+1;topic['future_flex_reinspection_seq']=meta['future_flex_reinspection_seq']
    return {}
  self_store=self.s
  p.collector=C()
  with patch('pipeline.time.sleep'):
   for _ in range(3):p.run_cycle(do_detail=True)
  self.assertEqual([next(i for i in x if i in (1,2,3)) for x in calls],[1,2,3]);self.assertEqual([x[0] for x in calls],[1,6,3]);self.assertTrue(all(set(x)&{6,7,8}=={6,7,8} for x in calls));self.assertTrue(all(4 not in x and 5 not in x for x in calls))
 def test_image_fetch_failure_does_not_call_provider_or_commit(self):
  self.add();self.s.set_fields(1,images=['https://linux.do/uploads/a.jpg'],images_omitted=0,images_inspected=True,detail_fetched=True)
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=lambda *a:self.fail('must not call provider'),clock=lambda:self.now)
  with patch('trusted_images.fetch',side_effect=ValueError('synthetic')):r=f.run()
  self.assertFalse(r['ok']);self.assertEqual(r['error'],'image_download_failed');self.assertEqual(r['batches_failed'],1);self.assertEqual(self.s.get(1)['future_flex_image_retry']['attempts'],1);self.assertIsNone(self.s.get(1)['filter']);self.assertNotEqual(self.s.data['future_flex']['status'],'unknown')
 def test_one_image_failure_does_not_block_other_topic(self):
  for i in (1,2):self.s.upsert_topics([{'id':i,'title':str(i),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z','images':['https://linux.do/%s.jpg'%i],'images_omitted':0,'images_inspected':True}])
  def fetch(url):
   if url.endswith('/1.jpg'):raise ValueError('fixture')
   return b'jpeg'
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=self.provider,clock=lambda:self.now)
  with patch('trusted_images.fetch',side_effect=fetch):r=f.run()
  self.assertEqual(r['judged'],1);self.assertTrue(r['ok']);self.assertEqual(r['batches_ok'],1);self.assertEqual(r['batches_failed'],0);self.assertIsNone(r['error']);self.assertEqual(r['image_incomplete'],1);self.assertTrue(r['partial']);self.assertIsNone(self.s.get(1)['filter']);self.assertIsNotNone(self.s.get(2)['filter']);self.assertEqual(self.s.get(1)['future_flex_image_retry']['attempts'],1)
 def test_too_many_images_quarantined_but_other_topics_advance(self):
  for i in (1,2):self.s.upsert_topics([{'id':i,'title':str(i),'created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z','images':[],'images_omitted':2 if i==1 else 0,'images_inspected':True}])
  clock=[self.now];f=FutureFlex(self.cfg,self.s,lambda x:None,transport=self.provider,clock=lambda:clock[0]);r=f.run();self.assertEqual(r['judged'],1);self.assertTrue(r['partial'])
  for _ in range(2):clock[0]+=61;f.run()
  retry=self.s.get(1)['future_flex_image_retry'];self.assertEqual(retry['attempts'],3);self.assertTrue(retry['quarantined']);self.assertIsNone(self.s.get(1)['filter'])
  self.s.set_fields(1,images_omitted=0);clock[0]+=61;self.assertEqual(f.run()['judged'],1)
 def test_image_race_refuses_and_does_not_change_sv1(self):
  self.add();self.s.set_fields(1,images=['https://linux.do/uploads/a.jpg'],images_omitted=0,images_inspected=True,detail_fetched=True);before=source_fingerprint(self.s.get(1))
  def provider(rows,taste,key,*parts):
   self.s.set_fields(1,images=['https://linux.do/uploads/b.jpg']);return self.provider(rows,taste,key,*parts)
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=provider,clock=lambda:self.now)
  with patch('trusted_images.fetch',return_value=b'fixture-jpeg'):r=f.run()
  self.assertEqual(r['stale'],1);self.assertIsNone(self.s.get(1)['filter']);self.assertEqual(before,source_fingerprint(self.s.get(1)));self.assertNotIn('future_flex_attempted',self.s.get(1))
  f.transport=self.provider
  with patch('trusted_images.fetch',return_value=b'fixture-jpeg'):r=f.run()
  self.assertEqual(r['judged'],1)
 def test_pipeline_health_http_fail_partial_and_waiting(self):
  import config
  from pipeline import Pipeline
  from singleton import CycleLock
  from unittest.mock import MagicMock
  cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']=dict(self.cfg['future_flex'],backoff_seconds=1)
  self.add();self.s.set_fields(1,images=[],images_omitted=0,images_inspected=True)
  p=Pipeline(cfg,self.s,lambda _:None);p.cycle_lock=CycleLock(self.p/'cycle');clock=[self.now];p.filter.clock=lambda:clock[0]
  p.collector.categories=[1];p.collector.fetch_list=lambda pages=None:{'topics':[],'pages':1,'page_errors':[]}
  conn=MagicMock();conn.getresponse.return_value.status=429;conn.getresponse.return_value.read.return_value=b'limited'
  with patch('future_flex.http.client.HTTPSConnection',return_value=conn):
   for n in (1,2):
    result=p.run_cycle(do_detail=False)['filter'];self.assertFalse(result['ok']);self.assertEqual(result['error'],'rate_limited');self.assertEqual(self.s.data['health']['filter_consecutive_errors'],n);clock[0]+=2
  self.s.upsert_topics([{'id':2,'title':'waiting','created_at':'2026-11-12T19:49:00Z','first_seen_at':'2026-11-12T19:49:00Z'}]);p.filter.transport=self.provider
  result=p.run_cycle(do_detail=False)['filter'];self.assertTrue(result['ok']);self.assertEqual(result['batches_ok'],1);self.assertTrue(result['partial']);self.assertEqual(result['image_incomplete'],1);self.assertIsNone(result['error']);self.assertEqual(self.s.data['health']['filter_consecutive_errors'],0)
  self.s.record_filter_failure('fixture');self.s.record_filter_failure('fixture')
  result=p.run_cycle(do_detail=False)['filter'];self.assertTrue(result['ok']);self.assertTrue(result['waiting_images']);self.assertEqual(result['batches_failed'],0);self.assertEqual(self.s.data['health']['filter_consecutive_errors'],0)
 def test_waiting_omitted_and_quarantined(self):
  self.add();self.s.set_fields(1,images=[],images_omitted=1,images_inspected=True)
  f=FutureFlex(self.cfg,self.s,lambda x:None,transport=lambda *x:self.fail('no provider'),clock=lambda:self.now)
  for _ in range(2):
   result=f.run();self.assertTrue(result['ok']);self.assertTrue(result['waiting_images']);self.assertEqual(result['batches_failed'],0);self.assertEqual(result['image_incomplete'],1);self.assertIsNone(result['error'])
  self.s.get(1)['future_flex_image_retry']['quarantined']=True
  result=f.run();self.assertTrue(result['ok']);self.assertTrue(result['waiting_images']);self.assertEqual(result['batches_failed'],0)
if __name__=='__main__':unittest.main()
