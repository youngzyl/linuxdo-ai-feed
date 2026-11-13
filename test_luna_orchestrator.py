import datetime,json,os,tempfile,threading,unittest,subprocess,sys
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from cryptography.fernet import Fernet
from store import Store,source_fingerprint
from server import App,make_handler
from luna_writer import _group_id
from test_batch_offline import T0,topic,response
from test_batch_transport import Handler,server_cert,SECRET
from batch_transport import BatchTransport
from luna_orchestrator import Orchestrator,OwnerClient,ExecutorGuard
import ssl,fcntl
class DelayedHandler(Handler):
 def do_GET(self):
  if self.path=='/v1/batches/batch_testbatch' and self.server.pending_count:
   self.server.pending_count-=1;self.server.calls.append((self.path,dict(self.headers),b''));self.response(200,{'id':'batch_testbatch','status':'in_progress','output_file_id':None,'error_file_id':None});return
  super().do_GET()
class EndToEnd(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name);self.s=Store(self.p/'s.json',self.p/'f.jsonl');self.s.upsert_topics([topic(i,T0-i) for i in range(1,21)]+[topic(101,T0-86400),topic(102,T0),topic(103,T0-86401)])
  self.app=App({'luna_enabled':True,'canary_enabled':False},self.s,None,lambda x:None);os.environ['LINUXDO_AI_OWNER_TOKEN']='owner-synthetic'
  self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.app));self.th=threading.Thread(target=self.http.serve_forever,daemon=True);self.th.start()
  cert,key=server_cert(self.p);self.provider=ThreadingHTTPServer(('127.0.0.1',0),DelayedHandler);self.provider.pending_count=0;self.provider.calls=[];self.provider.mode='ok';self.provider.errors='';self.provider.realtime=response('rt',[{'id':i} for i in range(1,9)])['response']['body']
  groups=[]
  for ids in (range(9,17),[17,18,19,20,101]):
   members=[{'id':i,'source_version':source_fingerprint(self.s.get(i))} for i in ids];groups.append((_group_id('run1',members),members))
  self.provider.output=''.join(json.dumps(response(g,m))+'\n' for g,m in reversed(groups))
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);self.provider.socket=ctx.wrap_socket(self.provider.socket,server_side=True);self.pt=threading.Thread(target=self.provider.serve_forever,daemon=True);self.pt.start()
  self.transport=BatchTransport.for_loopback(self.provider.server_address[1],str(cert),lambda:SECRET,timeout=4,max_download=1000000)
  self.client=OwnerClient(self.http.server_port,lambda:'owner-synthetic');self.service=self.p/'service';self.service.mkdir(mode=0o700);self.guard=ExecutorGuard.for_test(self.service);self.work=self.service/'luna-runs'/'run1'
 def tearDown(self):
  self.http.shutdown();self.http.server_close();self.provider.shutdown();self.provider.server_close();self.th.join();self.pt.join();os.environ.pop('LINUXDO_AI_OWNER_TOKEN',None);self.temp.cleanup()
 def instance(self):return Orchestrator(self.work,'run1',T0,self.client,self.transport,guard=self.guard)
 def test_end_to_end_resume_no_resubmit_and_cutoff(self):
  a=self.instance();self.assertEqual(a.advance()['state'],'submitted');calls=[v[0] for v in self.provider.calls]
  self.assertEqual(calls.count('/v1/responses'),1);self.assertEqual(calls.count('/v1/files'),1);self.assertEqual(calls.count('/v1/batches'),1)
  self.assertEqual(self.instance().advance()['state'],'complete');self.assertEqual(self.instance().advance()['state'],'complete')
  calls=[v[0] for v in self.provider.calls];self.assertEqual(calls.count('/v1/responses'),1);self.assertEqual(calls.count('/v1/files'),1);self.assertEqual(calls.count('/v1/batches'),1)
  for i in list(range(1,21))+[101]:self.assertEqual(self.s.get(i)['filter']['model'],'gpt-6-luna')
  self.assertIsNone(self.s.get(102).get('filter'));self.assertIsNone(self.s.get(103).get('filter'))
  self.assertEqual((self.work/'encryption.key').stat().st_mode & 0o777,0o600)
  for name in ('snapshot.enc','realtime.enc','input.enc','output.enc','ledger.sqlite'):
   p=self.work/name;self.assertEqual(p.stat().st_mode&0o777,0o600);self.assertNotIn(SECRET.encode(),p.read_bytes());self.assertNotIn(b'synthetic text',p.read_bytes())
 def test_short_and_empty_realtime_selection(self):
  self.s.data['topics']={str(i):self.s.data['topics'][str(i)] for i in range(1,4)}
  self.provider.realtime=response('rt',[{'id':i} for i in range(1,4)])['response']['body']
  self.assertEqual(self.instance().advance()['state'],'complete');self.assertEqual([x[0] for x in self.provider.calls],['/v1/responses'])
 def test_empty_selection_no_paid_request(self):
  self.s.data['topics'].clear();self.assertEqual(self.instance().advance()['state'],'complete');self.assertEqual(self.provider.calls,[])
 def test_no_batch_groups_completes_after_realtime_only(self):
  self.s.data['topics']={str(i):self.s.data['topics'][str(i)] for i in range(1,9)}
  result=self.instance().advance();self.assertEqual(result['state'],'complete');self.assertEqual(result['batch_groups'],0)
  self.assertEqual([x[0] for x in self.provider.calls],['/v1/responses'])
 def test_new_process_resume(self):
  self.assertEqual(self.instance().advance()['state'],'submitted')
  child=self.p/'resume_child.py'
  child.write_text("import sys,json\nfrom pathlib import Path\nfrom luna_orchestrator import Orchestrator,OwnerClient,ExecutorGuard\nfrom batch_transport import BatchTransport\nfrom test_batch_transport import SECRET\na=OwnerClient(int(sys.argv[2]),lambda:'owner-synthetic');b=BatchTransport.for_loopback(int(sys.argv[3]),sys.argv[4],lambda:SECRET,timeout=4,max_download=1000000)\nprint(json.dumps(Orchestrator(sys.argv[1],'run1',int(sys.argv[5]),a,b,guard=ExecutorGuard.for_test(Path(sys.argv[1]).parent.parent)).advance()))\n")
  env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).parent)
  r=subprocess.run([sys.executable,str(child),str(self.work),str(self.http.server_port),str(self.provider.server_port),str(self.p/'cert.pem'),str(T0)],capture_output=True,text=True,env=env,timeout=30)
  self.assertEqual(r.returncode,0);self.assertEqual(json.loads(r.stdout)['state'],'complete');self.assertNotIn(SECRET,r.stdout+r.stderr)
  for path in ('/v1/responses','/v1/files','/v1/batches'):self.assertEqual(sum(x[0]==path for x in self.provider.calls),1)
 def test_pending_then_resume_without_snapshot_or_paid_resend(self):
  self.provider.pending_count=1;calls=[];orig=self.client.snapshot
  def count(*a):calls.append(1);return orig(*a)
  self.client.snapshot=count
  self.assertEqual(self.instance().advance()['state'],'submitted');self.assertEqual(self.instance().advance()['state'],'in_progress');self.assertEqual(self.instance().advance()['state'],'complete')
  self.assertEqual(len(calls),1)
  for path in ('/v1/responses','/v1/files','/v1/batches'):self.assertEqual(sum(x[0]==path for x in self.provider.calls),1)
 def test_same_run_alternate_workdir_and_immutable_t0_refused(self):
  a=self.instance();self.assertEqual(a.advance()['state'],'submitted');count=len(self.provider.calls)
  with self.assertRaises(Exception):Orchestrator(self.service/'luna-runs'/'alternate','run1',T0,self.client,self.transport,guard=self.guard)
  with self.assertRaises(Exception):Orchestrator(self.work,'run1',T0+1,self.client,self.transport,guard=self.guard)
  self.assertEqual(len(self.provider.calls),count)
 def test_missing_ledger_cannot_be_recreated_or_resubmitted(self):
  self.assertEqual(self.instance().advance()['state'],'submitted');count=len(self.provider.calls)
  (self.work/'ledger.sqlite').unlink()
  with self.assertRaises(Exception):self.instance()
  self.assertEqual(len(self.provider.calls),count)
 def test_truncated_ledger_same_inode_is_not_reinitialized(self):
  self.assertEqual(self.instance().advance()['state'],'submitted');count=len(self.provider.calls)
  with (self.work/'ledger.sqlite').open('wb') as f:f.truncate(0)
  with self.assertRaises(Exception):self.instance()
  self.assertEqual(len(self.provider.calls),count)
 def test_valid_old_ledger_restored_in_place_cannot_resend_upload(self):
  import luna_orchestrator as mod
  original=mod.execute_upload;saved=[]
  def capture(ledger,run,key,transport):
   saved.append((self.work/'ledger.sqlite').read_bytes());return original(ledger,run,key,transport)
  with patch.object(mod,'execute_upload',side_effect=capture):self.assertEqual(self.instance().advance()['state'],'submitted')
  count=len(self.provider.calls)
  with (self.work/'ledger.sqlite').open('wb') as f:f.write(saved[0])
  with self.assertRaises(Exception):self.instance().advance()
  self.assertEqual(len(self.provider.calls),count)
 def test_missing_whole_workdir_cannot_recreate_same_run(self):
  import shutil
  self.assertEqual(self.instance().advance()['state'],'submitted');count=len(self.provider.calls);shutil.rmtree(self.work)
  with self.assertRaises(Exception):self.instance()
  self.assertEqual(len(self.provider.calls),count)
 def test_lost_encryption_key_fails_closed_without_network(self):
  self.assertEqual(self.instance().advance()['state'],'submitted');(self.work/'encryption.key').unlink();n=len(self.provider.calls)
  with self.assertRaises(Exception):self.instance()
  self.assertEqual(len(self.provider.calls),n)
 def test_legacy_canary_enabled_blocks_before_paid_call(self):
  self.app.cfg['canary_enabled']=True
  with self.assertRaises(Exception):self.instance().advance()
  self.assertEqual(self.provider.calls,[])
 def test_private_credentials_and_symlink_are_refused_without_leak(self):
  from luna_orchestrator import credential_file
  p=self.p/'credential';p.write_text(SECRET);p.chmod(0o644)
  with self.assertRaises(Exception) as e:credential_file(p)()
  self.assertNotIn(SECRET,str(e.exception));p.chmod(0o600);self.assertEqual(credential_file(p)(),SECRET)
  link=self.p/'link';link.symlink_to(p)
  with self.assertRaises(Exception):credential_file(link)()
 def test_local_lock_blocks_concurrent_advance(self):
  obj=self.instance();fd=os.open(self.work/'lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:
   with self.assertRaises(Exception):obj.advance()
  finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
  self.assertEqual(self.provider.calls,[])
 def test_lost_realtime_never_retries_after_restart(self):
  self.provider.mode='drop'
  with self.assertRaises(Exception):self.instance().advance()
  self.provider.mode='ok';n=sum(v[0]=='/v1/responses' for v in self.provider.calls)
  with self.assertRaises(Exception):self.instance().advance()
  self.assertEqual(sum(v[0]=='/v1/responses' for v in self.provider.calls),n)
 def test_lost_upload_never_retries_after_restart(self):
  orig=self.transport.upload_file
  def lost(payload):
   self.provider.mode='drop'
   try:return orig(payload)
   finally:self.provider.mode='ok'
  self.transport.upload_file=lost
  with self.assertRaises(Exception):self.instance().advance()
  n=sum(v[0]=='/v1/files' for v in self.provider.calls)
  with self.assertRaises(Exception):self.instance().advance()
  self.assertEqual(sum(v[0]=='/v1/files' for v in self.provider.calls),n)
 def test_lost_submit_never_retries_after_restart(self):
  orig=self.transport.create_batch
  def lost(fid):
   self.provider.mode='drop'
   try:return orig(fid)
   finally:self.provider.mode='ok'
  self.transport.create_batch=lost
  with self.assertRaises(Exception):self.instance().advance()
  n=sum(v[0]=='/v1/batches' for v in self.provider.calls)
  with self.assertRaises(Exception):self.instance().advance()
  self.assertEqual(sum(v[0]=='/v1/batches' for v in self.provider.calls),n)
 def test_owner_apply_response_loss_receipt_replay(self):
  original=self.client.apply;seen=[]
  def lost_once(payload):
   result=original(payload)
   if not seen:seen.append(True);raise OSError('synthetic response lost')
   return result
  self.client.apply=lost_once
  with self.assertRaises(Exception):self.instance().advance()
  self.client.apply=original;self.assertEqual(self.instance().advance()['state'],'submitted');self.assertEqual(self.instance().advance()['state'],'complete')
  self.assertEqual(sum(v[0]=='/v1/responses' for v in self.provider.calls),1)
 def test_committed_taste_is_used_and_encrypted(self):
  import learn
  learn.record(self.s,103,'keep','synthetic-taste-marker')
  self.assertEqual(self.instance().advance()['state'],'submitted')
  body=json.loads(next(x[2] for x in self.provider.calls if x[0]=='/v1/responses'))
  self.assertIn('synthetic-taste-marker',body['input'][0]['content'])
  self.assertEqual(self.instance().advance()['state'],'complete')
  for name in ('snapshot.enc','realtime.enc','input.enc','output.enc'):
   self.assertNotIn(b'synthetic-taste-marker',(self.work/name).read_bytes())
 def test_stale_batch_does_not_apply_or_resubmit(self):
  self.assertEqual(self.instance().advance()['state'],'submitted');self.s.set_fields(9,body_text='changed source')
  result=self.instance().advance();self.assertEqual(result['state'],'reconciled');self.assertEqual(result['stale'],[9])
  n=len([x for x in self.provider.calls if x[0] in ('/v1/responses','/v1/files','/v1/batches')])
  self.assertEqual(self.instance().advance()['state'],'reconciled')
  self.assertEqual(len([x for x in self.provider.calls if x[0] in ('/v1/responses','/v1/files','/v1/batches')]),n)
  self.assertIsNone(self.s.get(9).get('filter'))
 def test_partial_only_complete_groups_applied(self):
  rows=self.provider.output.splitlines();failed=json.loads(rows.pop());self.provider.output='\n'.join(rows)+'\n';self.provider.errors=json.dumps({'custom_id':failed['custom_id'],'error':{'code':'expired'}})+'\n'
  self.assertEqual(self.instance().advance()['state'],'submitted');result=self.instance().advance();self.assertEqual(result['state'],'partial');self.assertEqual(self.instance().advance()['state'],'partial')
  self.assertEqual(self.s.data['luna']['status'],'active');n=len(self.provider.calls)
  self.assertEqual(self.instance().finish_partial()['state'],'partial_cancelled');self.assertEqual(self.s.data['luna']['status'],'cancelled')
  self.assertEqual(self.instance().advance()['state'],'partial_cancelled');self.assertEqual(len(self.provider.calls),n)
  next_run=self.client.snapshot('new_run',T0);self.assertEqual(next_run['run_id'],'new_run')
 def test_unprocessable_download_explicit_finish_without_resend(self):
  self.provider.output='{invalid-json}\n';self.provider.errors=''
  self.assertEqual(self.instance().advance()['state'],'submitted')
  with self.assertRaises(Exception):self.instance().advance()
  n=len(self.provider.calls);self.assertEqual(self.instance().finish_partial()['state'],'partial_cancelled')
  self.assertEqual(self.s.data['luna']['status'],'cancelled');self.assertEqual(self.instance().advance()['state'],'partial_cancelled');self.assertEqual(len(self.provider.calls),n)
 def test_partial_finish_lost_reply_requires_explicit_retry(self):
  rows=self.provider.output.splitlines();failed=json.loads(rows.pop());self.provider.output='\n'.join(rows)+'\n';self.provider.errors=json.dumps({'custom_id':failed['custom_id'],'error':{'code':'expired'}})+'\n'
  self.assertEqual(self.instance().advance()['state'],'submitted');self.assertEqual(self.instance().advance()['state'],'partial');original=self.client.finish;seen=[]
  def lost_once(*args):
   result=original(*args)
   if not seen:seen.append(True);raise OSError('synthetic lost reply')
   return result
  self.client.finish=lost_once;n=len(self.provider.calls)
  with self.assertRaises(Exception):self.instance().finish_partial()
  with self.assertRaises(Exception):self.instance().advance()
  self.client.finish=original;self.assertEqual(self.instance().finish_partial()['state'],'partial_cancelled');self.assertEqual(len(self.provider.calls),n)
if __name__=='__main__':unittest.main()
