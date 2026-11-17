import hashlib,json,tempfile,unittest,datetime as dt
from pathlib import Path
from types import SimpleNamespace
from receipts import ReceiptStore
from responses_adapter import _complete_resource_error
from daily import attempt_resolved

class LedgerFixTests(unittest.TestCase):
 def exc(self,body,consumed=True,length=None,status=429):
  if length is None:length=len(body)
  r=SimpleNamespace(status_code=status,is_stream_consumed=consumed,content=body,headers={'content-length':str(length)})
  return SimpleNamespace(status_code=status,response=r)
 def test_complete_resource_proof(self):
  raw=b'{"error":{"code":"flex_unavailable","type":"resource_unavailable","message":"private-key"}}'
  proof=_complete_resource_error(self.exc(raw));self.assertTrue(proof['body_read_complete']);self.assertEqual(proof['provider_code'],'flex_unavailable');self.assertNotIn('private-key',repr(proof));self.assertTrue(attempt_resolved({'outcome':'capacity_deferred','diagnostic':dict(proof,http_status=429)}))
  for exc in (self.exc(raw,False),self.exc(raw,length=len(raw)+1),self.exc(raw,status=500),self.exc(b'{"error":{"code":"flex_unavailable","type":"insufficient_quota"}}'),self.exc(b'{"error":{"code":"flex_unavailable","code":"x"}}'),self.exc(b'bad')):
   self.assertIsNone(_complete_resource_error(exc))
 def test_real_sdk_429_cached_response(self):
  try:
   import httpx2,openai
  except ImportError:self.skipTest('SDK unavailable')
  raw=b'{"error":{"code":"flex_unavailable","type":"resource_unavailable"}}'
  req=httpx2.Request('POST','https://api.openai.com/v1/responses')
  response=httpx2.Response(429,content=raw,headers={'content-length':str(len(raw))},request=req)
  error=openai.RateLimitError('safe synthetic',response=response,body={'error':{'code':'flex_unavailable','type':'resource_unavailable'}})
  proof=_complete_resource_error(error)
  self.assertEqual(proof['provider_code'],'flex_unavailable');self.assertEqual(proof['provider_type'],'resource_unavailable')
  import asyncio
  from responses_adapter import ResponsesAdapter
  with tempfile.TemporaryDirectory() as root:
   with ReceiptStore(root).singleton() as store:
    _,run=store.admit()
    class Client:
     max_retries=0
     def __init__(self):self.responses=self;self.calls=0
     async def create(self,**kwargs):self.calls+=1;raise error
    client=Client();adapter=ResponsesAdapter(store,run['run_id'],client=client)
    with self.assertRaises(RuntimeError):asyncio.run(adapter.generate('independent'))
    _,receipt=store._load_active(run['run_id']);attempt=receipt['attempts'][0]
    self.assertEqual(attempt['outcome'],'capacity_deferred');self.assertTrue(attempt_resolved(attempt))
    with self.assertRaises(RuntimeError):asyncio.run(adapter.generate('different'))
    self.assertEqual(client.calls,1)

 def test_owner_cas_preserves_original_snapshot_and_utc_admission(self):
  with tempfile.TemporaryDirectory() as root:
   day='2026-11-17';p=Path(root)/(day+'.json');original={'run_id':day,'attempts':[{'number':1,'outcome':'unknown','disposition':'abandoned_no_retry'},{'number':2,'outcome':'unknown','recovery_nonce':'a'*64}]};p.write_text(json.dumps(original));raw=p.read_bytes();sha=hashlib.sha256(raw).hexdigest()
   with ReceiptStore(root).singleton() as s:
    with self.assertRaises(ValueError):s.owner_abandon_all(day,'0'*64)
    self.assertEqual(p.read_bytes(),raw)
    result=s.owner_abandon_all(day,sha)
    self.assertEqual(result['attempts'][0],original['attempts'][0]);self.assertEqual(result['attempts'][1]['outcome'],'unknown');self.assertEqual(result['attempts'][1]['disposition'],'abandoned_no_retry')
    self.assertEqual((Path(root)/('owner-snapshot-'+sha+'.json')).read_bytes(),raw)
    self.assertTrue(attempt_resolved(result['attempts'][1]))
    self.assertFalse(s.admit(dt.datetime(2026,11,17,23,59,tzinfo=dt.timezone.utc))[0])
    yes,new=s.admit(dt.datetime(2026,11,18,tzinfo=dt.timezone.utc));self.assertTrue(yes);self.assertEqual(len(new['run_nonce']),64)

if __name__=='__main__':unittest.main()
