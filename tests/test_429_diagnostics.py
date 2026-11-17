import hashlib
import io
import http.client
import json
import unittest
from unittest.mock import patch

from future_flex import request, FlexError, FutureFlex
from test_future_flex import Tests as FlexTests
from store import Store

class Response:
 def __init__(self, body, declared=None, fail=False):
  self.status=429; self.body=body; self.length=len(body) if declared is None else declared; self.fail=fail; self.closed=False
 def read(self, limit):
  if self.fail: raise OSError('private-read-error')
  data=self.body[:limit]; self.length-=len(data); self.closed=True
  return data
 def isclosed(self): return self.closed

class Connection:
 def __init__(self, response): self.response=response; self.calls=0; self.closed=False
 def request(self, *args, **kwargs): self.calls+=1
 def getresponse(self): return self.response
 def close(self): self.closed=True

def wire(response):
 c=Connection(response)
 with patch('future_flex.http.client.HTTPSConnection',return_value=c):
  try: request([{'id':1}],'','private-key')
  except FlexError as e: return e,c
 raise AssertionError('expected FlexError')

class WireTests(unittest.TestCase):
 def test_real_http_response_framing(self):
  body=b'{"error":{"code":"resource_exhausted"}}'
  class Socket:
   def __init__(self, raw): self.raw=raw
   def makefile(self, *args): return io.BytesIO(self.raw)
  for headers,payload,expected in [(b'Content-Length: '+str(len(body)).encode()+b'\r\n',body,'rate_limited'),(b'Content-Length: '+str(len(body)+3).encode()+b'\r\n',body,'outcome_unknown'),(b'Transfer-Encoding: chunked\r\n',b'%x\r\n'%len(body)+body+b'\r\n0\r\n\r\n','rate_limited'),(b'Transfer-Encoding: chunked\r\n',b'%x\r\n'%len(body)+body+b'\r\n','outcome_unknown')]:
   with self.subTest(headers=headers,payload=payload):
    response=http.client.HTTPResponse(Socket(b'HTTP/1.1 429 Too Many Requests\r\n'+headers+b'\r\n'+payload));response.begin()
    e,c=wire(response);self.assertEqual(e.code,expected);self.assertEqual(e.diagnostic['body_read_complete'],expected=='rate_limited')
 def test_complete(self):
  body=b'{"error":{"code":"resource_exhausted","type":"valid.type-1","message":"private-message"}}'
  e,c=wire(Response(body)); self.assertEqual(e.code,'rate_limited')
  self.assertEqual(e.diagnostic,{'body_read_complete':True,'body_length':len(body),'body_sha256':hashlib.sha256(body).hexdigest(),'json_shape':'object_error_object','error_code':'resource_exhausted','error_type':'valid.type-1'})
  self.assertTrue(c.closed);self.assertEqual(c.calls,1);self.assertNotIn('private-message',str(e.diagnostic))
 def test_partial_valid_json_is_unknown(self):
  body=b'{"error":{"code":"resource_exhausted"}}'
  e,c=wire(Response(body,len(body)+4));self.assertEqual(e.code,'outcome_unknown');self.assertFalse(e.diagnostic['body_read_complete']);self.assertEqual(e.diagnostic['body_length'],len(body));self.assertNotIn('error_code',e.diagnostic)
 def test_read_failure(self):
  e,c=wire(Response(b'',fail=True));self.assertEqual(e.code,'outcome_unknown');self.assertFalse(e.diagnostic['body_read_complete']);self.assertIsNone(e.diagnostic['body_length']);self.assertIsNone(e.diagnostic['body_sha256']);self.assertNotIn('private-read-error',str(e.diagnostic))
 def test_malicious_fields_and_unrecognized(self):
  body=json.dumps({'error':{'code':'private secret', 'type':'x'*97, 'message':'private-message'},'private-key':'value'}).encode()
  e,c=wire(Response(body));self.assertEqual(e.code,'outcome_unknown');self.assertEqual(e.diagnostic['json_shape'],'object_error_object');self.assertNotIn('error_code',e.diagnostic);self.assertNotIn('error_type',e.diagnostic);self.assertNotIn('private',str(e.diagnostic))
 def test_oversized_and_duplicate(self):
  for body in (b' '*65537,b'{"error":{"code":"resource_exhausted","code":"resource_exhausted"}}'):
   e,c=wire(Response(body));self.assertEqual(e.code,'outcome_unknown');self.assertNotIn('error_code',e.diagnostic)

class PersistenceTests(FlexTests):
 def test_recognized_persists_backoff(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  body=b'{"error":{"code":"resource_exhausted"}}';c=Connection(Response(body))
  f=FutureFlex(self.cfg,self.store,lambda x:None,clock=lambda:self.now)
  with patch('future_flex.http.client.HTTPSConnection',return_value=c):
   self.assertEqual(f.run()['error'],'rate_limited')
  state=Store(self.store.path,self.store.failure_log).data['future_flex'];self.assertEqual(state['status'],'backoff');self.assertEqual(state['diagnostic']['error_code'],'resource_exhausted');self.assertEqual(c.calls,1)
 def test_owner_state_persists_without_leak_and_blocks_replay(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  body=b'{"error":{"code":"unknown_code","message":"private-message"},"key":"private-key"}'
  c=Connection(Response(body))
  f=FutureFlex(self.cfg,self.store,lambda x:None,clock=lambda:self.now)
  with patch('future_flex.http.client.HTTPSConnection',return_value=c):
   self.assertEqual(f.run()['error'],'outcome_unknown');self.assertEqual(f.run()['error'],'unknown')
  state=Store(self.store.path,self.store.failure_log).data['future_flex'];d=state['diagnostic']
  self.assertEqual(state['status'],'unknown');self.assertEqual(d['body_sha256'],hashlib.sha256(body).hexdigest());self.assertEqual(d['error_code'],'unknown_code');self.assertTrue(d['body_read_complete']);self.assertEqual(c.calls,1)
  text=json.dumps(state);self.assertNotIn('private-message',text);self.assertNotIn('private-key',text)
