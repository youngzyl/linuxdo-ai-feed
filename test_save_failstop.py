import subprocess,sys,tempfile,unittest,json
from pathlib import Path
CHILD=r'''
import sys,os
from pathlib import Path
from unittest.mock import patch
from store import Store
p=Path(sys.argv[1]);phase=sys.argv[2];s=Store(p/'state.json',p/'failure.jsonl');s.upsert_topics([{'id':1,'title':'x','created_at':'2026-01-01T00:00:00Z'}]);s.save();s.set_fields(1,title='new')
real_replace=os.replace;real_fsync=os.fsync;calls=[0]
def replacement(a,b):
 real_replace(a,b)
 if phase=='replace':raise OSError('injected')
def fsync(fd):
 calls[0]+=1
 if (phase=='file' and calls[0]==1) or (phase=='dir' and calls[0]==2):raise OSError('injected')
 return real_fsync(fd)
with patch('store.os.replace',side_effect=replacement),patch('store.os.fsync',side_effect=fsync):s.save()
(p/'AFTER').write_text('bad')
'''
class SaveFailStopTests(unittest.TestCase):
 def test_fsync_and_uncertain_replace_kill_before_later_writes(self):
  for phase in ('file','replace','dir'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp);r=subprocess.run([sys.executable,'-c',CHILD,tmp,phase],cwd=str(Path(__file__).parent),capture_output=True,timeout=20)
    self.assertEqual(r.returncode,70);self.assertFalse((p/'AFTER').exists())
    title=json.loads((p/'state.json').read_text())['topics']['1']['title']
    self.assertEqual(title,'synthetic' if False else ('x' if phase=='file' else 'new'))
if __name__=='__main__':unittest.main()
