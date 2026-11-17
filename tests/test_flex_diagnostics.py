import json
from unittest.mock import patch, MagicMock

from test_future_flex import Tests as FlexTests
from future_flex import FutureFlex, FlexError, request
from store import Store


class DiagnosticTests(FlexTests):
 def test_predispatch_and_response_metadata_persist_before_validation(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def provider(*args):
   state=Store(self.store.path,self.store.failure_log).data['future_flex']
   self.assertEqual(state['status'],'unknown')
   self.assertEqual(state['pending_ids'],[10])
   self.assertTrue(state['pending_started_at'])
   d=state['diagnostic'];self.assertEqual(d['request_nonce'],state['last_attempt'])
   self.assertIn('10',d['source_versions']);self.assertIn('10',d['image_versions'])
   body=self.provider(*args);body['id']='resp_abc123';body['output'][0]['content'][0]['text']='sensitive article'
   return body
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=provider,clock=lambda:self.now)
  self.assertEqual(f.run()['error'],'response_invalid')
  state=Store(self.store.path,self.store.failure_log).data['future_flex'];d=state['diagnostic']
  self.assertEqual(d['response_id'],'resp_abc123');self.assertEqual(d['response_model'],'gpt-6-luna')
  self.assertEqual(d['response_tier'],'flex');self.assertEqual(d['usage']['input_tokens'],10)
  self.assertEqual(d['phase'],'validation');self.assertEqual(d['code'],'response_invalid')
  self.assertNotIn('sensitive article',json.dumps(d))
  self.assertEqual(f.run()['error'],'unknown')

 def test_error_is_sanitized(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def provider(*args):raise FlexError('auth_rejected',403)
  FutureFlex(self.cfg,self.store,lambda x:None,transport=provider,clock=lambda:self.now).run()
  d=Store(self.store.path,self.store.failure_log).data['future_flex']['diagnostic']
  self.assertEqual((d['phase'],d['code'],d['http_status']),('dispatch','auth_rejected',403))
  self.assertNotIn('synthetic',json.dumps(d))

 def test_wire_nonce(self):
  c=MagicMock();c.getresponse.return_value.status=200;c.getresponse.return_value.read.return_value=b'{}'
  with patch('future_flex.http.client.HTTPSConnection',return_value=c):request([{'id':1}],'','synthetic',request_nonce='a'*32)
  self.assertEqual(c.request.call_args.kwargs['headers']['X-Client-Request-Id'],'a'*32)

 def test_known_http_status_survives_broken_error_body(self):
  for status,code in ((408,'outcome_unknown'),(500,'outcome_unknown'),(429,'outcome_unknown'),(403,'auth_rejected')):
   with self.subTest(status=status):
    c=MagicMock();c.getresponse.return_value.status=status;c.getresponse.return_value.read.side_effect=TimeoutError('sensitive')
    with patch('future_flex.http.client.HTTPSConnection',return_value=c):
     with self.assertRaises(FlexError) as caught:request([{'id':1}],'','synthetic')
    self.assertEqual((caught.exception.code,caught.exception.status),(code,status))

 def test_incomplete_reason_recorded_without_replay(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def provider(*args):
   body=self.provider(*args);body.update(id='resp_abc123',status='incomplete',incomplete_details={'reason':'max_output_tokens'});return body
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=provider,clock=lambda:self.now)
  self.assertEqual(f.run()['error'],'response_invalid')
  d=Store(self.store.path,self.store.failure_log).data['future_flex']['diagnostic']
  self.assertEqual((d['response_status'],d['incomplete_reason']),('incomplete','max_output_tokens'))
  self.assertEqual(f.run()['error'],'unknown')
