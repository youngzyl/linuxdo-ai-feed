import contextlib,datetime,io,json,os,stat,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from cryptography.fernet import Fernet
from store import Store,source_fingerprint
from batch_offline import Ledger,prepare,load_payload,reconcile,check_apply,quote,OfflineError,OfflineMockAPI,mock_upload,mock_submit,mock_download,reconcile_download,cost_report

T0=1763000000
def iso(t):return datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat()
def topic(i,t):return {'id':i,'title':f'Test {i}','body_text':'synthetic text','created_at':iso(t),'filter':None}
def response(i, items):
 rows=[{'i':j,'valuable':True,'score':80,'category':'AI','reason':'ok','summary':'ok'} for j in range(1,len(items)+1)]
 body={'status':'completed','model':'gpt-6-luna','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'results':rows})}]}],'usage':{'input_tokens':100,'output_tokens':50,'input_tokens_details':{'cached_tokens':10},'output_tokens_details':{'reasoning_tokens':20}}}
 return {'custom_id':i,'response':{'status_code':200,'body':body},'error':None}
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name);self.key=Fernet.generate_key();self.store=Store(self.p/'s.json',self.p/'f.jsonl')
  self.topics=[topic(i,T0-i) for i in range(1,21)]+[topic(101,T0-86400),topic(102,T0),topic(103,T0-86401)]
  self.store.upsert_topics(self.topics);self.ledger=Ledger(self.p/'ledger.sqlite')
 def tearDown(self):self.tmp.cleanup()
 def prepared(self):return prepare(self.ledger,'run1',T0,self.store,'',self.p/'payload.enc',self.key)
 def test_selection_boundary_group_jsonl_encrypted(self):
  info=self.prepared();self.assertEqual(info['realtime'],list(range(1,9)))
  members=self.ledger.members('run1');ids=[x['id'] for g in members.values() for x in g]
  self.assertEqual(set(ids),set(range(9,21))|{101});self.assertEqual(len(ids),len(set(ids)))
  raw=(self.p/'payload.enc').read_bytes();self.assertNotIn(b'synthetic text',raw);self.assertEqual(stat.S_IMODE((self.p/'payload.enc').stat().st_mode),0o600)
  reqs=[json.loads(x) for x in load_payload(self.ledger,'run1',self.key).splitlines()]
  self.assertEqual(len(reqs),2);self.assertEqual(len({x['custom_id'] for x in reqs}),2)
  for x in reqs:self.assertEqual((x['method'],x['url'],x['body']['model'],x['body']['reasoning']['effort'],x['body']['store']),('POST','/v1/responses','gpt-6-luna','high',False))
 def test_duplicate_and_timestamp_rejected(self):
  with self.assertRaises(OfflineError):prepare(self.ledger,'bad',T0,self.store,'',self.p/'a',self.key,topics=self.topics+[self.topics[0]])
  bad=topic(500,T0);bad['created_at']='bad'
  with self.assertRaises(OfflineError):prepare(self.ledger,'bad2',T0,self.store,'',self.p/'b',self.key,topics=[bad])
 def test_unknown_upload_submit_no_resend(self):
  self.prepared();self.ledger.begin_upload('run1')
  with self.assertRaises(OfflineError):self.ledger.begin_upload('run1')
  with self.assertRaises(OfflineError):self.ledger.transition('run1',{'upload_unknown'},'prepared')
  self.ledger.upload_confirmed('run1','file-123');self.ledger.begin_submit('run1')
  with self.assertRaises(OfflineError):self.ledger.begin_submit('run1')
  self.ledger.submit_confirmed('run1','batch-123');self.ledger.poll('run1','in_progress');self.ledger.poll('run1','completed','file-out','file-err')
  self.assertEqual(self.ledger.status('run1'),'completed')
 def test_join_partial_duplicate_invalid_and_stale(self):
  self.prepared();groups=self.ledger.members('run1');results=[response(k,v) for k,v in groups.items()]
  plan=reconcile(groups,[json.dumps(x) for x in reversed(results)],[]);self.assertTrue(plan['complete']);check_apply(self.store,groups,plan)
  self.store.data['topics']['9']['body_text']='changed'
  with self.assertRaises(OfflineError):check_apply(self.store,groups,plan)
  for output in ([json.dumps(results[0])],[json.dumps(results[0]),json.dumps(results[0])],['{bad']):
   with self.assertRaises(OfflineError):reconcile(groups,output,[])
  partial=reconcile(groups,[json.dumps(results[0])],[json.dumps({'custom_id':results[1]['custom_id'],'error':{'code':'expired'}})])
  self.assertFalse(partial['complete'])
  with self.assertRaises(OfflineError):check_apply(self.store,groups,partial)
 def test_usage_pricing_counts_reasoning_once(self):
  q=quote({'input_tokens':1000000,'output_tokens':1000000,'cached_tokens':100000,'cache_write_tokens':200000,'reasoning_tokens':500000}, {'uncached':Decimal('1'),'cache_read':Decimal('.2'),'cache_write':Decimal('2'),'output':Decimal('4')})
  self.assertEqual(q,Decimal('5.12'))
  with self.assertRaises(OfflineError):quote({'input_tokens':1,'output_tokens':1,'cached_tokens':2,'cache_write_tokens':0,'reasoning_tokens':0},{k:Decimal('1') for k in ['uncached','cache_read','cache_write','output']})
 def test_invalid_handle_does_not_log_secret(self):
  self.prepared();self.ledger.begin_upload('run1');buf=io.StringIO()
  with contextlib.redirect_stdout(buf),contextlib.redirect_stderr(buf):
   with self.assertRaises(OfflineError):self.ledger.upload_confirmed('run1','sk-secret-value')
  self.assertNotIn('sk-secret-value',buf.getvalue());self.assertNotIn(b'sk-secret-value',(self.p/'ledger.sqlite').read_bytes())
 def test_mock_lifecycle_and_restart_persistence(self):
  self.prepared();groups=self.ledger.members('run1');values=[response(k,v) for k,v in groups.items()]
  mock=OfflineMockAPI({'file-out':[json.dumps(x) for x in values]})
  mock_upload(self.ledger,'run1',self.key,mock);mock_submit(self.ledger,'run1',mock)
  self.ledger.poll('run1','validating');self.ledger.poll('run1','completed','file-out')
  mock_download(self.ledger,'run1',self.key,mock,self.p/'result.enc')
  self.assertNotIn(b'synthetic',(self.p/'result.enc').read_bytes());self.assertEqual(stat.S_IMODE((self.p/'result.enc').stat().st_mode),0o600)
  reopened=Ledger(self.p/'ledger.sqlite');plan=reconcile_download(reopened,'run1',self.key)
  self.assertTrue(plan['complete']);self.assertEqual(reopened.status('run1'),'reconciled');self.assertTrue(check_apply(self.store,groups,plan))
  with reopened.db() as db:
   with self.assertRaises(Exception):db.execute('DELETE FROM events')
   self.assertEqual(db.execute('SELECT count(*) FROM reserved').fetchone()[0],8)
 def test_mock_upload_and_submit_unknown_do_not_resend(self):
  self.prepared();mock=OfflineMockAPI(fail_upload=True)
  with self.assertRaises(OfflineError):mock_upload(self.ledger,'run1',self.key,mock)
  reopened=Ledger(self.p/'ledger.sqlite')
  with self.assertRaises(OfflineError):mock_upload(reopened,'run1',self.key,mock)
  self.assertEqual(mock.calls,['upload']);self.assertEqual(reopened.status('run1'),'upload_unknown')
 def test_mock_partial_expired(self):
  self.prepared();groups=self.ledger.members('run1');a,b=list(groups)
  mock=OfflineMockAPI({'file-out':[json.dumps(response(a,groups[a]))],'file-err':[json.dumps({'custom_id':b,'error':{'code':'expired'}})]})
  mock_upload(self.ledger,'run1',self.key,mock);mock_submit(self.ledger,'run1',mock);self.ledger.poll('run1','expired','file-out','file-err')
  mock_download(self.ledger,'run1',self.key,mock,self.p/'result.enc');plan=reconcile_download(self.ledger,'run1',self.key)
  self.assertEqual(self.ledger.status('run1'),'partial');self.assertFalse(plan['complete'])
 def test_mock_submit_timeout_and_http_error_file(self):
  self.prepared();mock=OfflineMockAPI(fail_submit=True);mock_upload(self.ledger,'run1',self.key,mock)
  with self.assertRaises(OfflineError):mock_submit(self.ledger,'run1',mock)
  with self.assertRaises(OfflineError):mock_submit(Ledger(self.p/'ledger.sqlite'),'run1',mock)
  self.assertEqual(mock.calls,['upload','submit']);self.assertEqual(self.ledger.status('run1'),'submit_unknown')
  groups=self.ledger.members('run1');a,b=list(groups)
  plan=reconcile(groups,[json.dumps(response(a,groups[a]))],[json.dumps({'custom_id':b,'response':{'status_code':429},'error':None})])
  self.assertFalse(plan['complete'])
 def test_tamper_wrong_key_and_invalid_result(self):
  self.prepared();groups=self.ledger.members('run1');a,b=list(groups)
  with self.assertRaises(OfflineError):load_payload(self.ledger,'run1',Fernet.generate_key())
  broken=response(a,groups[a]);broken['response']['body']['output'][0]['content'][0]['text']='not JSON'
  with self.assertRaises(OfflineError):reconcile(groups,[json.dumps(broken),json.dumps(response(b,groups[b]))],[])
  duplicate=[json.dumps(response(a,groups[a])),json.dumps(response(b,groups[b]))]
  with self.assertRaises(OfflineError):reconcile(groups,duplicate,[json.dumps({'custom_id':a,'error':{'code':'expired'}})])
 def test_cost_report_separates_channels_and_unknown_cache_writes(self):
  rates={k:Decimal('1') for k in ['uncached','cache_read','cache_write','output']}
  u={'input_tokens':10,'output_tokens':5,'cached_tokens':0,'cache_write_tokens':None,'reasoning_tokens':2}
  report=cost_report([u],[u],rates,rates)
  self.assertIsNone(report['realtime']['estimate']);self.assertIsNone(report['batch']['estimate'])
  u['cache_write_tokens']=0;report=cost_report([u],[u],rates,rates)
  self.assertEqual(report['batch']['estimate'],Decimal('0.000015'));self.assertEqual(report['realtime']['estimate'],Decimal('0.000015'))
if __name__=='__main__':unittest.main()
