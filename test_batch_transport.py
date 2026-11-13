import contextlib,datetime,io,ipaddress,json,os,socket,ssl,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from cryptography.fernet import Fernet
from batch_offline import Ledger,prepare,reconcile_download
from store import Store
from test_batch_offline import T0,topic,response
from batch_transport import BatchTransport,TransportError,execute_upload,execute_submit,poll_batch,download_results,run_realtime

SECRET='sk-local-synthetic-secret'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def response(self,n,body,kind='application/json',extra=None):
  data=body if isinstance(body,bytes) else json.dumps(body).encode();self.send_response(n);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
  for k,v in (extra or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(data)
 def do_POST(self):
  length=int(self.headers.get('Content-Length','0'));b=self.rfile.read(length);self.server.calls.append((self.path,dict(self.headers),b))
  mode=self.server.mode
  if mode=='redirect':self.response(307,{},extra={'Location':'/redirected'});return
  if mode=='unauthorized':self.response(401,{'error':SECRET});return
  if mode=='drop':self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  if self.path=='/v1/files':self.response(200,{'id':'file-testupload'});return
  if self.path=='/v1/batches':self.response(200,{'id':'batch_testbatch'});return
  if self.path=='/v1/responses':
   if getattr(self.server,'on_realtime',None):self.server.on_realtime()
   self.response(200,self.server.realtime);return
  self.response(404,{})
 def do_GET(self):
  self.server.calls.append((self.path,dict(self.headers),b''))
  if self.server.mode=='redirect':self.response(302,{},extra={'Location':'/redirected'});return
  if self.path=='/v1/batches/batch_testbatch':self.response(200,{'id':'batch_testbatch','status':'completed','output_file_id':'file-testout','error_file_id':'file-testerr'});return
  if self.path=='/v1/files/file-testout/content':self.response(200,self.server.output.encode(),'application/jsonl');return
  if self.path=='/v1/files/file-testerr/content':self.response(200,self.server.errors.encode(),'application/jsonl');return
  self.response(404,{})

def server_cert(path):
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'127.0.0.1')]);now=datetime.datetime.now(datetime.timezone.utc)
 cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=2)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
 c=path/'cert.pem';k=path/'priv.pem';c.write_bytes(cert.public_bytes(serialization.Encoding.PEM));k.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()));return c,k
class TransportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name);cert,key=server_cert(self.p)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.calls=[];self.server.mode='ok';self.server.output='';self.server.errors='';self.server.realtime={}
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);self.server.socket=ctx.wrap_socket(self.server.socket,server_side=True)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.transport=BatchTransport.for_loopback(self.server.server_address[1],str(cert),lambda:SECRET,timeout=3,max_download=65536)
  self.key=Fernet.generate_key();self.store=Store(self.p/'s.json',self.p/'f.jsonl');self.store.upsert_topics([topic(i,T0-i) for i in range(1,21)]);self.ledger=Ledger(self.p/'ledger.sqlite');prepare(self.ledger,'run1',T0,self.store,'',self.p/'input.enc',self.key)
 def tearDown(self):self.server.shutdown();self.server.server_close();self.tmp.cleanup()
 def test_real_https_full_batch_lifecycle_partial_usage(self):
  groups=self.ledger.members('run1');a,b=list(groups)
  self.server.output=json.dumps(response(a,groups[a]))+'\n';self.server.errors=json.dumps({'custom_id':b,'error':{'code':'expired'}})+'\n'
  execute_upload(self.ledger,'run1',self.key,self.transport);execute_submit(self.ledger,'run1',self.transport);poll_batch(self.ledger,'run1',self.transport)
  download_results(self.ledger,'run1',self.key,self.transport,self.p/'output.enc');plan=reconcile_download(self.ledger,'run1',self.key)
  self.assertFalse(plan['complete']);self.assertEqual(plan['usage'][0]['input_tokens'],100);self.assertEqual(self.ledger.status('run1'),'partial')
  requests=self.server.calls;self.assertEqual([x[0] for x in requests],['/v1/files','/v1/batches','/v1/batches/batch_testbatch','/v1/files/file-testout/content','/v1/files/file-testerr/content'])
  self.assertIn(b'name="purpose"\r\n\r\nbatch',requests[0][2]);self.assertIn(b'name="file"; filename="batch.jsonl"',requests[0][2]);self.assertIn(b'/v1/responses',requests[0][2]);self.assertEqual(requests[0][1]['Authorization'],'Bearer '+SECRET)
  self.assertEqual(json.loads(requests[1][2]),{'input_file_id':'file-testupload','endpoint':'/v1/responses','completion_window':'24h'})
 def test_redirect_unauthorized_drop_fail_closed_and_secret_sanitized(self):
  for mode in ['redirect','unauthorized','drop']:
   with self.subTest(mode=mode):
    self.server.mode=mode;ledger=Ledger(self.p/(mode+'.db'));prepare(ledger,mode,T0,self.store,'',self.p/(mode+'.enc'),self.key);before=len(self.server.calls);buf=io.StringIO()
    with contextlib.redirect_stdout(buf),contextlib.redirect_stderr(buf):
     with self.assertRaises(TransportError) as e:execute_upload(ledger,mode,self.key,self.transport)
     self.assertNotIn(SECRET,str(e.exception));self.assertNotIn(SECRET,buf.getvalue())
    self.assertEqual(ledger.status(mode),'upload_unknown');self.assertEqual(len(self.server.calls),before+1)
    with self.assertRaises(Exception):execute_upload(ledger,mode,self.key,self.transport)
    self.assertEqual(len(self.server.calls),before+1)
 def test_realtime_request_reservation_usage_and_no_retry(self):
  self.server.realtime=response('rt',[{'id':i} for i in range(1,9)])['response']['body']
  plan=run_realtime(self.ledger,'run1',self.store,'',self.transport,self.key,self.p/'rt.enc')
  self.assertEqual(len(plan['verdicts']),8);self.assertEqual(plan['usage']['reasoning_tokens'],20)
  path,headers,raw=self.server.calls[-1];body=json.loads(raw);self.assertEqual(path,'/v1/responses');self.assertEqual(body['model'],'gpt-6-luna');self.assertEqual(body['reasoning'],{'effort':'high'});self.assertIs(body['store'],False)
  with self.assertRaises(Exception):run_realtime(self.ledger,'run1',self.store,'',self.transport,self.key,self.p/'rt2.enc')
  self.assertNotIn(b'synthetic text',(self.p/'rt.enc').read_bytes())
 def test_realtime_drop_unknown_no_retry(self):
  self.server.mode='drop'
  with self.assertRaises(TransportError):run_realtime(self.ledger,'run1',self.store,'',self.transport,self.key,self.p/'rt.enc')
  with self.assertRaises(Exception):run_realtime(self.ledger,'run1',self.store,'',self.transport,self.key,self.p/'rt2.enc')
  self.assertEqual(len(self.server.calls),1)
 def test_submit_response_loss_and_idempotent_terminal_poll(self):
  execute_upload(self.ledger,'run1',self.key,self.transport);self.server.mode='drop'
  with self.assertRaises(TransportError):execute_submit(self.ledger,'run1',self.transport)
  self.assertEqual(self.ledger.status('run1'),'submit_unknown');n=len(self.server.calls)
  with self.assertRaises(Exception):execute_submit(Ledger(self.p/'ledger.sqlite'),'run1',self.transport)
  self.assertEqual(len(self.server.calls),n)
  other=Ledger(self.p/'other.sqlite');prepare(other,'other',T0,self.store,'',self.p/'other.enc',self.key);self.server.mode='ok'
  execute_upload(other,'other',self.key,self.transport);execute_submit(other,'other',self.transport);first=poll_batch(other,'other',self.transport);n=len(self.server.calls);second=poll_batch(other,'other',self.transport)
  self.assertEqual(first['status'],second['status']);self.assertEqual(n,len(self.server.calls))
 def test_realtime_stale_after_response_is_not_validated(self):
  self.server.realtime=response('rt',[{'id':i} for i in range(1,9)])['response']['body']
  self.server.on_realtime=lambda:self.store.data['topics']['1'].update(body_text='changed')
  with self.assertRaises(Exception):run_realtime(self.ledger,'run1',self.store,'',self.transport,self.key,self.p/'rt.enc')
  with self.ledger.db() as db:self.assertEqual(db.execute('SELECT state FROM realtime_events ORDER BY seq DESC LIMIT 1').fetchone()[0],'received')
  self.assertTrue((self.p/'rt.enc').exists())
 def test_get_redirect_is_not_followed(self):
  self.server.mode='redirect'
  with self.assertRaises(TransportError) as e:self.transport.get_file('file-testout')
  self.assertEqual(str(e.exception),'http_302');self.assertEqual([x[0] for x in self.server.calls],['/v1/files/file-testout/content'])
 def test_callback_error_cannot_leak_secret(self):
  def broken():raise TransportError(SECRET)
  bad=BatchTransport.for_loopback(self.server.server_address[1],str(self.p/'cert.pem'),broken)
  with self.assertRaises(TransportError) as e:bad.get_file('file-testout')
  self.assertNotIn(SECRET,str(e.exception));self.assertEqual(len(self.server.calls),0)
 def test_verified_tls_and_download_bound(self):
  wrong=BatchTransport.for_loopback(self.server.server_address[1],None,lambda:SECRET,timeout=3,max_download=10)
  with self.assertRaises(TransportError):wrong.get_file('file-testout')
  self.server.output='x'*100
  limited=BatchTransport.for_loopback(self.server.server_address[1],str(self.p/'cert.pem'),lambda:SECRET,timeout=3,max_download=10)
  with self.assertRaises(TransportError):limited.get_file('file-testout')
if __name__=='__main__':unittest.main()
