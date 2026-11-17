import json,unittest
from unittest.mock import MagicMock,patch
from future_flex import request,response_format
class StructuredPayloadTests(unittest.TestCase):
 def test_flex_connection_timeout_is_900_seconds(self):
  c=MagicMock();c.getresponse.return_value.status=200;c.getresponse.return_value.read.return_value=b'{}'
  with patch('future_flex.http.client.HTTPSConnection',return_value=c) as conn:request([{'id':1}],'','fake')
  conn.assert_called_once_with('api.openai.com',443,timeout=900)
 def test_flex_wire_schema_and_count(self):
  for count in (1,4,8):
   c=MagicMock();c.getresponse.return_value.status=200;c.getresponse.return_value.read.return_value=b'{}'
   with patch('future_flex.http.client.HTTPSConnection',return_value=c):request([{'id':i} for i in range(count)],'','fake')
   body=json.loads(c.request.call_args.kwargs['body']);self.assertEqual(body['model'],'gpt-6-luna');self.assertEqual(body['reasoning'],{'effort':'high'});self.assertEqual(body['service_tier'],'flex');self.assertEqual(body['max_output_tokens'],8192)
   fmt=body['text']['format'];self.assertEqual(fmt['type'],'json_schema');self.assertTrue(fmt['strict']);schema=fmt['schema'];self.assertEqual(schema['required'],['results']);self.assertFalse(schema['additionalProperties']);arr=schema['properties']['results'];self.assertEqual((arr['minItems'],arr['maxItems']),(count,count));row=arr['items'];self.assertFalse(row['additionalProperties']);self.assertEqual(set(row['required']),{'i','valuable','score','category','reason','summary'})
 def test_invalid_counts(self):
  for count in (0,9,True,1.0,None):
   with self.subTest(count=count),self.assertRaises(ValueError):response_format(count)
if __name__=='__main__':unittest.main()
