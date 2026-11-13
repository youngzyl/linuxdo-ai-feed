import socket,tempfile,threading,unittest,os
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from store import Store
from server import App,make_handler
class FramingTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();p=Path(self.t.name);self.s=Store(p/'s',p/'f');self.app=App({'luna_enabled':True},self.s,None,lambda _:None);self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.app));self.th=threading.Thread(target=self.http.serve_forever,daemon=True);self.th.start();self.env=patch.dict(os.environ,{'LINUXDO_AI_OWNER_TOKEN':'dummy'});self.env.start()
 def tearDown(self):self.env.stop();self.http.shutdown();self.http.server_close();self.th.join();self.t.cleanup()
 def req(self,raw):
  c=socket.create_connection(('127.0.0.1',self.http.server_port),timeout=3);c.settimeout(3);c.sendall(raw);out=b''
  while True:
   try:b=c.recv(65536)
   except (ConnectionResetError,TimeoutError):break
   if not b:break
   out+=b
  c.close();return out
 def test_auth_failure_closes_with_unread_request_body(self):
  x=self.req(b'POST /api/luna/snapshot HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer wrong\r\nContent-Length: 4\r\n\r\nABCDGET /health HTTP/1.1\r\nHost: local\r\n\r\n')
  self.assertEqual(x.count(b'HTTP/1.'),1);self.assertIn(b' 401 ',x)
 def test_duplicate_length_and_transfer_encoding_rejected(self):
  for headers in (b'Content-Length: 0\r\nContent-Length: 0\r\n',b'Transfer-Encoding: chunked\r\nContent-Length: 0\r\n',b'Transfer-Encoding: chunked\r\n',b'Content-Length: 0, 0\r\n'):
   with self.subTest(headers=headers):
    x=self.req(b'POST /api/luna/snapshot HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer dummy\r\n'+headers+b'\r\n')
    self.assertIn(b' 400 ',x)
if __name__=='__main__':unittest.main()
