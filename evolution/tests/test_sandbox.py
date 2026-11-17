"""The candidate execution boundary must remain disabled until repaired."""
import subprocess
import unittest
from unittest import mock

import sandbox


class SandboxTests(unittest.TestCase):
    def test_all_candidates_fail_before_any_child_process(self):
        candidates = [
            'def choose_batch(rows): return []',
            'import os; os.write(1, b"[]"); os._exit(0)',
            'def choose_batch(rows):\n while True: pass',
            '',
        ]
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('spawned')):
            for code in candidates:
                with self.subTest(code=code), self.assertRaisesRegex(
                    sandbox.SandboxError, 'candidate execution disabled'
                ):
                    sandbox.run_candidate(code, [])

    def test_invalid_objects_are_not_interpreted(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.run_candidate(object(), object())


if __name__ == '__main__':
    unittest.main()
