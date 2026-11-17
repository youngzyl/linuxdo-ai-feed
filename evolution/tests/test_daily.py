import datetime as dt
import fcntl
import json
from pathlib import Path
import tempfile
import unittest

import daily

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.receipts = self.root / 'receipts'
        self.runs.mkdir()
        self.receipts.mkdir()
        self.calls = 0

    def journal(self, day, outcomes):
        obj = {'run_id': day, 'status': 'admitted', 'attempts': []}
        for i, outcome in enumerate(outcomes, 1):
            obj['attempts'].append({'number': i, 'outcome': outcome,
                                    'response_handle': 'resp_1' if outcome == 'success' else None})
        (self.receipts / (day + '.json')).write_text(json.dumps(obj))

    def report(self, day, status='completed'):
        out = self.runs / day
        out.mkdir(exist_ok=True)
        (out / 'live-report.json').write_text(json.dumps({'status': status, 'best_id': 'old',
                                                         'best_training': {'valid': True}}))
        (out / 'candidate.py').write_text('preserved')

    def fake(self, output, deadline, **kwargs):
        self.calls += 1
        self.journal('2026-11-17', ['success'])
        self.report('2026-11-17')
        return 0

    def run_daily(self, **kwargs):
        return daily.run_daily(self.runs, self.receipts, 'key-path', now=NOW, runner=self.fake, **kwargs)

    def test_first_run_then_duplicate_returns_report_without_call(self):
        self.report('2026-11-16')
        self.journal('2026-11-16', ['success'])
        code, result = self.run_daily()
        self.assertEqual((code, self.calls), (0, 1))
        self.assertEqual(result['prior_runs'][0]['best_id'], 'old')
        code, result = self.run_daily()
        self.assertEqual((code, self.calls, result['status']), (0, 1, 'existing_report'))
        self.assertEqual((self.runs / '2026-11-16/candidate.py').read_text(), 'preserved')

    def test_any_day_unknown_or_pending_blocks(self):
        for outcome in ('unknown', 'pending'):
            with self.subTest(outcome=outcome):
                self.journal('2026-11-15', [outcome])
                code, result = self.run_daily()
                self.assertEqual((code, self.calls, result['status']), (1, 0, 'blocked_unresolved'))

    def test_empty_prior_receipt_blocks(self):
        self.journal('2026-11-15', [])
        self.assertEqual(self.run_daily()[1]['status'], 'blocked_unresolved')
        self.assertEqual(self.calls, 0)

    def test_duplicate_completed_readonly_even_if_prior_day_unknown(self):
        self.journal('2026-11-15', ['unknown'])
        self.journal('2026-11-17', ['success'])
        self.report('2026-11-17')
        code, result = self.run_daily()
        self.assertEqual((code, self.calls, result['status']), (0, 0, 'existing_report'))

    def test_failed_report_with_success_is_not_retried(self):
        self.journal('2026-11-17', ['success'])
        self.report('2026-11-17', 'failed')
        code, result = self.run_daily()
        self.assertEqual((code, self.calls, result['status']), (1, 0, 'existing_report'))
        self.assertEqual(result['report']['status'], 'failed')

    def test_existing_incomplete_report_or_dir_block(self):
        (self.runs / '2026-11-17').mkdir()
        self.assertEqual(self.run_daily()[1]['status'], 'blocked_incomplete_existing_run')
        self.assertEqual(self.calls, 0)

    def test_bad_or_unexpected_journal_blocks(self):
        self.journal('2026-11-15', ['something-new'])
        self.assertEqual(self.run_daily()[1]['status'], 'blocked_unresolved')
        (self.receipts / '2026-11-15.json').write_text('{')
        with self.assertRaises(ValueError):
            self.run_daily()
        self.assertEqual(self.calls, 0)

    def test_singleton_and_deadline(self):
        with (self.receipts / '.daily.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_daily()
        with self.assertRaises(ValueError):
            self.run_daily(deadline=1201)
        self.assertEqual(self.calls, 0)

    def test_unknown_after_success_does_not_succeed(self):
        def uncertain(output, deadline, **kwargs):
            self.journal('2026-11-17', ['success', 'unknown'])
            self.report('2026-11-17')
            return 0
        code, _ = daily.run_daily(self.runs, self.receipts, 'key-path', now=NOW, runner=uncertain)
        self.assertEqual(code, 1)
        self.assertEqual(self.run_daily()[1]['status'], 'blocked_unresolved')
        self.assertEqual(self.calls, 0)

    def test_abandoned_history_allows_later_utc_day(self):
        obj = {'run_id': '2026-11-16', 'attempts': [{'number': 1, 'outcome': 'unknown',
               'disposition': 'abandoned_no_retry', 'abandon_source_sha256': 'a'*64,
               'recovery_nonce': 'b'*64, 'abandoned_at': '2026-11-16T12:00:00+00:00'}]}
        (self.receipts / '2026-11-16.json').write_text(json.dumps(obj))
        code, result = self.run_daily()
        self.assertEqual((code, self.calls, result['status']), (0, 1, 'ran'))
        self.assertEqual(json.loads((self.receipts / '2026-11-16.json').read_text()), obj)

    def test_completed_report_without_success_fails(self):
        self.journal('2026-11-17', ['error'])
        self.report('2026-11-17')
        self.assertEqual(self.run_daily()[0], 1)
        self.assertEqual(self.calls, 0)


if __name__ == '__main__':
    unittest.main()
