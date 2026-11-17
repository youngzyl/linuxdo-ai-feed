import tempfile
import unittest
from pathlib import Path
from responses_adapter import start_isolated_service

class IsolatedStartTests(unittest.TestCase):
    def test_fake_key_start_stop_admission_no_calls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            key = root / 'key'
            key.write_text('offline-test-key')
            key.chmod(0o600)
            store = root / 'receipts'
            service = start_isolated_service(store, credential_file=key, deadline_seconds=20)
            service.stop()
            import json
            receipts = list(store.glob('????-??-??.json'))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())['attempts'], [])
            with self.assertRaises(RuntimeError):
                start_isolated_service(store, credential_file=key, deadline_seconds=20)
