import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from receipts import ReceiptStore
from responses_adapter import ResponsesAdapter
from daily import attempt_resolved


class RecoveryTests(unittest.TestCase):
    def test_cas_idempotent_distinct_scope_and_cap(self):
        with tempfile.TemporaryDirectory() as root:
            today = dt.datetime.now(dt.timezone.utc).date().isoformat()
            path = Path(root) / (today + '.json')
            store = ReceiptStore(root)
            with store.singleton():
                _, data = store.admit()
                store.begin_attempt(today, prompt_sha256='a' * 64)
                store.record_attempt(today, 1, response_handle=None, model=None, tier=None, usage=None, outcome='unknown')
            before = path.read_bytes()
            old_hash = hashlib.sha256(before).hexdigest()
            with ReceiptStore(root).singleton() as store:
                with self.assertRaises(ValueError):
                    store.owner_abandon(today, '0' * 64)
                self.assertEqual(path.read_bytes(), before)
                result = store.owner_abandon(today, old_hash)
                self.assertEqual(result['attempts'][0]['outcome'], 'unknown')
                self.assertEqual(result['attempts'][0]['disposition'], 'abandoned_no_retry')
                self.assertTrue(attempt_resolved(result['attempts'][0]))
                again = path.read_bytes()
                self.assertEqual(store.owner_abandon(today, old_hash), result)
                self.assertEqual(path.read_bytes(), again)
                new_hash = hashlib.sha256(again).hexdigest()
            with ReceiptStore(root).singleton() as store:
                with self.assertRaises(ValueError):
                    store.owner_resume(today, old_hash)
                store.owner_resume(today, new_hash)
                with self.assertRaises(ValueError):
                    store.begin_attempt(today, prompt_sha256='a' * 64)
                class Client:
                    max_retries = 0
                    def __init__(self): self.responses = self; self.calls = []
                    async def create(self, **kwargs):
                        self.calls.append(kwargs)
                        intent = json.loads(path.read_text())['attempts'][1]
                        payload = json.dumps(kwargs['input'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
                        self_test.assertEqual(intent['prompt_sha256'], hashlib.sha256(payload).hexdigest())
                        self_test.assertEqual(intent['outcome'], 'pending')
                        self_test.assertIn('Independent attempt 2: optimize only multimodal choose_batch packing via greedy search; previous unknown generation is abandoned; do not recreate or retry its response.', kwargs['input'][0]['content'])
                        self_test.assertIn(result['attempts'][0]['recovery_nonce'], kwargs['input'][0]['content'])
                        return SimpleNamespace(id='resp_test', model='gpt-6-luna', service_tier='flex', status='completed', output_text='code', usage=None)
                self_test = self
                client = Client()
                adapter = ResponsesAdapter(store, today, client=client)
                self.assertEqual(asyncio.run(adapter.generate('original fixture')), 'code')
                with self.assertRaises(RuntimeError):
                    asyncio.run(adapter.generate('another different callback'))
                self.assertEqual(len(client.calls), 1)
                saved = json.loads(path.read_text())
                self.assertEqual([a['outcome'] for a in saved['attempts']], ['unknown', 'success'])
                self.assertEqual(saved['attempts'][1]['number'], 2)
                self.assertNotEqual(saved['attempts'][1]['prompt_sha256'], 'a' * 64)
            with ReceiptStore(root).singleton() as store:
                with self.assertRaises(ValueError):
                    store.owner_resume(today, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_recovered_service_ipc_one_dispatch(self):
        import os, socket, threading, time
        from responses_adapter import serve_from_memory, WorkerFactory, _send_frame, _recv_frame
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as sockdir:
            today = dt.datetime.now(dt.timezone.utc).date().isoformat()
            path = Path(root) / (today + '.json')
            with ReceiptStore(root).singleton() as store:
                store.admit()
                store.begin_attempt(today)
                store.record_attempt(today, 1, response_handle=None, model=None, tier=None, usage=None, outcome='unknown')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                store.owner_abandon(today, digest)
            resume = hashlib.sha256(path.read_bytes()).hexdigest()
            class Client:
                max_retries = 0
                def __init__(self): self.responses = self; self.calls = []
                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    return SimpleNamespace(id='resp_test', model='gpt-6-luna', service_tier='flex', status='completed', output_text='code', usage=None)
            client, ready = Client(), threading.Event()
            sockpath = os.path.join(sockdir, 'sock')
            results = {}
            def serve():
                results['code'] = serve_from_memory(root, sockpath, time.monotonic()+30, 100000, None, client=client, ready=ready.set, resume_sha256=resume)
            thread = threading.Thread(target=serve)
            thread.start()
            self.assertTrue(ready.wait(5))
            worker = WorkerFactory(sockpath, 10)(None)
            self.assertEqual(asyncio.run(worker.generate('distinct')), 'code')
            with self.assertRaises(RuntimeError):
                asyncio.run(worker.generate('second callback'))
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sockpath)
            _send_frame(sock, {'op':'shutdown'}, 1024)
            _recv_frame(sock, 1024)
            sock.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results['code'], 0)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual([a['outcome'] for a in json.loads(path.read_text())['attempts']], ['unknown', 'success'])

    def test_daily_does_not_accept_fake_abandon(self):
        self.assertFalse(attempt_resolved({'outcome': 'unknown', 'disposition': 'abandoned_no_retry'}))


if __name__ == '__main__':
    unittest.main()
