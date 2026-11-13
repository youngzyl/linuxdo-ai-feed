import json, os, pathlib, subprocess, sys, tempfile, unittest
from store import Store
import canary

CHILD=r'''
import os,sys,threading
from pathlib import Path
from unittest.mock import patch
import canary
from store import Store
p=Path(sys.argv[1]); phase=sys.argv[2]; s=Store(p/'state.json',p/'failure.jsonl')
s.upsert_topics([{'id':i,'title':'t'+str(i),'body_text':'body','created_at':f'2026-01-{i%28+1:02}T00:00:00Z'} for i in range(1,49)]); s.save()
x=canary.snapshot(s,'run_001')
payload={'run_id':'run_001','batch':0,'model':'gpt-6.1-terra','provider':'openai-codex','items':[{'i':i+1,'topic_id':v['id'],'source_version':v['source_version'],'valuable':True,'score':80,'category':'c','reason':'r','summary':'s'} for i,v in enumerate(x['topics'][:8])]}
(p/'payload.json').write_text(__import__('json').dumps(payload))
real_replace=os.replace;real_fsync=os.fsync; counter=[0]
def fake_replace(src,dst):
 if phase=='replace_before': raise OSError('before')
 real_replace(src,dst)
 if phase=='replace_after': raise OSError('after')
def fake_fsync(fd):
 counter[0]+=1
 if phase=='temp_fsync' and counter[0]==1: raise OSError('temp')
 if phase=='dir_fsync' and counter[0]==2: raise OSError('dir')
 return real_fsync(fd)
def fake_publish(*a):
 ready=threading.Event()
 def competing():
  ready.set()
  with s.lock: (p/'CONCURRENT_WRITE').write_text('must not happen')
 threading.Thread(target=competing,daemon=True).start();ready.wait(2)
 raise RuntimeError('publish')
class SimulatedStop(BaseException): pass
def fake_stop():
 if not s.lock._is_owned(): os._exit(72)
 raise SimulatedStop()
try:
 with patch('canary.os.replace',side_effect=fake_replace), patch('canary.os.fsync',side_effect=fake_fsync):
  if phase=='publish':
   with patch('canary._publish',side_effect=fake_publish): canary.apply(s,payload)
  elif phase=='injected':
   with patch('canary._publish',side_effect=RuntimeError('fault')), patch('canary._failstop',side_effect=fake_stop): canary.apply(s,payload)
  else: canary.apply(s,payload)
except SimulatedStop:
 os._exit(71)
(p/'AFTER_WRITE').write_text('should not appear')
'''
class FailStopTests(unittest.TestCase):
 def child(self,phase):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);p=pathlib.Path(t.name)
  r=subprocess.run([sys.executable,'-c',CHILD,str(p),phase],cwd=str(pathlib.Path(__file__).parent),capture_output=True,timeout=20)
  return p,r
 def test_uncertain_replace_kills_process_before_following_write(self):
  for phase in ('replace_before','replace_after','publish','dir_fsync'):
   with self.subTest(phase=phase):
    p,r=self.child(phase);self.assertEqual(r.returncode,70);self.assertFalse((p/'AFTER_WRITE').exists());self.assertFalse((p/'CONCURRENT_WRITE').exists())
    data=json.loads((p/'state.json').read_text())
    if phase in ('replace_after','publish','dir_fsync'):
     self.assertIn('0',data['canary']['receipts'])
     s=Store(p/'state.json',p/'failure.jsonl');payload=json.loads((p/'payload.json').read_text());self.assertEqual(canary.apply(s,payload)['accepted'],8)
    else:self.assertNotIn('0',data['canary']['receipts'])
 def test_pre_replace_fsync_not_committed(self):
  p,r=self.child('temp_fsync');self.assertNotEqual(r.returncode,70);self.assertFalse((p/'AFTER_WRITE').exists())
  data=json.loads((p/'state.json').read_text());self.assertNotIn('0',data['canary']['receipts'])
 def test_injected_baseexception_only_in_child_and_lock_held(self):
  p,r=self.child('injected');self.assertEqual(r.returncode,71);self.assertFalse((p/'AFTER_WRITE').exists())
if __name__=='__main__':unittest.main()
