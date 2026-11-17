import datetime as dt
import tempfile
import unittest

from receipts import ReceiptStore


class ReceiptTests(unittest.TestCase):
    def test_daily_admission_is_persistent(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            when = dt.datetime(2026, 1, 2, 23, 59, tzinfo=dt.timezone.utc)
            with store.singleton():
                admitted, receipt = store.admit(when)
                self.assertTrue(admitted)
                self.assertEqual(receipt['run_id'], '2026-01-02')
            reopened = ReceiptStore(root)
            with reopened.singleton():
                admitted, duplicate = reopened.admit(when)
                self.assertFalse(admitted)
                self.assertEqual(duplicate, receipt)

    def test_pending_intent_survives_restart_and_blocks_reexecution(self):
        with tempfile.TemporaryDirectory() as root:
            when = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
            store = ReceiptStore(root)
            with store.singleton():
                _, receipt = store.admit(when)
                store.begin_attempt(receipt['run_id'])
            reopened = ReceiptStore(root)
            with reopened.singleton():
                admitted, receipt = reopened.admit(when)
                self.assertFalse(admitted)
                self.assertEqual(receipt['attempts'][0]['outcome'], 'pending')
                with self.assertRaises(RuntimeError):
                    reopened.begin_attempt(receipt['run_id'])

    def test_lock_is_nonblocking(self):
        with tempfile.TemporaryDirectory() as root:
            with ReceiptStore(root).singleton():
                with self.assertRaises(BlockingIOError):
                    with ReceiptStore(root).singleton():
                        self.fail('lock unexpectedly acquired')

    def test_utc_conversion_and_naive_time_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                with self.assertRaises(ValueError):
                    store.admit(dt.datetime(2026, 1, 2))
                _, receipt = store.admit(dt.datetime(2026, 1, 2, 1, tzinfo=dt.timezone(dt.timedelta(hours=8))))
                self.assertEqual(receipt['run_id'], '2026-01-01')

    def test_intent_is_irreversible_and_limited_to_two_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
                rid = run['run_id']
                self.assertEqual(store.begin_attempt(rid), 1)
                with self.assertRaises(RuntimeError):
                    store.begin_attempt(rid)
                store.record_attempt(rid, 1, response_handle='resp_1', model='model', tier='flex', usage={'tokens': 1}, outcome='success')
                self.assertEqual(store.begin_attempt(rid), 2)
                store.record_attempt(rid, 2, response_handle='resp_2', model='model', tier='flex', usage={}, outcome='success')
                with self.assertRaises(RuntimeError):
                    store.begin_attempt(rid)
            other = ReceiptStore(root)
            with other.singleton():
                self.assertFalse(other.admit(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))[0])
                with self.assertRaises(RuntimeError):
                    other.begin_attempt(rid)


if __name__ == '__main__':
    unittest.main()
