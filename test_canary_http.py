import http.client, json, os, tempfile, threading, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from store import Store
from server import App, make_handler

class HTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); p=Path(self.tmp.name)
  self.store=Store(p/'state.json',p/'failures.jsonl')
  self.app=App({},self.store,None,lambda *a:None)
  self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.app))
  self.th=threading.Thread(target=self.http.serve_forever,daemon=True); self.th.start()
  self.env=patch.dict(os.environ,{'LINUXDO_AI_OWNER_TOKEN':'test-owner'},clear=False);self.env.start()
 def tearDown(self):
  self.env.stop();self.http.shutdown();self.http.server_close();self.th.join();self.tmp.cleanup()
 def req(self,method,path,token=None,origin=None):
  c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=3);h={'Content-Type':'application/json'}
  if token: h['Authorization']='Bearer '+token
  if origin:h['Origin']=origin
  c.request(method,path,body=json.dumps({'run_id':'run_001'}),headers=h);r=c.getresponse();r.read();c.close();return r.status
 def test_disabled_default(self): self.assertEqual(self.req('POST','/api/canary/snapshot','test-owner'),404)
 def test_auth_method_origin(self):
  self.app.cfg['canary_enabled']=True
  self.assertEqual(self.req('POST','/api/canary/snapshot'),401)
  self.assertEqual(self.req('POST','/api/canary/snapshot','wrong'),401)
  self.assertEqual(self.req('POST','/api/canary/snapshot','test-owner','https://untrusted.example'),403)
  self.assertEqual(self.req('GET','/api/canary/snapshot','test-owner'),404)
  self.assertEqual(self.req('POST','/api/canary/snapshot','test-owner'),409)
 def test_nonpost_methods_never_dispatch(self):
  self.app.cfg['canary_enabled']=True
  for method in ('GET','HEAD','OPTIONS','PUT','DELETE','PATCH'):
   with self.subTest(method=method):
    self.assertIn(self.req(method,'/api/canary/apply','test-owner'),(404,405,501))
 def test_oversized_post_rejected(self):
  self.app.cfg['canary_enabled']=True
  import socket
  with socket.create_connection(('127.0.0.1',self.http.server_port),timeout=3) as c:
   c.sendall(b'POST /api/canary/apply HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-owner\r\nContent-Type: application/json\r\nContent-Length: 65537\r\n\r\n')
   r=http.client.HTTPResponse(c);r.begin();r.read();self.assertEqual(r.status,413)
if __name__=='__main__':unittest.main()
