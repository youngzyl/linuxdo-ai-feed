import os
import tempfile
import unittest
from pathlib import Path
from responses_adapter import read_credential_file


class CredentialFileTests(unittest.TestCase):
    def test_private_regular_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key'
            path.write_text('offline-test-key\n')
            path.chmod(0o600)
            self.assertEqual(read_credential_file(path), 'offline-test-key')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_credential_file(path)
            path.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_credential_file(link)
            path.write_text('bad key\n')
            with self.assertRaises(ValueError):
                read_credential_file(path)


if __name__ == '__main__':
    unittest.main()
