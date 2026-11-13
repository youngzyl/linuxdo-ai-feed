import http.client,json,os,tempfile,threading,time,unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from server import App,make_handler
from store import Store
from filter import Filter
class EndToEndWriter(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.state=self.root/'state.json';self.fail=self.root/'fail.jsonl';self.s=Store(self.state,self.fail)
  self.s.upsert_topics([{'id':i,'title':'synthetic','body_text':'test','created_at':'2026-01-01T00:00:00Z'} for i in range(1,4)]);self.s.save()
  self.env=patch.dict(os.environ,{'LINUXDO_AI_OWNER_TOKEN':'fixture-owner'});self.env.start();self.start_server()
 def start_server(self):
  self.app=App({'luna_enabled':True},self.s,None,lambda x:None);self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.app));self.th=threading.Thread(target=self.http.serve_forever,daemon=True);self.th.start()
 def stop_server(self):self.http.shutdown();self.http.server_close();self.th.join()
 def tearDown(self):self.stop_server();self.env.stop();self.tmp.cleanup()
 def post(self,path,body,token='fixture-owner'):
  c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=10);c.request('POST',path,json.dumps(body),{'Authorization':'Bearer '+token,'Content-Type':'application/json'});r=c.getresponse();raw=r.read();n=r.status;c.close();return n,json.loads(raw)
 def snapshot(self,run='r1'):
  status,result=self.post('/api/luna/snapshot',{'run_id':run,'t0':int(time.time())});self.assertEqual(status,200);return result
 def payload(self,snap):
  return {'run_id':snap['run_id'],'group_id':'realtime','model':'gpt-6-luna','provider':'openai','items':[{'i':j+1,'topic_id':x['id'],'source_version':x['source_version'],'valuable':True,'score':90,'category':'a','reason':'b','summary':'c'} for j,x in enumerate(snap['groups']['realtime'])]}
 def test_finish_cancel_short_group_and_immutable_snapshot_http(self):
  snap=self.snapshot();self.assertEqual(len(snap['groups']['realtime']),3);tid=snap['groups']['realtime'][0]['id'];self.s.set_fields(tid,body_text='changed')
  self.assertEqual(self.post('/api/luna/snapshot',{'run_id':'r1','t0':snap['t0']})[1],snap)
  self.assertEqual(self.post('/api/luna/finish',{'run_id':'r1','status':'cancelled'},'wrong')[0],401)
  self.assertEqual(self.post('/api/luna/finish',{'run_id':'r1','status':'cancelled'})[1]['status'],'cancelled')
  self.assertEqual(len(self.snapshot('r2')['groups']['realtime']),3)
  self.assertEqual(self.post('/api/luna/snapshot',{'run_id':'r1','t0':snap['t0']})[1],snap)
 def test_empty_snapshot_no_request_and_next_run(self):
  self.s.data['topics'].clear();x=self.snapshot();self.assertEqual(x['groups']['realtime'],[]);self.assertEqual(self.s.data['luna']['status'],'completed');self.assertEqual(self.snapshot('r2')['groups']['realtime'],[])
 def test_restart_receipt_http(self):
  snap=self.snapshot();payload=self.payload(snap);status,receipt=self.post('/api/luna/apply',payload);self.assertEqual(status,200);self.stop_server();self.s=Store(self.state,self.fail);self.start_server()
  self.assertEqual(self.post('/api/luna/apply',payload),(200,receipt));payload['items'][0]['score']=1;self.assertEqual(self.post('/api/luna/apply',payload)[0],409)
 def test_real_scheduler_delayed_filter_uses_reservation_fence(self):
  import config
  from pipeline import Pipeline,Scheduler
  from singleton import CycleLock
  cfg=config.load_config();cfg['_data_dir']=str(self.root);cfg['filter']['model']='old-broken'
  pipe=Pipeline(cfg,self.s,lambda _:None);pipe.cycle_lock=CycleLock(self.root/'.fixture-cycle.lock')
  class Collector:
   categories=[1]
   def fetch_list(self,pages=None):return {'topics':[],'pages':1,'page_errors':[],'list_source':'fixture'}
  pipe.collector=Collector();started=threading.Event();release=threading.Event();finished=threading.Event();results=[]
  def judge(key,batch,snapshot=None):
   started.set();self.assertTrue(release.wait(10));return ([{'topic_id':v['id'],'source_version':snapshot[v['id']],'valuable':False,'model':'old-broken'} for v in batch],[])
  pipe.filter.judge_batch=judge
  class Runner:
   store=self.s
   def run_cycle(inner):
    try:
     r=pipe.run_cycle(do_detail=False);results.append(r);return r
    finally:finished.set()
   def next_delay_seconds(inner):return 3600
  scheduler=Scheduler(Runner(),lambda _:None)
  with patch('filter.resolve_api_key',return_value=('fixture','fixture')),patch('pipeline.resolve_api_key',return_value=('fixture','fixture')):
   scheduler.start()
   try:
    self.assertTrue(started.wait(10));snap=self.snapshot();self.assertEqual(self.s.dispatch_pending()[0],[])
    self.assertEqual(self.post('/api/luna/apply',self.payload(snap))[0],200);release.set();self.assertTrue(finished.wait(10))
   finally:release.set();scheduler.stop()
  self.assertEqual(results[0]['filter']['stale'],3)
  self.assertTrue(all(self.s.get(i)['filter']['model']=='gpt-6-luna' for i in range(1,4)))
 def test_inflight_scheduler_conflict_and_reservation_blocks_new_dispatch(self):
  started=threading.Event();release=threading.Event();results=[]
  f=Filter({'filter':{'model':'old-broken','prompt_version':'v1','batch_size':8}},self.s,lambda _:None)
  def judge(key,batch,snapshot=None):
   started.set();self.assertTrue(release.wait(10));return ([{'topic_id':v['id'],'source_version':snapshot[v['id']],'valuable':False,'model':'old-broken'} for v in batch],[])
  f.judge_batch=judge
  def run():
   with patch('filter.resolve_api_key',return_value=('fixture','fixture')),patch('filter.time.sleep',return_value=None):results.append(f.run())
  th=threading.Thread(target=run);th.start();self.assertTrue(started.wait(5));snap=self.snapshot()
  self.assertEqual(self.s.dispatch_pending()[0],[]);status,receipt=self.post('/api/luna/apply',self.payload(snap));self.assertEqual(status,200);release.set();th.join(10);self.assertFalse(th.is_alive())
  self.assertEqual(results[0]['stale'],3);self.assertTrue(all(self.s.get(i)['filter']['model']=='gpt-6-luna' for i in range(1,4)))
if __name__=='__main__':unittest.main()
