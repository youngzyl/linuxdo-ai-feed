import copy,datetime,http.client,json,os,subprocess,sys,tempfile,threading,unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from store import Store
from server import App,make_handler
import luna_writer as luna
T0=1763000000
def t(i,created):return {'id':i,'title':'topic '+str(i),'body_text':'synthetic','created_at':datetime.datetime.fromtimestamp(created,datetime.timezone.utc).isoformat()}
class WriterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name);self.s=Store(self.p/'state.json',self.p/'failures.jsonl')
  self.s.upsert_topics([t(i,T0-i) for i in range(1,21)]+[t(101,T0-86400),t(102,T0),t(103,T0-86401)]);self.s.save()
 def tearDown(self):self.tmp.cleanup()
 def snapshot(self):return luna.snapshot(self.s,'run1',T0)
 def payload(self,snap,group):
  return {'run_id':'run1','group_id':group,'model':'gpt-6-luna','provider':'openai','items':[{'i':i+1,'topic_id':v['id'],'source_version':v['source_version'],'valuable':True,'score':80,'category':'AI','reason':'reason','summary':'summary'} for i,v in enumerate(snap['groups'][group])]}
 def test_selection_exact_bounds_and_group_no_overlap(self):
  x=self.snapshot();self.assertEqual([i['id'] for i in x['groups']['realtime']],list(range(1,9)))
  batch=[i['id'] for k,g in x['groups'].items() if k!='realtime' for i in g]
  self.assertEqual(set(batch),set(range(9,21))|{101});self.assertEqual(len(batch),len(set(batch)))
  self.assertNotIn(102,batch);self.assertNotIn(103,batch);self.assertEqual(x,luna.snapshot(self.s,'run1',T0))
  with self.assertRaises(luna.Conflict):luna.snapshot(self.s,'run1',T0+1)
 def test_snapshot_bounds_and_bad_date_fail_before_commit(self):
  before=self.s.path.read_bytes()
  with patch.object(luna,'MAX_TOPICS',10):
   with self.assertRaises(luna.Conflict):self.snapshot()
  self.assertEqual(before,self.s.path.read_bytes());self.assertNotIn('luna',self.s.data)
  self.s.data['topics']['1']['created_at']='not-a-date'
  with self.assertRaises(luna.Conflict):self.snapshot()
  self.assertEqual(before,self.s.path.read_bytes())
 def test_prereplace_fsync_failure_does_not_apply(self):
  x=self.snapshot();p=self.payload(x,'realtime');before=self.s.path.read_bytes()
  with patch('canary.os.fsync',side_effect=OSError('fault')):
   with self.assertRaises(luna.PersistenceFailure):luna.apply(self.s,p)
  self.assertEqual(before,self.s.path.read_bytes());self.assertEqual(self.s.data['luna']['receipts'],{})
 def test_batch_and_realtime_commit_receipt_restart(self):
  x=self.snapshot();groups=list(x['groups']);p=self.payload(x,groups[1]);q=self.payload(x,'realtime')
  with self.assertRaises(luna.Conflict):luna.apply(self.s,p)
  self.assertEqual(luna.apply(self.s,q)['accepted'],8);r=luna.apply(self.s,p);self.assertEqual(r['accepted'],8)
  self.assertEqual(r,luna.apply(self.s,p));s2=Store(self.p/'state.json',self.p/'failures.jsonl');self.assertEqual(r,luna.apply(s2,p))
  self.assertEqual(luna.apply(s2,q)['accepted'],8)
  altered=copy.deepcopy(p);altered['items'][0]['score']=1
  with self.assertRaises(luna.Conflict):luna.apply(s2,altered)
 def test_same_version_competition_and_source_change_atomic(self):
  x=self.snapshot();p=self.payload(x,'realtime');a=p['items'][0];self.s.set_verdict(a['topic_id'],{'valuable':False},source_version=a['source_version'])
  result=luna.apply(self.s,p)
  self.assertEqual(result['stale'],[a['topic_id']]);self.assertEqual(result['accepted'],len(p['items'])-1)
  self.assertTrue(all(self.s.model_value(self.s.get(it['topic_id'])) is True for it in p['items'][1:]))
 def test_batch_source_change_after_realtime_is_atomic(self):
  x=self.snapshot();luna.apply(self.s,self.payload(x,'realtime'))
  bg=next(k for k in x['groups'] if k!='realtime');b=self.payload(x,bg);self.s.set_fields(b['items'][0]['topic_id'],body_text='new')
  result=luna.apply(self.s,b)
  self.assertEqual(result['stale'],[b['items'][0]['topic_id']]);self.assertEqual(result['accepted'],len(b['items'])-1)
  self.assertIsNone(self.s.model_value(self.s.get(b['items'][0]['topic_id'])))
 def test_invalid_model_unknown_overlap_and_error_are_rejected(self):
  x=self.snapshot();p=self.payload(x,'realtime')
  for variant in ('badmodel','unknown','duplicate','short','error','cross'):
   q=copy.deepcopy(p)
   if variant=='badmodel':q['model']='gpt-6.1-terra'
   if variant=='unknown':q['group_id']='g-unknown'
   if variant=='duplicate':q['items'][1]=copy.deepcopy(q['items'][0]);q['items'][1]['i']=2
   if variant=='short':q['items'].pop()
   if variant=='error':q['error']={'code':'expired'}
   if variant=='cross':q['items'][0]['topic_id']=x['groups'][next(k for k in x['groups'] if k!='realtime')][0]['id']
   with self.subTest(variant=variant):
    with self.assertRaises((luna.Invalid,luna.Conflict)):luna.apply(self.s,q)
  self.assertEqual(self.s.data['luna']['receipts'],{})
 def test_concurrent_feedback_bookmark_and_apply(self):
  x=self.snapshot();p=self.payload(x,'realtime');barrier=threading.Barrier(3);errors=[]
  def work(fn):
   try:barrier.wait();fn()
   except BaseException as e:errors.append(type(e).__name__)
  def owner():self.s.set_manual_override(2,'skip');self.s.save()
  def bookmark():self.s.queue_add(3);self.s.save()
  ts=[threading.Thread(target=work,args=(fn,)) for fn in (lambda:luna.apply(self.s,p),owner,bookmark)]
  for a in ts:a.start()
  for a in ts:a.join(10)
  self.assertFalse(any(a.is_alive() for a in ts));self.assertEqual(errors,[])
  s=Store(self.p/'state.json',self.p/'failures.jsonl');self.assertIn(3,s.queue());self.assertEqual(s.get(2)['manual_override'],'skip');self.assertTrue(s.get(2)['filter']['valuable']);self.assertEqual(luna.apply(s,p)['accepted'],8)
 def test_simultaneous_different_receipts_only_one_wins(self):
  x=self.snapshot();a=self.payload(x,'realtime');b=copy.deepcopy(a);b['items'][0]['score']=10;barrier=threading.Barrier(2);results=[]
  def worker(p):
   barrier.wait()
   try:results.append(('ok',luna.apply(self.s,p)))
   except luna.Conflict:results.append(('conflict',None))
  ts=[threading.Thread(target=worker,args=(v,)) for v in (a,b)]
  for t in ts:t.start()
  for t in ts:t.join(5)
  self.assertFalse(any(t.is_alive() for t in ts));self.assertEqual(sorted(v[0] for v in results),['conflict','ok'])
 def test_existing_orchestrator_group_ids_and_normalized_results_integrate(self):
  from batch_offline import Ledger,prepare,reconcile
  from test_batch_offline import response
  from cryptography.fernet import Fernet
  from luna_bridge import to_writer_payload
  x=self.snapshot();ledger=Ledger(self.p/'ledger.db');prepare(ledger,'run1',T0,self.s,'',self.p/'input.enc',Fernet.generate_key())
  groups=ledger.members('run1');self.assertEqual(set(groups),set(x['groups'])-{'realtime'})
  rt={'realtime':[{'id':r['id'],'source_version':r['source_version']} for r in x['groups']['realtime']]}
  rtplan=reconcile(rt,[json.dumps(response('realtime',rt['realtime']))],[])
  self.assertEqual(luna.apply(self.s,to_writer_payload('run1','realtime',{'verdicts':rtplan['verdicts']['realtime']}))['accepted'],8)
  lines=[json.dumps(response(gid,members)) for gid,members in reversed(list(groups.items()))]
  plan=reconcile(groups,lines,[])
  for gid,members in groups.items():self.assertEqual(luna.apply(self.s,to_writer_payload('run1',gid,plan))['accepted'],len(members))
 def test_postreplace_crash_recovery(self):
  x=self.snapshot();p=self.payload(x,'realtime');(self.p/'payload.json').write_text(json.dumps(p))
  code='''import os,sys,json\nfrom pathlib import Path\nfrom unittest.mock import patch\nfrom store import Store\nimport luna_writer,canary\np=Path(sys.argv[1]);s=Store(p/"state.json",p/"failures.jsonl"); payload=json.loads((p/"payload.json").read_text()); orig=os.replace\ndef replaced(a,b):\n orig(a,b);raise OSError("after replace")\nwith patch("canary.os.replace",side_effect=replaced):luna_writer.apply(s,payload)\n(p/"AFTER").write_text("bad")'''
  r=subprocess.run([sys.executable,'-c',code,str(self.p)],cwd=str(Path(__file__).parent),capture_output=True,timeout=30)
  self.assertEqual(r.returncode,70);self.assertFalse((self.p/'AFTER').exists());s=Store(self.p/'state.json',self.p/'failures.jsonl');self.assertEqual(luna.apply(s,p)['accepted'],8)
class HTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);self.s=Store(p/'s.json',p/'f.jsonl');self.logs=[];self.app=App({},self.s,None,self.logs.append)
  self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.app));self.th=threading.Thread(target=self.http.serve_forever,daemon=True);self.th.start();self.env=patch.dict(os.environ,{'LINUXDO_AI_OWNER_TOKEN':'dummy-owner'});self.env.start()
 def tearDown(self):self.env.stop();self.http.shutdown();self.http.server_close();self.th.join();self.tmp.cleanup()
 def req(self,method,path,token=None,origin=None):
  c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=3);h={'Content-Type':'application/json'}
  if token:h['Authorization']='Bearer '+token
  if origin:h['Origin']=origin
  c.request(method,path,json.dumps({'run_id':'run1','t0':T0}),h);r=c.getresponse();r.read();c.close();return r.status
 def test_unknown_exception_sanitized(self):
  self.app.cfg['luna_enabled']=True
  with patch('server.luna_mod.snapshot',side_effect=RuntimeError('synthetic-sensitive-marker')):
   self.assertEqual(self.req('POST','/api/luna/snapshot','dummy-owner'),500)
  self.assertNotIn('synthetic-sensitive-marker',' '.join(self.logs))
 def test_http_snapshot_apply_and_auth(self):
  self.app.cfg['luna_enabled']=True;self.s.upsert_topics([t(i,T0-i) for i in range(1,17)])
  def post(path,payload,token='dummy-owner'):
   c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=5);c.request('POST',path,json.dumps(payload),{'Content-Type':'application/json','Authorization':'Bearer '+token});r=c.getresponse();data=json.loads(r.read());status=r.status;c.close();return status,data
  status,snap=post('/api/luna/snapshot',{'run_id':'run1','t0':T0});self.assertEqual(status,200)
  rows=[{'i':i+1,'topic_id':v['id'],'source_version':v['source_version'],'valuable':True,'score':80,'category':'AI','reason':'reason','summary':'summary'} for i,v in enumerate(snap['groups']['realtime'])]
  payload={'run_id':'run1','group_id':'realtime','model':'gpt-6-luna','provider':'openai','items':rows}
  self.assertEqual(post('/api/luna/apply',payload,'wrong')[0],401);self.assertEqual(post('/api/luna/apply',payload)[0],200);self.assertEqual(post('/api/luna/apply',payload)[1]['accepted'],8)
 def test_default_disabled_and_all_methods_auth(self):
  path='/api/luna/snapshot';self.assertEqual(self.req('POST',path,'dummy-owner'),404);self.app.cfg['luna_enabled']=True
  self.assertEqual(self.req('POST',path),401);self.assertEqual(self.req('POST',path,'wrong'),401);self.assertEqual(self.req('POST',path,'dummy-owner','https://untrusted.example'),403)
  self.assertEqual(self.req('POST','/api/luna/apply'),401);self.assertEqual(self.req('POST','/api/luna/apply','wrong'),401);self.assertEqual(self.req('POST','/api/luna/apply','dummy-owner','https://untrusted.example'),403)
  self.assertEqual(self.req('POST',path,'dummy-owner'),200)
  for method in ('GET','HEAD','OPTIONS','PUT','DELETE','PATCH','TRACE','CONNECT'):
   for endpoint in ('snapshot','apply'):
    with self.subTest(method=method,endpoint=endpoint):self.assertIn(self.req(method,'/api/luna/'+endpoint,'dummy-owner'),(404,405,501))
if __name__=='__main__':unittest.main()
