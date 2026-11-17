import datetime,json,pathlib,tempfile,threading,unittest
from unittest.mock import patch
from store import Store
try:from future_flex import FutureFlex,FlexError
except ImportError:FutureFlex=None;FlexError=Exception
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=pathlib.Path(self.tmp.name);self.store=Store(p/'state.json',p/'failures');self.key=p/'key';self.key.write_text('synthetic');self.key.chmod(0o600)
  self.cfg={'future_flex':{'enabled':True,'activation_t0':1794512882,'key_file':str(self.key),'max_batch':8,'backoff_seconds':60}}
  self.now=1794513000
 def tearDown(self):self.tmp.cleanup()
 def add(self,tid,created,first,filter=None):
  self.store.upsert_topics([{'id':tid,'title':str(tid),'created_at':created,'first_seen_at':first,'filter':filter}])
 def stamp(self,ts):return datetime.datetime.fromtimestamp(ts,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
 def provider(self,*args):
  batch=args[0];return {'status':'completed','model':'gpt-6-luna','service_tier':'flex','usage':{'input_tokens':10,'output_tokens':5,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens_details':{'reasoning_tokens':2}},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'results':[{'i':i+1,'valuable':True,'score':90,'category':'x','reason':'x','summary':'x'} for i in range(len(batch))]})}]}]}
 def test_cutoff_reservation_bound_and_persistence(self):
  self.add(1,self.stamp(self.now-200),self.stamp(self.now));self.add(2,self.stamp(self.now-1),self.stamp(self.now-200));self.add(3,self.stamp(self.now+200),self.stamp(self.now));
  for i in range(10,20):self.add(i,self.stamp(self.now-1),self.stamp(self.now-1))
  self.store.data['luna']={'status':'active','groups':{'realtime':[{'id':19}]},'receipts':{}}
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=self.provider,clock=lambda:self.now);r=f.run();self.assertEqual(r['judged'],8);self.assertIsNone(self.store.get(19)['filter']);self.assertIsNone(self.store.get(1)['filter']);self.assertIsNone(self.store.get(2)['filter']);self.assertIsNone(self.store.get(3)['filter']);
  reloaded=Store(self.store.path,self.store.failure_log);self.assertEqual(reloaded.data['future_flex']['activation_t0'],1794512882)
 def test_429_backoff_and_auth_block(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));calls=[]
  def limit(*a):calls.append(1);raise FlexError('rate_limited',429)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=limit,clock=lambda:self.now);self.assertFalse(f.run()['ok']);f.run();self.assertEqual(len(calls),1);self.assertIsNone(self.store.get(10)['filter'])
 def test_unknown_blocks_without_replay(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));calls=[]
  def unknown(*a):calls.append(1);raise FlexError('outcome_unknown')
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=unknown,clock=lambda:self.now);f.run();f.run();self.assertEqual(len(calls),1);self.assertEqual(self.store.data['future_flex']['status'],'unknown')
 def test_response_wrong_tier_never_applied(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def wrong(*a):v=self.provider(*a);v['service_tier']='default';return v
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=wrong,clock=lambda:self.now);self.assertFalse(f.run()['ok']);self.assertIsNone(self.store.get(10)['filter'])
 def test_auth_disable_persists_after_restart(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));calls=[]
  def auth(*a):calls.append(1);raise FlexError('auth_rejected',401)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=auth,clock=lambda:self.now);self.assertEqual(f.run()['error'],'auth_rejected')
  self.store=Store(self.store.path,self.store.failure_log);f=FutureFlex(self.cfg,self.store,lambda x:None,transport=auth,clock=lambda:self.now);self.assertEqual(f.run()['error'],'auth_disabled');self.assertEqual(len(calls),1)
 def test_existing_stale_model_verdict_not_rejudged(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1),{'valuable':True,'source_version':'old'})
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=lambda *a:self.fail('not allowed'),clock=lambda:self.now);self.assertEqual(f.run()['judged'],0)
 def test_terminal_prior_reservation_is_excluded(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));self.store.data['luna']={'status':'completed','groups':{'g':[{'id':10}]},'receipts':{'g':{}}}
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=lambda *a:self.fail('not allowed'),clock=lambda:self.now);self.assertEqual(f.run()['judged'],0)
 def test_actual_wire_contract_and_no_retry(self):
  import future_flex as ff
  from unittest.mock import MagicMock
  c=MagicMock();c.getresponse.return_value.status=429;c.getresponse.return_value.read.return_value=b'{}'
  with patch.object(ff.http.client,'HTTPSConnection',return_value=c) as fac:
   with self.assertRaises(FlexError) as e:ff.request([{'id':1}], '', 'synthetic')
  self.assertEqual(e.exception.code,'rate_limited');fac.assert_called_once_with('api.openai.com',443,timeout=210);self.assertEqual(c.request.call_count,1)
  body=json.loads(c.request.call_args.kwargs['body']);self.assertEqual(body['model'],'gpt-6-luna');self.assertEqual(body['service_tier'],'flex');self.assertEqual(body['reasoning'],{'effort':'high'});self.assertFalse(body['store']);self.assertNotIn('tools',body)
 def test_simultaneous_dispatch_no_overlap(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));entered=threading.Event();release=threading.Event();calls=[]
  def slow(*a):calls.append(1);entered.set();release.wait(3);return self.provider(*a)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=slow,clock=lambda:self.now);out=[];t=threading.Thread(target=lambda:out.append(f.run()));t.start();self.assertTrue(entered.wait(2));other=f.run();release.set();t.join(3);self.assertFalse(t.is_alive());self.assertEqual(other['error'],'already_running');self.assertEqual(len(calls),1)
 def test_reservation_appearing_during_request_prevents_commit(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def race(*a):
   v=self.provider(*a)
   with self.store.lock:self.store.data['luna']={'status':'active','groups':{'g':[{'id':10}]},'receipts':{}}
   return v
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=race,clock=lambda:self.now);r=f.run();self.assertEqual(r['stale'],1);self.assertIsNone(self.store.get(10)['filter'])
 def test_auth_403_disables(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def auth(*a):raise FlexError('auth_rejected',403)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=auth,clock=lambda:self.now);self.assertFalse(f.run()['ok']);self.assertEqual(self.store.data['future_flex']['status'],'auth_disabled');self.assertEqual(self.store.attention(self.cfg)['kind'],'future_flex_auth_disabled')
 def test_pipeline_selects_future_provider_and_not_legacy(self):
  import config,time
  from pipeline import Pipeline
  from singleton import CycleLock
  now=int(time.time());cfg=config.load_config();cfg['filter']['enabled']=True;cfg['future_flex']={'enabled':True,'images_enabled':True,'activation_t0':now-5,'key_file':str(self.key),'max_batch':8}
  with patch('pipeline.Filter',side_effect=AssertionError('legacy provider used')):
   p=Pipeline(cfg,self.store,lambda x:None)
  p.cycle_lock=CycleLock(pathlib.Path(self.tmp.name)/'.cycle');p.filter.transport=self.provider
  class Collector:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[{'id':41,'title':'future','created_at':datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'images':[],'images_omitted':0,'images_inspected':True}],'pages':1,'page_errors':[],'list_source':'fixture'}
  p.collector=Collector();r=p.run_cycle(do_detail=False);self.assertTrue(r['filter']['ok']);self.assertEqual(r['filter']['judged'],1);self.assertEqual(self.store.get(41)['filter']['model'],'gpt-6-luna')
 def test_unknown_persists_and_activation_immutable_after_restart(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1));calls=[]
  def unknown(*a):calls.append(1);raise FlexError('outcome_unknown')
  FutureFlex(self.cfg,self.store,lambda x:None,transport=unknown,clock=lambda:self.now).run();self.store=Store(self.store.path,self.store.failure_log)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=unknown,clock=lambda:self.now);self.assertEqual(f.run()['error'],'unknown');self.assertEqual(len(calls),1)
  self.cfg['future_flex']['activation_t0']+=1
  with self.assertRaises(ValueError):FutureFlex(self.cfg,self.store,lambda x:None,transport=unknown,clock=lambda:self.now)
 def test_malformed_response_and_wrong_model_never_commit(self):
  for variant in ('malformed','model'):
   with self.subTest(variant=variant):
    self.add(100 if variant=='malformed' else 101,self.stamp(self.now-1),self.stamp(self.now-1))
    def bad(*a):
     b=self.provider(*a)
     if variant=='malformed':b['output'][0]['content'][0]['text']='not json'
     else:b['model']='wrong'
     return b
    f=FutureFlex(self.cfg,self.store,lambda x:None,transport=bad,clock=lambda:self.now);self.assertEqual(f.run()['error'],'response_invalid');self.assertEqual(self.store.data['future_flex']['status'],'unknown')
    with self.store.lock:self.store.data['future_flex']['status']='ready'
 def test_cas_stale_does_not_rejudge(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def race(*a):v=self.provider(*a);self.store.get(10)['title']='changed';return v
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=race,clock=lambda:self.now);r=f.run();self.assertEqual(r['stale'],1);self.assertIsNone(self.store.get(10)['filter']);f.transport=self.provider;r=f.run();self.assertEqual(r['judged'],1)
if __name__=='__main__':unittest.main()
