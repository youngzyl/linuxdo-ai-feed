import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from store import Store

class StreamingSaveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.s=Store(self.root/'state.json',self.root/'failures.jsonl')
 def tearDown(self):self.tmp.cleanup()
 def test_save_streams_without_json_dumps_materializing_full_state(self):
  self.s.upsert_topics([{'id':1,'title':'a','body_text':'测'*1000,'created_at':'2026-01-01T00:00:00Z'}])
  with patch('store.json.dumps',side_effect=AssertionError('full state materialized')):self.s.save()
  self.assertEqual(len(json.loads(self.s.path.read_text())['topics']),1)
 def test_serialization_error_keeps_previous_state_file(self):
  import subprocess,sys
  code = '''from pathlib import Path\nfrom unittest.mock import patch\nfrom store import Store\nimport sys\np=Path(sys.argv[1]);s=Store(p/'state.json',p/'failures.jsonl')\nwith patch('store.json.dump',side_effect=OSError('fault')):s.save()\n'''
  old=self.s.path.read_bytes()
  r=subprocess.run([sys.executable,'-c',code,str(self.root)],cwd=str(Path(__file__).parent),capture_output=True,timeout=10)
  self.assertEqual(r.returncode,70)
  self.assertEqual(self.s.path.read_bytes(),old)
if __name__=='__main__':unittest.main()
