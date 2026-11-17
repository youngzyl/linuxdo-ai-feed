"""Explicit live-host regression: disabled sandbox cannot start a container."""
import shutil
import subprocess
import unittest
from unittest import mock

import sandbox


@unittest.skipUnless(shutil.which('docker'), 'Docker unavailable')
class LiveIsolationTests(unittest.TestCase):
    def test_forged_stdout_and_exit_are_rejected_without_docker(self):
        # The old in-process runner allowed this to impersonate a successful result.
        attack = 'import os; os.write(1, b"[]"); os._exit(0)'
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('spawned Docker')):
            with self.assertRaisesRegex(sandbox.SandboxError, 'candidate execution disabled'):
                sandbox.run_candidate(attack, [])

    def test_benign_candidate_also_fails_closed(self):
        with self.assertRaisesRegex(sandbox.SandboxError, 'candidate execution disabled'):
            sandbox.run_candidate('def choose_batch(rows): return []', [])


if __name__ == '__main__':
    unittest.main()
