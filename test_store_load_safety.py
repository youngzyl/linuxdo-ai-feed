import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from store import Store

class StateLoadTests(unittest.TestCase):
 def setUp(self): self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name);self.state=self.p/'state.json';self.failure=self.p/'failure.jsonl'
 def tearDown(self): self.temp.cleanup()
 def build(self): return Store(self.state,self.failure)
 def test_missing_creates_new_store(self):
  s=self.build();self.assertTrue(self.state.is_file());self.assertEqual(s.data['topics'],{})
 def test_malformed_json_fails_without_write(self):
  raw=b'{"topics":oops';self.state.write_bytes(raw)
  with self.assertRaises(Exception):self.build()
  self.assertEqual(self.state.read_bytes(),raw);self.assertFalse((self.p/'state.json.tmp').exists())
 def test_nonobject_fails_without_write(self):
  for raw in (b'[]',b'null',b'"hi"',b'123'):
   with self.subTest(raw=raw):
    self.state.write_bytes(raw)
    with self.assertRaises(Exception):self.build()
    self.assertEqual(self.state.read_bytes(),raw)
 def test_inaccessible_does_not_reset_or_write(self):
  raw=b'{"topics":{}}';self.state.write_bytes(raw);orig=Path.read_text
  def guarded(p,*args,**kwargs):
   if p==self.state:raise PermissionError('simulated')
   return orig(p,*args,**kwargs)
  with patch.object(Path,'read_text',guarded):
   with self.assertRaises(Exception):self.build()
  self.assertEqual(self.state.read_bytes(),raw);self.assertFalse((self.p/'state.json.tmp').exists())
 def test_dangling_symlink_not_treated_as_new(self):
  self.state.symlink_to(self.p/'absent-target')
  with self.assertRaises(Exception):self.build()
  self.assertTrue(self.state.is_symlink());self.assertFalse((self.p/'absent-target').exists())
 def test_directory_at_state_path_fails_without_replacement(self):
  self.state.mkdir()
  with self.assertRaises(Exception):self.build()
  self.assertTrue(self.state.is_dir())
 def test_competing_create_is_never_clobbered(self):
  original=Path.open
  state=self.state
  def raced(p,*args,**kwargs):
   mode=args[0] if args else kwargs.get('mode','r')
   if p==state and mode=='x':
    with original(p,'w',encoding='utf-8') as f:f.write('{"topics":{"existing":{}}}')
   return original(p,*args,**kwargs)
  with patch.object(Path,'open',raced):
   with self.assertRaises(Exception):self.build()
  self.assertEqual(json.loads(self.state.read_text())['topics'],{'existing':{}})
 def test_wrong_topic_collection_type_fails(self):
  raw=b'{"topics":[]}';self.state.write_bytes(raw)
  with self.assertRaises(Exception):self.build()
  self.assertEqual(self.state.read_bytes(),raw)
 def test_missing_topics_and_corrupt_topic_record_fail(self):
  for obj in ({}, {'topics':{'7':None}}, {'topics':{'7':[]}}, {'topics':{},'canary':[]}):
   with self.subTest(obj=obj):
    self.state.write_text(json.dumps(obj));raw=self.state.read_bytes()
    with self.assertRaises(Exception):self.build()
    self.assertEqual(self.state.read_bytes(),raw)
 def test_valid_older_state_migrates_without_disk_write(self):
  obj={'topics':{'7':{'id':7,'title':'a','state':'picked','filter':{'valuable':True},'body_text':'b'}},'queue':[7]};self.state.write_text(json.dumps(obj));raw=self.state.read_bytes();s=self.build()
  self.assertEqual(self.state.read_bytes(),raw);self.assertEqual(s.data['queue'],[7]);self.assertEqual(s.data['topics']['7']['filter']['source_version'],s.data['topics']['7']['source_version'])
if __name__=='__main__':unittest.main()
