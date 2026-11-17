import json
import tempfile
import unittest
from pathlib import Path

from store import Store


class FlexHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        self.store = Store(path / 'state.json', path / 'failures.jsonl')
        self.cfg = {'future_flex': {'enabled': True}, 'filter': {'enabled': True}}

    def test_persisted_unknown_is_reported_even_before_filter_runs(self):
        self.store.data['future_flex'] = {'status': 'unknown', 'pending_ids': [1, 2],
            'pending_started_at': '2026-01-01T12:00:00+00:00', 'last_error_code': 'response_invalid'}
        self.store.save()
        path = Path(self.tmp.name)
        reloaded = Store(path / 'state.json', path / 'failures.jsonl')
        payload = reloaded.health_payload(cfg=self.cfg, uptime_s=0)
        self.assertEqual(payload['status'], 'degraded')
        self.assertEqual(payload['attention'], {'needed': True, 'kind': 'future_flex_unknown',
            'reason': 'future flex dispatch outcome uncertain; dispatch blocked',
            'code': 'response_invalid', 'started_at': '2026-01-01T12:00:00+00:00', 'count': 2})

    def test_auth_is_distinct_and_diagnostics_cannot_leak(self):
        self.store.data['future_flex'] = {'status': 'auth_disabled', 'pending_ids': ['secret'],
            'pending_started_at': 'secret', 'last_error_code': 'secret'}
        att = self.store.health_payload(cfg=self.cfg, uptime_s=0)['attention']
        self.assertEqual(att['kind'], 'future_flex_auth_disabled')
        self.assertEqual(att['code'], 'auth_rejected')
        self.assertIsNone(att['started_at'])
        self.assertIsNone(att['count'])
        self.assertNotIn('secret', json.dumps(att))

    def test_disabled_and_legacy(self):
        self.store.data['future_flex'] = {'status': 'unknown'}
        self.assertEqual(self.store.attention(self.cfg)['code'], 'outcome_unknown')
        self.cfg['future_flex']['enabled'] = False
        self.assertFalse(self.store.attention(self.cfg)['needed'])

    def test_ready_state_does_not_claim_stale_error_blocks_dispatch(self):
        from flex_health import blocked_attention
        self.assertIsNone(blocked_attention({'status': 'ready'}, 'unknown'))
        self.assertIsNone(blocked_attention({'status': 'ready'}, 'auth_disabled'))
        self.assertEqual(blocked_attention({'status': 'unknown'}, 'unknown')['kind'], 'future_flex_unknown')


if __name__ == '__main__':
    unittest.main()
