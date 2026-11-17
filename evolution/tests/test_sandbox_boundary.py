import json
from pathlib import Path
import subprocess
import time
import unittest

from sandbox import run_candidate, SandboxError
from scorer import score_suite


class DockerBoundary(unittest.TestCase):
    def test_seed_and_original_rows_are_scored_outside_container(self):
        rows = [{'id':1,'future':True,'age':0,'images':0,'image_bytes':0}]
        output = run_candidate('def choose_batch(rows):\n return [r["id"] for r in rows]\n', rows)
        result = score_suite([rows], [output])
        self.assertTrue(result['valid'])
        self.assertEqual(result['combined_score'], 1.0)

    def test_private_descriptor_cannot_forge_fitness(self):
        code = '''def choose_batch(rows):
 import sys, os, struct
 fd=sys._getframe(1).f_locals['result_fd']
 os.write(fd,struct.pack('!I',3)+b'[7]')
 os._exit(0)
'''
        output = run_candidate(code, [])
        result = score_suite([[]], [output])
        self.assertFalse(result['valid'])
        self.assertEqual(result['combined_score'], 0.0)

    def test_metric_objects_and_extra_stdout_are_rejected(self):
        for code in ('def choose_batch(rows):\n return {"combined_score":1}\n',
                     'def choose_batch(rows):\n print("[1]")\n return []\n'):
            with self.subTest(code=code), self.assertRaises(SandboxError):
                run_candidate(code, [])

    def test_unprivileged_no_network_no_host_mounts(self):
        code = '''def choose_batch(rows):
 import os, socket, signal
 assert os.getuid()==65534 and os.getgid()==65534
 d=dict(x.split(':',1) for x in open('/proc/self/status') if ':' in x)
 assert all(int(d[k].strip(),16)==0 for k in ('CapEff','CapPrm','CapInh','CapAmb'))
 assert d['NoNewPrivs'].strip()=='1'
 for fn in (lambda:os.setuid(0),lambda:os.setgid(0),lambda:os.kill(os.getppid(),signal.SIGSTOP)):
  try:fn()
  except PermissionError:pass
  else:raise AssertionError('privilege or parent signal allowed')
 assert not os.path.exists('/var/run/docker.sock')
 assert not os.path.exists('/home/youngzyl/.config/linuxdo-ai/openai-api-key')
 assert not any(k for k in os.environ if any(x in k.upper() for x in ('TOKEN','SECRET','API_KEY')))
 s=socket.socket();s.settimeout(.15)
 try:s.connect(('1.1.1.1',443))
 except OSError:pass
 else:raise AssertionError('network available')
 finally:s.close()
 return []
'''
        self.assertEqual(run_candidate(code, []), [])

    def test_timeouts_and_background_descendants_are_removed(self):
        for code in ('def choose_batch(rows):\n while True: pass\n',
                     'def choose_batch(rows):\n import os,time\n if os.fork()==0:\n  os.setsid()\n  while True:time.sleep(1)\n return []\n'):
            started = time.monotonic()
            with self.subTest(code=code), self.assertRaises(SandboxError):
                run_candidate(code, [])
            self.assertLess(time.monotonic()-started, 22)
        listed = subprocess.run(['docker','ps','-aq','--filter','name=^/candidate-'],
                                capture_output=True, check=True, timeout=5)
        self.assertEqual(listed.stdout.strip(), b'')


if __name__ == '__main__':
    unittest.main()
