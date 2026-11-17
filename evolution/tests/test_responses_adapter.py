import asyncio
import os
import pickle
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from dataclasses import asdict
from types import SimpleNamespace

from receipts import ReceiptStore
from responses_adapter import (
    IPCResponsesClient,
    IsolatedService,
    ResponsesAdapter,
    WorkerFactory,
    install_worker_client,
    isolated_serve_args,
    peer_allowed,
    serve_from_memory,
    start_isolated_service,
    uses_real_llm_interface,
)


class FakeClient:
    def __init__(self, response):
        self.responses = self
        self.response = response
        self.calls = []
        self.max_retries = 0

    async def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class AdapterTests(unittest.TestCase):
    def test_request_failure_records_only_allowlisted_diagnostics(self):
        error = RuntimeError('secret message')
        error.status_code = 429
        error.body = {'error': {'code': 'rate_limit_exceeded', 'type': 'invalid_request_error',
                                'message': 'secret message'}}
        store = mock.Mock()
        store.begin_attempt.return_value = 1
        adapter = ResponsesAdapter(store, 'run', client=FakeClient(error))
        with self.assertRaisesRegex(RuntimeError, '^Responses attempt failed or was incomplete$'):
            asyncio.run(adapter.generate('hello'))
        self.assertEqual(store.begin_attempt.call_args.args, ('run',))
        self.assertEqual(len(store.begin_attempt.call_args.kwargs['prompt_sha256']), 64)
        saved = store.record_attempt.call_args.kwargs
        self.assertEqual(saved['outcome'], 'unknown')
        self.assertEqual(saved['diagnostic'], {'phase': 'request', 'http_status': 429,
            'exception_class': 'RuntimeError', 'provider_code': 'rate_limit_exceeded',
            'provider_type': 'invalid_request_error'})
        self.assertNotIn('secret message', repr(saved))

    def test_failure_rejects_arbitrary_identifiers_and_bounds_usage(self):
        response = _completed()
        response.status = 'failed'
        response.error = {'code': 'sk_secretidentifier', 'type': 'secret_identifier'}
        response.usage = SimpleNamespace(input_tokens=3, output_tokens=True, total_tokens=2**100)
        store = mock.Mock()
        store.begin_attempt.return_value = 1
        with self.assertRaises(RuntimeError):
            asyncio.run(ResponsesAdapter(store, 'run', client=FakeClient(response)).generate('hello'))
        saved = store.record_attempt.call_args.kwargs
        self.assertEqual(saved['diagnostic'], {'phase': 'response', 'exception_class': 'ValueError'})
        self.assertEqual(saved['response_handle'], 'resp_1')
        self.assertEqual(saved['usage'], {'input_tokens': 3})
        self.assertNotIn('secret', repr(saved))

    def test_invalid_exception_fields_are_not_persisted(self):
        error = RuntimeError('sk_secret')
        error.status_code = True
        error.code = 'sk_secret'
        error.type = 'sk_secret'
        store = mock.Mock()
        store.begin_attempt.return_value = 1
        with self.assertRaises(RuntimeError):
            asyncio.run(ResponsesAdapter(store, 'run', client=FakeClient(error)).generate('hello'))
        saved = store.record_attempt.call_args.kwargs
        self.assertEqual(saved['diagnostic'], {'phase': 'request', 'exception_class': 'RuntimeError'})
        self.assertNotIn('sk_secret', repr(saved))

    def test_request_is_journaled_and_fixed(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                response = SimpleNamespace(id='resp_1', model='gpt-6-luna', service_tier='flex', status='completed', output_text='code', usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 3}))
                client = FakeClient(response)
                adapter = ResponsesAdapter(store, run['run_id'], client=client, deadline_seconds=10)
                self.assertEqual(asyncio.run(adapter.generate('hello')), 'code')
                self.assertEqual(client.calls[0]['model'], 'gpt-6-luna')
                self.assertEqual(client.calls[0]['service_tier'], 'flex')
                self.assertEqual(client.calls[0]['reasoning'], {'effort': 'high'})
                self.assertEqual(client.calls[0]['max_output_tokens'], 6000)
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'][0]['outcome'], 'success')

    def test_failure_blocks_replay_and_does_not_expose_exception(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(RuntimeError('SECRET'))
                adapter = ResponsesAdapter(store, run['run_id'], client=client)
                with self.assertRaises(Exception) as caught:
                    asyncio.run(adapter.generate('hello'))
                self.assertNotIn('SECRET', str(caught.exception))
                with self.assertRaises(RuntimeError):
                    asyncio.run(adapter.generate('hello'))
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'][0]['outcome'], 'unknown')
            with store.singleton():
                admitted, saved = store.admit()
                self.assertFalse(admitted)
                self.assertEqual(saved['attempts'][0]['outcome'], 'unknown')

    def test_untrusted_response_metadata_not_saved(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                response = SimpleNamespace(id='secret-key-123', model='secret-key-123',
                    service_tier='secret-key-123', status='completed', output_text='code', usage=None)
                client = FakeClient(response)
                with self.assertRaises(RuntimeError):
                    asyncio.run(ResponsesAdapter(store, run['run_id'], client=client).generate('hello'))
                receipt = store._load_active(run['run_id'])[1]
                self.assertNotIn('secret-key-123', repr(receipt))
                self.assertEqual(receipt['attempts'][0]['outcome'], 'unknown')
                self.assertIsNone(receipt['attempts'][0]['response_handle'])

    def test_prompt_limit_blocks_before_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(None)
                adapter = ResponsesAdapter(store, run['run_id'], client=client, max_prompt_bytes=5)
                with self.assertRaises(ValueError):
                    asyncio.run(adapter.generate('hello'))
                self.assertEqual(client.calls, [])
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'], [])

    def test_wrong_tier_is_unknown_and_cannot_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                response = SimpleNamespace(id='resp_1', model='gpt-6-luna', service_tier='default', status='completed', output_text='code', usage=None)
                client = FakeClient(response)
                adapter = ResponsesAdapter(store, run['run_id'], client=client)
                with self.assertRaises(Exception):
                    asyncio.run(adapter.generate('hello'))
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'][0]['outcome'], 'unknown')
                with self.assertRaises(RuntimeError):
                    asyncio.run(adapter.generate('hello'))
                self.assertEqual(len(client.calls), 1)


SECRET = 'sk-test-secret-not-real'


def _completed(text='code'):
    return SimpleNamespace(
        id='resp_1', model='gpt-6-luna', service_tier='flex', status='completed',
        output_text=text, usage=SimpleNamespace(input_tokens=3, output_tokens=4, total_tokens=7))


class ResponsesAdapterService:
    def __init__(self, store, run_id, client, **kwargs):
        from responses_adapter import AdapterService
        self._service = AdapterService(store, run_id, client=client, deadline_seconds=10, **kwargs)

    def __enter__(self):
        self._service.start()
        return self._service

    def __exit__(self, exc_type, exc, tb):
        self._service.stop()
        return False


class IPCTests(unittest.TestCase):
    def test_worker_roundtrip_ignores_framework_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    mode = os.stat(service._path).st_mode
                    self.assertEqual(stat.S_IMODE(mode), 0o600)
                    self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(service._path)).st_mode), 0o700)
                    worker = service.worker_factory()(SimpleNamespace(api_key=SECRET, name='gpt-4o', retries=3))
                    text = asyncio.run(worker.generate_with_context(
                        'system', [{'role': 'user', 'content': 'hello'}],
                        temperature=0.9, seed=7, model='gpt-4o', service_tier='default'))
                self.assertEqual(text, 'code')
                self.assertEqual(worker.last_usage, {'input_tokens': 3, 'output_tokens': 4, 'total_tokens': 7})
                self.assertEqual(client.calls[0]['model'], 'gpt-6-luna')
                self.assertEqual(client.calls[0]['service_tier'], 'flex')
                self.assertEqual(client.calls[0]['reasoning'], {'effort': 'high'})
                self.assertEqual(client.calls[0]['max_output_tokens'], 6000)
                self.assertFalse(client.calls[0]['store'])
                self.assertNotIn('temperature', client.calls[0])
                self.assertNotIn('seed', client.calls[0])
                self.assertNotIn(SECRET, repr(client.calls[0]))

    def test_duplicate_success_is_not_paid_again_and_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    worker = service.worker_factory()(None)
                    self.assertEqual(asyncio.run(worker.generate('hello')), 'code')
                    self.assertEqual(asyncio.run(worker.generate('hello')), 'code')
                    self.assertEqual(len(client.calls), 1)
                    client.response = RuntimeError(SECRET)
                    with self.assertRaises(RuntimeError) as caught:
                        asyncio.run(worker.generate('other'))
                    self.assertNotIn(SECRET, str(caught.exception))
                    with self.assertRaises(RuntimeError):
                        asyncio.run(worker.generate('other'))
                    self.assertEqual(len(client.calls), 2)
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'][1]['outcome'], 'unknown')

    def test_pickled_factory_and_config_omit_key(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    model = SimpleNamespace(
                        api_key=SECRET, name='other', retries=4,
                        api_base='https://api.openai.com/v1', provider='openai', reasoning_effort='low')
                    install_worker_client(model, service)
                    blob = pickle.dumps({'factory': model.init_client, 'api_key': model.api_key, 'api_base': model.api_base})
                    self.assertNotIn(SECRET.encode(), blob)
                    self.assertIsNone(model.api_key)
                    self.assertEqual(model.retries, 0)
                    self.assertEqual(model.name, 'gpt-6-luna')
                    self.assertEqual(model.api_base, 'http://127.0.0.1:9')
                    restored = pickle.loads(blob)['factory']
                    self.assertIsInstance(restored, WorkerFactory)
                    self.assertNotIn(SECRET, repr(restored))
                    text = asyncio.run(restored(SimpleNamespace(api_key=SECRET)).generate('hello'))
                    self.assertEqual(text, 'code')

    def test_subprocess_worker_imports_factory_without_key(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed('from-parent'))
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    factory_path = os.path.join(root, 'factory.pkl')
                    with open(factory_path, 'wb') as handle:
                        pickle.dump(service.worker_factory(), handle)
                    module_dir = os.path.dirname(os.path.abspath(__import__('responses_adapter').__file__))
                    env = os.environ.copy()
                    env.pop('OPENAI_API_KEY', None)
                    env['PYTHONPATH'] = module_dir + os.pathsep + env.get('PYTHONPATH', '')
                    script = (
                        'import asyncio, pickle, sys\n'
                        'from types import SimpleNamespace\n'
                        'factory = pickle.load(open(sys.argv[1], "rb"))\n'
                        'client = factory(SimpleNamespace(api_key="sk-test-secret-not-real", retries=3))\n'
                        'print(asyncio.run(client.generate_with_context("sys", [{"role": "user", "content": "hi"}])))\n'
                    )
                    proc = subprocess.run(
                        [sys.executable, '-c', script, factory_path],
                        cwd=module_dir, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), 'from-parent')
                self.assertNotIn(SECRET, proc.stderr)
                self.assertEqual(client.calls[0]['service_tier'], 'flex')
                self.assertEqual(len(client.calls), 1)

    def test_deadline_blocks_before_paid_call(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with self.assertRaises(TimeoutError):
                    ResponsesAdapterService(store, run['run_id'], client, deadline_monotonic=time.monotonic() - 1).__enter__()
                self.assertEqual(client.calls, [])
                self.assertEqual(store._load_active(run['run_id'])[1]['attempts'], [])

    def test_other_user_cannot_connect(self):
        if os.getuid() != 0:
            self.skipTest('credential drop requires root')
        nobody = 65534
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    script = (
                        'import os, socket, sys\n'
                        'os.setgid(int(sys.argv[2])); os.setuid(int(sys.argv[2]))\n'
                        'sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
                        'try:\n'
                        '    sock.connect(sys.argv[1])\n'
                        'except PermissionError:\n'
                        '    print("denied")\n'
                        'else:\n'
                        '    print("connected")\n'
                    )
                    proc = subprocess.run(
                        [sys.executable, '-c', script, service._path, str(nobody)],
                        capture_output=True, text=True, timeout=15)
                self.assertEqual(proc.stdout.strip(), 'denied', proc.stderr)
                self.assertEqual(client.calls, [])
        self.assertFalse(peer_allowed(nobody, expected_uid=0))
        self.assertTrue(peer_allowed(0, expected_uid=0))

    def test_raw_request_cannot_override_policy_or_inject_key(self):
        import socket
        from responses_adapter import _recv_frame, _send_frame
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed())
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(5)
                    sock.connect(service._path)
                    _send_frame(sock, {
                        'op': 'generate', 'system_message': 'sys',
                        'messages': [{'role': 'user', 'content': 'hello'}],
                        'api_key': SECRET, 'model': 'gpt-4o', 'service_tier': 'default',
                    }, 100000)
                    payload = _recv_frame(sock, 100000)
                    sock.close()
                self.assertTrue(payload['ok'])
                self.assertEqual(client.calls[0]['model'], 'gpt-6-luna')
                self.assertNotIn(SECRET, repr(client.calls))

    def test_isolated_launch_keeps_key_off_argv_and_env(self):
        args = isolated_serve_args('/tmp/store', '/tmp/sock', 12.5, 1000)
        self.assertNotIn(SECRET, '\0'.join(args))
        with mock.patch('responses_adapter.subprocess.Popen') as popen:
            proc = popen.return_value
            proc.stdout.readline.return_value = b'ready\n'
            proc.poll.return_value = None
            handle = start_isolated_service('/tmp/store', SECRET, deadline_monotonic=time.monotonic() + 30)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs['env']
            self.assertNotIn(SECRET, '\0'.join(argv))
            self.assertNotIn(SECRET, '\0'.join(env.values()))
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(proc.stdin.write.call_args.args[0], SECRET.encode())
            self.assertIsInstance(handle, IsolatedService)
            handle.stop()
            proc.terminate.assert_called()

    def test_duplicate_admission_does_not_construct_client(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                store.admit()
            client = FakeClient(_completed())
            code = serve_from_memory(
                root, os.path.join(root, 'missing.sock'), time.monotonic() + 30, 1000, SECRET, client=client)
            self.assertEqual(code, 2)
            self.assertEqual(client.calls, [])

    def test_memory_service_admits_and_serves_without_network(self):
        import socket
        from responses_adapter import _recv_frame, _send_frame
        with tempfile.TemporaryDirectory() as root:
            directory = tempfile.mkdtemp(prefix='oe-responses-')
            os.chmod(directory, 0o700)
            socket_path = os.path.join(directory, 'sock')
            client = FakeClient(_completed('served'))
            ready = threading.Event()
            result = {}

            def run():
                result['code'] = serve_from_memory(
                    root, socket_path, time.monotonic() + 30, 100000, None,
                    client=client, ready=ready.set)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(ready.wait(5))
            worker = WorkerFactory(socket_path, 10)(None)
            self.assertEqual(asyncio.run(worker.generate('hello')), 'served')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            _send_frame(sock, {'op': 'shutdown'}, 1024)
            _recv_frame(sock, 1024)
            sock.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result['code'], 0)
            self.assertEqual(len(client.calls), 1)
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    def test_key_is_not_in_adapter_repr(self):
        import types
        import responses_adapter
        module = types.ModuleType('openai')
        module.constructed = []

        class AsyncOpenAI:
            def __init__(self, api_key, max_retries, base_url, http_client):
                assert base_url == 'https://api.openai.com/v1'
                assert http_client.trust_env is False
                assert http_client.follow_redirects is False
                module.constructed.append({'max_retries': max_retries})
                self.api_key = api_key
                self.max_retries = max_retries

        module.AsyncOpenAI = AsyncOpenAI
        previous = sys.modules.get('openai')
        sys.modules['openai'] = module
        try:
            with tempfile.TemporaryDirectory() as root:
                store = ReceiptStore(root)
                with store.singleton():
                    _, run = store.admit()
                    os.environ.pop('OPENAI_API_KEY', None)
                    adapter = ResponsesAdapter(store, run['run_id'], api_key=SECRET)
                    self.assertNotIn(SECRET, repr(adapter))
                    self.assertNotIn('OPENAI_API_KEY', os.environ)
                    self.assertEqual(adapter._client.max_retries, 0)
                    self.assertEqual(module.constructed[0]['max_retries'], 0)
                    self.assertIs(adapter._client.api_key, SECRET)
        finally:
            if previous is None:
                sys.modules.pop('openai', None)
            else:
                sys.modules['openai'] = previous

    @unittest.skipUnless(uses_real_llm_interface(), 'pinned OpenEvolve is not installed')
    def test_real_ensemble_uses_responses_ipc_not_chat_completions(self):
        from openevolve.config import LLMModelConfig
        from openevolve.llm.base import LLMInterface
        from openevolve.llm.ensemble import LLMEnsemble
        self.assertTrue(issubclass(IPCResponsesClient, LLMInterface))
        self.assertTrue(issubclass(ResponsesAdapter, LLMInterface))
        with tempfile.TemporaryDirectory() as root:
            store = ReceiptStore(root)
            with store.singleton():
                _, run = store.admit()
                client = FakeClient(_completed('evolved'))
                with ResponsesAdapterService(store, run['run_id'], client) as service:
                    model = LLMModelConfig(
                        name='gpt-4o', api_key=SECRET, retries=3,
                        api_base='https://api.openai.com/v1', weight=1)
                    install_worker_client(model, service)
                    serialized = pickle.dumps(asdict(model))
                    self.assertNotIn(SECRET.encode(), serialized)
                    restored = LLMModelConfig(**pickle.loads(serialized))
                    ensemble = LLMEnsemble([restored])
                    ensemble.reseed(1)
                    text = asyncio.run(ensemble.generate_with_context(
                        'sys', [{'role': 'user', 'content': 'mutate'}]))
                    self.assertEqual(text, 'evolved')
                    self.assertEqual(ensemble.last_usage['input_tokens'], 3)
                    self.assertIsInstance(ensemble.models[0], IPCResponsesClient)
                    self.assertEqual(client.calls[0]['model'], 'gpt-6-luna')
                    self.assertEqual(client.calls[0]['service_tier'], 'flex')
                    self.assertNotIn('seed', client.calls[0])
                    self.assertFalse(hasattr(client, 'chat'))
