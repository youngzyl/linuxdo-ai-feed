import subprocess,sys,tempfile,unittest
from pathlib import Path
CHILD=r'''
import sys
from pathlib import Path
import run
run.DATA_DIR=Path(sys.argv[1]);run.LOG_DIR=Path(sys.argv[1]);run.setup_logging=lambda verbose=False:type('L',(),{'info':staticmethod(lambda x:None)})()
run.Pipeline=lambda *a:None
try: cfg,store,pipeline,log=run.build({})
except RuntimeError: sys.exit(21)
print('locked',flush=True)
if sys.argv[2]=='hold':sys.stdin.readline()
'''
class SingletonTests(unittest.TestCase):
 def test_two_independent_processes_cannot_build_same_store(self):
  with tempfile.TemporaryDirectory() as tmp:
   first=subprocess.Popen([sys.executable,'-c',CHILD,tmp,'hold'],cwd=str(Path(__file__).parent),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   try:
    self.assertEqual(first.stdout.readline().strip(),'locked')
    second=subprocess.run([sys.executable,'-c',CHILD,tmp,'once'],cwd=str(Path(__file__).parent),capture_output=True,text=True,timeout=10)
    self.assertEqual(second.returncode,21)
   finally:
    first.stdin.write('\n');first.stdin.flush();first.communicate(timeout=10)
   third=subprocess.run([sys.executable,'-c',CHILD,tmp,'once'],cwd=str(Path(__file__).parent),capture_output=True,text=True,timeout=10)
   self.assertEqual(third.returncode,0)
if __name__=='__main__':unittest.main()
