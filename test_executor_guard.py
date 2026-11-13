import os,subprocess,sys,tempfile,unittest
from pathlib import Path
from luna_orchestrator import ExecutorGuard,OrchestrationError
CHILD=r'''
import sys
from pathlib import Path
from luna_orchestrator import ExecutorGuard
root=Path(sys.argv[1]);run=sys.argv[2];directory=Path(sys.argv[3]);action=sys.argv[4]
g=ExecutorGuard.for_test(root)
try:
 g.validate(run,directory)
 with g.lock():
  if action=='hold':print('locked',flush=True);sys.stdin.readline()
  else:(root/'API_CALLED').write_text('bad')
except Exception as e:
 print(type(e).__name__,flush=True);sys.exit(3)
'''
class GuardTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.service=Path(self.tmp.name)/'service';self.service.mkdir(mode=0o700);self.g=ExecutorGuard.for_test(self.service)
 def test_canonical_path_only_and_alias_refused(self):
  expected=self.service/'luna-runs'/'run1';self.g.validate('run1',expected)
  for bad in (self.service/'elsewhere'/'run1',self.service/'luna-runs'/'other',self.service/'luna-runs'/'..'/'luna-runs'/'run1'):
   with self.subTest(bad=bad):
    with self.assertRaises(OrchestrationError):self.g.validate('run1',bad)
  (self.service/'luna-runs').mkdir();(self.service/'luna-runs'/'link').symlink_to(self.service,target_is_directory=True)
  with self.assertRaises(OrchestrationError):self.g.validate('link',self.service/'luna-runs'/'link')
 def test_two_processes_different_workdirs_use_global_lock(self):
  a=self.service/'luna-runs'/'one';b=self.service/'luna-runs'/'two';cwd=str(Path(__file__).parent)
  first=subprocess.Popen([sys.executable,'-c',CHILD,str(self.service),'one',str(a),'hold'],cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  try:
   self.assertEqual(first.stdout.readline().strip(),'locked')
   second=subprocess.run([sys.executable,'-c',CHILD,str(self.service),'two',str(b),'try'],cwd=cwd,capture_output=True,text=True,timeout=10)
   self.assertEqual(second.returncode,3);self.assertFalse((self.service/'API_CALLED').exists())
   alias=subprocess.run([sys.executable,'-c',CHILD,str(self.service),'one',str(b),'try'],cwd=cwd,capture_output=True,text=True,timeout=10)
   self.assertEqual(alias.returncode,3);self.assertFalse((self.service/'API_CALLED').exists())
  finally:
   if first.poll() is None:first.stdin.write('\n');first.stdin.flush()
   first.communicate(timeout=10)
if __name__=='__main__':unittest.main()
