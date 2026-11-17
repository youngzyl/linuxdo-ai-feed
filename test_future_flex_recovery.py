"""HTTP contract tests for owner-approved recovery of uncertain Flex batches."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import server
from future_flex import FlexError, FutureFlex, backfill_exclusions
from store import Store

ROUTE = '/api/future-flex/recover'
TOKEN = 'test-recovery-owner-token'


def attempt_digest(meta):
    snapshot = {k: meta.get(k) for k in ('status', 'pending_ids', 'last_attempt')}
    raw = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


class FutureFlexRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / 'state.json', root / 'failures')
        key = root / 'key'
        key.write_text('synthetic')
        key.chmod(0o600)
        self.cfg = {'host': '127.0.0.1', 'port': 0, 'future_flex': {
            'enabled': True, 'activation_t0': 100, 'key_file': str(key), 'max_batch': 8}}
        self.calls = []
        def uncertain(rows, *args):
            self.calls.append([r['id'] for r in rows])
            raise FlexError('outcome_unknown')
        self.flex = FutureFlex(self.cfg, self.store, lambda _: None, transport=uncertain, clock=lambda: 200)
        self.store.upsert_topics([{'id': n, 'title': str(n), 'created_at': '1970-01-01T00:02:30Z',
                                   'first_seen_at': '1970-01-01T00:02:30Z', 'filter': None}
                                  for n in (10, 11)])
        self.assertEqual(self.flex.run()['error'], 'outcome_unknown')
        self.pipeline = mock.Mock(running=False, filter=self.flex)
        env = mock.patch.dict(os.environ, {'LINUXDO_AI_OWNER_TOKEN': TOKEN,
                                          'LINUXDO_AI_OWNER_TOKEN_FILE': '',
                                          'LINUXDO_AI_ALLOWED_ORIGINS': ''})
        env.start()
        self.addCleanup(env.stop)
        self.httpd = server.build_server(self.cfg, server.App(self.cfg, self.store, self.pipeline, lambda _: None))
        self.cfg['port'] = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def request(self, body, token=TOKEN):
        conn = http.client.HTTPConnection('127.0.0.1', self.cfg['port'], timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        try:
            conn.request('POST', ROUTE, json.dumps(body), headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw)
        finally:
            conn.close()

    def payload(self):
        meta = self.store.data['future_flex']
        return {'action': 'quarantine_uncertain', 'expected_pending_ids': list(meta['pending_ids']),
                'expected_attempt_digest': attempt_digest(meta)}

    def test_owner_auth_and_strict_schema_leave_state_unchanged(self):
        payload = self.payload()
        before = copy.deepcopy(self.store.data['future_flex'])
        for token in (None, 'wrong'):
            with self.subTest(token=token):
                self.assertIn(self.request(payload, token)[0], (401, 403))
                self.assertEqual(self.store.data['future_flex'], before)
        invalid = [dict(payload, extra=True), dict(payload, action='retry'),
                   dict(payload, expected_pending_ids='10'), dict(payload, expected_pending_ids=[True]),
                   dict(payload, expected_pending_ids=[10, 10]),
                   dict(payload, expected_attempt_digest='bad')]
        invalid += [{k: v for k, v in payload.items() if k != field} for field in payload]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
                self.assertEqual(self.store.data['future_flex'], before)

    def test_quarantine_is_atomic_persistent_idempotent_and_never_resent(self):
        body = self.payload()
        pending = set(body['expected_pending_ids'])
        with mock.patch.object(self.store, 'save', wraps=self.store.save) as save:
            status, first = self.request(body)
            self.assertEqual(status, 200, first)
            self.assertEqual(save.call_count, 1)
            status, second = self.request(body)
            self.assertEqual(status, 200, second)
            self.assertEqual(save.call_count, 1)
        meta = self.store.data['future_flex']
        self.assertEqual(meta['status'], 'ready')
        self.assertEqual(meta['pending_ids'], [])
        self.assertTrue(pending <= set(meta['excluded_ids']))
        self.assertEqual(Store(self.store.path, self.store.failure_log).data['future_flex'], meta)
        self.assertTrue(any('recover' in key or 'receipt' in key for key in meta), meta)
        calls_before = copy.deepcopy(self.calls)
        self.flex.run()
        self.assertEqual(self.calls, calls_before)
        self.assertTrue(all(self.store.get(tid)['filter'] is None for tid in pending))

    def test_cas_rejects_changed_pending_digest_and_non_unknown(self):
        original = self.payload()
        variants = [dict(original, expected_pending_ids=[999]),
                    dict(original, expected_attempt_digest='0' * 64)]
        for body in variants:
            before = copy.deepcopy(self.store.data['future_flex'])
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 409)
                self.assertEqual(self.store.data['future_flex'], before)
        for status in ('ready', 'backoff', 'auth_disabled'):
            self.store.data['future_flex']['status'] = status
            before = copy.deepcopy(self.store.data['future_flex'])
            with self.subTest(status=status):
                self.assertEqual(self.request(original)[0], 409)
                self.assertEqual(self.store.data['future_flex'], before)

    def test_busy_dispatch_lock_rejects_without_mutation(self):
        body = self.payload()
        before = copy.deepcopy(self.store.data['future_flex'])
        self.flex.lock.acquire()
        try:
            status, _ = self.request(body)
            self.assertEqual(status, 409)
            self.assertEqual(self.store.data['future_flex'], before)
        finally:
            self.flex.lock.release()

    def test_replay_rejected_after_new_attempt(self):
        body = self.payload()
        self.assertEqual(self.request(body)[0], 200)
        meta = self.store.data['future_flex']
        meta.update(status='unknown', pending_ids=body['expected_pending_ids'],
                    last_attempt={'sequence': 'new attempt'})
        before = copy.deepcopy(meta)
        self.assertEqual(self.request(body)[0], 409)
        self.assertEqual(meta, before)

    def test_backfill_release_cannot_release_quarantined_ids(self):
        body = self.payload()
        self.assertEqual(self.request(body)[0], 200)
        meta = self.store.data['future_flex']
        tid = body['expected_pending_ids'][0]
        prior = {'run_id': 'prior', 'status': 'cancelled',
                 'groups': {'g': [{'id': tid}]}, 'receipts': {}}
        data = {'luna': prior, 'luna_history': {}}
        meta.update(backfill_prior_run_id='prior',
                    backfill_release={'unprocessed_groups': ['g']})
        self.assertIn(tid, backfill_exclusions(data, meta))

    def test_replay_after_real_new_attempt_is_conflict(self):
        body = self.payload()
        self.assertEqual(self.request(body)[0], 200)
        self.store.upsert_topics([{'id': 12, 'title': 'new',
                                   'created_at': '1970-01-01T00:02:30Z',
                                   'first_seen_at': '1970-01-01T00:02:30Z', 'filter': None}])
        self.assertEqual(self.flex.run()['error'], 'outcome_unknown')
        self.assertEqual(self.request(body)[0], 409)


    def test_resource_retry_is_atomic_persistent_and_idempotent(self):
        meta = self.store.data['future_flex']
        diagnostic = {'http_status': 429, 'body_read_complete': True,
                      'phase': 'dispatch', 'request_nonce': meta['last_attempt'],
                      'json_shape': 'object_error_object', 'error_code': 'flex_unavailable', 'error_type': 'resource_unavailable'}
        meta['quarantined_ids'] = list(range(100,116))
        meta['excluded_ids'] = list(range(100,116))
        meta['diagnostic'] = copy.deepcopy(diagnostic)
        body = dict(self.payload(), action='retry_proven_resource')
        excluded = copy.deepcopy(meta['excluded_ids'])
        with mock.patch.object(self.store, 'save', wraps=self.store.save) as save:
            status, result = self.request(body)
            self.assertEqual(status, 200, result)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(self.request(body)[0], 200)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(meta['status'], 'backoff')
        self.assertEqual(meta['next_retry'], 260)
        self.assertEqual(meta['pending_ids'], [])
        self.assertEqual(meta['excluded_ids'], excluded)
        self.assertEqual(meta.get('quarantined_ids'), list(range(100,116)))
        self.assertEqual(result['receipt']['old_diagnostic'], diagnostic)
        self.assertEqual(result['receipt']['old_status'], 'unknown')
        self.assertEqual(result['receipt']['action'], body['action'])
        self.assertEqual(Store(self.store.path, self.store.failure_log).data['future_flex'], meta)
        self.assertEqual(self.flex.run()['error'], 'rate_backoff')
        meta['status'] = 'ready'
        self.assertTrue(self.request(body)[1]['idempotent'])
        meta['status'] = 'backoff'
        self.flex.clock = lambda: 260
        prior = len(self.calls)
        self.assertEqual(self.flex.run()['error'], 'outcome_unknown')
        self.assertEqual(len(self.calls), prior + 1)
        self.assertEqual(set(self.calls[-1]), set(body['expected_pending_ids']))
        self.assertEqual(self.flex.run()['error'], 'unknown')
        self.assertEqual(len(self.calls), prior + 1)
        self.assertEqual(self.request(body)[0], 409)

    def test_resource_retry_requires_complete_unambiguous_proof(self):
        meta = self.store.data['future_flex']
        body = dict(self.payload(), action='retry_proven_resource')
        good = {'http_status': 429, 'body_read_complete': True, 'phase': 'dispatch',
                'request_nonce': meta['last_attempt'], 'json_shape': 'object_error_object', 'error_type': 'resource_unavailable'}
        invalid = [None, {}, dict(good, http_status=500), dict(good, body_read_complete=False),
                   dict(good, phase='validation'), dict(good, request_nonce='other'),
                   dict(good, json_shape='object_other'), dict(good, error_type='other'),
                   dict(good, error_code='insufficient_quota'),
                   dict(good, error_code='project_spend_limit_exceeded'),
                   dict(good, error_code='bad secret'),
                   dict(good, error_code='flex_unavailable', error_type='insufficient_quota')]
        for diagnostic in invalid:
            with self.subTest(diagnostic=diagnostic):
                meta['diagnostic'] = diagnostic
                before = copy.deepcopy(meta)
                self.assertEqual(self.request(body)[0], 409)
                self.assertEqual(meta, before)
        meta['diagnostic'] = good
        before = copy.deepcopy(meta)
        self.assertIn(self.request(body, 'wrong')[0], (401, 403))
        self.assertEqual(self.request(dict(body, expected_attempt_digest='0'*64))[0], 409)
        self.assertEqual(self.request(dict(body, expected_pending_ids=[999]))[0], 409)
        self.flex.lock.acquire()
        try:
            self.assertEqual(self.request(body)[0], 409)
        finally:
            self.flex.lock.release()
        self.assertEqual(meta, before)
        self.assertEqual(self.request(body)[0], 200)


if __name__ == '__main__':
    unittest.main()
