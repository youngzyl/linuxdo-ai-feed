"""Bounded OpenAI Responses adapter for the pinned OpenEvolve LLM interface.

OpenEvolve builds each worker client from a pickled ``LLMModelConfig.init_client``.
A lock-owning store, async client, or project key must not be serialized into that
config, argv, or environment. Workers receive a peer-authenticated IPC factory and
talk to one parent service that owns the receipt journal, deadline, and key.

The caller of an in-process service must hold ``ReceiptStore.singleton()`` and keep
that store for the whole run. ``start_isolated_service`` admits the run in a child
that is not the process OpenEvolve later forks. No retries or tier/provider
fallback are performed. This module never places a paid call by itself.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    from openevolve.llm.base import LLMInterface
except ModuleNotFoundError as exc:
    if exc.name not in ('openevolve', 'openevolve.llm', 'openevolve.llm.base'):
        raise
    # Allows offline tests and import before the pinned framework is installed.
    class LLMInterface:
        pass


_SAFE_ERRORS = frozenset({
    'Responses attempt failed or was incomplete',
    'attempt limit or unresolved prior intent',
    'run deadline exceeded',
    'prompt exceeds byte limit',
    'invalid context',
    'invalid message',
    'duplicate request',
    'unauthorized',
    'invalid request',
    'adapter service unavailable',
    'adapter service failed',
})
_RESPONSE_FRAME_LIMIT = 1_000_000
_BLACKHOLE_API_BASE = 'http://127.0.0.1:9'

# These are enumerations, not patterns accepting arbitrary provider text: a key
# or other identifier can itself consist entirely of otherwise plausible tokens.
_DIAGNOSTIC_EXCEPTIONS = frozenset({
    'APIError', 'APIConnectionError', 'APITimeoutError', 'APIStatusError',
    'BadRequestError', 'AuthenticationError', 'PermissionDeniedError',
    'NotFoundError', 'ConflictError', 'UnprocessableEntityError',
    'RateLimitError', 'InternalServerError', 'TimeoutError', 'CancelledError',
    'ValueError', 'RuntimeError', 'OSError',
})
_PROVIDER_CODES = frozenset({
    'insufficient_quota', 'rate_limit_exceeded', 'invalid_api_key',
    'invalid_request_error', 'authentication_error', 'permission_error',
    'server_error', 'internal_error', 'service_unavailable',
    'context_length_exceeded', 'model_not_found', 'billing_hard_limit_reached',
    'tokens', 'requests', 'engine_overloaded', 'unsupported_country_region_territory',
    'resource_unavailable', 'resource_exhausted', 'flex_unavailable',
    'credit_balance_exhausted', 'organization_spend_limit_exceeded', 'project_spend_limit_exceeded',
})


def _field(obj, name):
    try:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    except Exception:
        return None


def _failure_diagnostic(exc, phase, response=None):
    diagnostic = {'phase': phase}
    name = type(exc).__name__
    if name in _DIAGNOSTIC_EXCEPTIONS:
        diagnostic['exception_class'] = name
    status = _field(exc, 'status_code')
    if status is None:
        status = _field(_field(exc, 'response'), 'status_code')
    if type(status) is int and 100 <= status <= 599:
        diagnostic['http_status'] = status
    error = _field(response, 'error')
    if error is None:
        body = _field(exc, 'body')
        error = _field(body, 'error') or body
    for field in ('code', 'type'):
        value = _field(error, field)
        if value is None:
            value = _field(exc, field)
        if type(value) is str and value in _PROVIDER_CODES:
            diagnostic['provider_' + field] = value
    return diagnostic


def _complete_resource_error(exc):
    """Accept only cached, fully consumed bounded 429 JSON; never read a stream."""
    response = _field(exc, 'response')
    if _field(exc, 'status_code') != 429 or _field(response, 'status_code') != 429 or _field(response, 'is_stream_consumed') is not True:
        return None
    try:
        raw = response.content
        headers = response.headers
        declared = headers.get('content-length')
        if not isinstance(raw, bytes) or len(raw) > 65536 or (declared is not None and (not declared.isdigit() or int(declared) != len(raw))):
            return None
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('duplicate')
                result[key] = value
            return result
        obj = json.loads(raw, object_pairs_hook=unique)
        err = obj.get('error') if isinstance(obj, dict) else None
        if not isinstance(err, dict): return None
        code, kind = err.get('code'), err.get('type')
        quota = ('insufficient_quota', 'credit_balance_exhausted', 'organization_spend_limit_exceeded', 'project_spend_limit_exceeded')
        if code in quota or kind in quota: return None
        if code != 'flex_unavailable' and kind != 'resource_unavailable': return None
        return {'body_read_complete': True, 'body_length': len(raw), 'body_sha256': hashlib.sha256(raw).hexdigest(),
                'json_shape': 'object_error_object',
                'provider_code': code if code in _PROVIDER_CODES else None,
                'provider_type': kind if kind in _PROVIDER_CODES else None}
    except Exception:
        return None


def uses_real_llm_interface():
    return LLMInterface.__module__ == 'openevolve.llm.base'


def peer_allowed(uid, expected_uid=None):
    expected = os.getuid() if expected_uid is None else expected_uid
    return isinstance(uid, int) and not isinstance(uid, bool) and uid == expected


def _safe_error(message):
    return message if message in _SAFE_ERRORS else 'Responses attempt failed or was incomplete'


def _raise_for_error(message):
    message = _safe_error(message)
    if message in ('prompt exceeds byte limit', 'invalid context', 'invalid message'):
        raise ValueError(message)
    if message == 'run deadline exceeded':
        raise TimeoutError(message)
    raise RuntimeError(message)


def _read_exact(sock, count):
    chunks = []
    remaining = count
    while remaining:
        block = sock.recv(remaining)
        if not block:
            raise RuntimeError('adapter service unavailable')
        chunks.append(block)
        remaining -= len(block)
    return b''.join(chunks)


def _send_frame(sock, payload, limit):
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(body) > limit:
        raise ValueError('prompt exceeds byte limit')
    sock.sendall(len(body).to_bytes(4, 'big') + body)


def _recv_frame(sock, limit):
    length = int.from_bytes(_read_exact(sock, 4), 'big')
    if length <= 0 or length > limit:
        raise RuntimeError('invalid request')
    return json.loads(_read_exact(sock, length).decode('utf-8'))


def _peer_uid(sock):
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    except OSError:
        return None
    if len(raw) < struct.calcsize('3i'):
        return None
    _pid, uid, _gid = struct.unpack('3i', raw[:struct.calcsize('3i')])
    return uid


def _fingerprint(system_message, messages):
    body = json.dumps({'m': messages, 's': system_message}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class ResponsesAdapter(LLMInterface):
    MODEL = 'gpt-6-luna'
    TIER = 'flex'

    def __init__(self, store, run_id, *, api_key=None, client=None,
                 deadline_seconds=600, deadline_monotonic=None, max_prompt_bytes=100_000):
        if deadline_seconds <= 0 or max_prompt_bytes <= 0:
            raise ValueError('limits must be positive')
        if client is None:
            if not api_key:
                raise ValueError('API key required')
            import httpx2
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key, base_url='https://api.openai.com/v1',
                                 max_retries=0,
                                 http_client=httpx2.AsyncClient(trust_env=False, follow_redirects=False))
        if getattr(client, 'max_retries', None) != 0:
            raise ValueError('client must have zero retries')
        self._client = client
        self._store = store
        self._run_id = run_id
        self._deadline = deadline_monotonic if deadline_monotonic is not None else time.monotonic() + deadline_seconds
        self._max_prompt_bytes = max_prompt_bytes
        self._lock = asyncio.Lock()
        self.last_usage = None

    def __repr__(self):
        return f'ResponsesAdapter(run_id={self._run_id!r})'

    async def generate(self, prompt, **kwargs):
        return await self.generate_with_context('', [{'role': 'user', 'content': prompt}], **kwargs)

    async def generate_with_context(self, system_message, messages, **kwargs):
        # Framework sampling kwargs cannot alter the fixed request policy.
        del kwargs
        if not isinstance(system_message, str) or not isinstance(messages, list):
            raise ValueError('invalid context')
        nonce = getattr(self._store, 'recovery_nonce', None)
        day_nonce = getattr(self._store, 'run_nonce', None)
        if isinstance(day_nonce, str):
            system_message += ('\nIndependent daily experiment; do not reproduce previous unknown generations. Run nonce: ' + day_nonce)
        if isinstance(nonce, str):
            system_message += ('\nIndependent attempt 2: optimize only multimodal choose_batch packing via greedy search; previous unknown generation is abandoned; do not recreate or retry its response.\nIndependent attempt nonce: ' + nonce)
        inputs = []
        if system_message:
            inputs.append({'role': 'system', 'content': system_message})
        for message in messages:
            if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'system') or not isinstance(message.get('content'), str):
                raise ValueError('invalid message')
            inputs.append({'role': message['role'], 'content': message['content']})
        if len(json.dumps(inputs, ensure_ascii=False).encode('utf-8')) > self._max_prompt_bytes:
            raise ValueError('prompt exceeds byte limit')
        async with self._lock:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('run deadline exceeded')
            prompt_sha256 = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            number = self._store.begin_attempt(self._run_id, prompt_sha256=prompt_sha256)
            handle = None
            model = None
            tier = None
            usage = None
            response = None
            phase = 'request'
            try:
                response = await asyncio.wait_for(self._client.responses.create(
                    model=self.MODEL, input=inputs, reasoning={'effort': 'high'},
                    service_tier=self.TIER, max_output_tokens=6000, store=False,
                ), timeout=remaining)
                phase = 'response'
                # Provider-controlled metadata must not carry secrets into receipts.
                # Persist only the pinned policy values and a bounded opaque ID.
                import re
                raw_handle = getattr(response, 'id', None)
                handle = raw_handle if isinstance(raw_handle, str) and re.fullmatch(r'resp_[A-Za-z0-9]{1,128}', raw_handle) else None
                raw_model = getattr(response, 'model', None)
                raw_tier = getattr(response, 'service_tier', None)
                model = self.MODEL if raw_model == self.MODEL else None
                tier = self.TIER if raw_tier == self.TIER else None
                raw_usage = getattr(response, 'usage', None)
                if raw_usage is not None:
                    usage = {key: value for key in ('input_tokens', 'output_tokens', 'total_tokens')
                             if type(value := getattr(raw_usage, key, None)) is int and 0 <= value <= 2**63 - 1}
                text = getattr(response, 'output_text', None)
                if not isinstance(handle, str) or not handle or model != self.MODEL or tier != self.TIER or getattr(response, 'status', None) != 'completed' or not isinstance(text, str) or not text:
                    raise ValueError('response failed policy or completion checks')
            except BaseException as exc:
                # Never persist or propagate provider error strings, which can contain secrets.
                diagnostic = _failure_diagnostic(exc, phase, response)
                proof = _complete_resource_error(exc) if phase == 'request' else None
                if proof is not None:
                    diagnostic.update(proof)
                self._store.record_attempt(self._run_id, number, response_handle=handle,
                    model=model, tier=tier, usage=usage, outcome='capacity_deferred' if proof else 'unknown',
                    diagnostic=diagnostic)
                if not isinstance(exc, Exception):
                    raise
                raise RuntimeError('Responses attempt failed or was incomplete') from None
            self._store.record_attempt(self._run_id, number, response_handle=handle,
                model=model, tier=tier, usage=usage, outcome='success')
            self.last_usage = usage
            return text


class WorkerFactory:
    """Picklable OpenEvolve ``init_client``. Holds a socket path, never a key."""

    def __init__(self, socket_path, rpc_timeout):
        self.socket_path = socket_path
        self.rpc_timeout = rpc_timeout

    def __repr__(self):
        return 'WorkerFactory(socket=hidden)'

    def __call__(self, model_cfg=None):
        del model_cfg
        return IPCResponsesClient(self.socket_path, self.rpc_timeout)


class IPCResponsesClient(LLMInterface):
    def __init__(self, socket_path, rpc_timeout):
        self._socket_path = socket_path
        self._rpc_timeout = rpc_timeout
        self.last_usage = None

    def __repr__(self):
        return 'IPCResponsesClient(socket=hidden)'

    async def generate(self, prompt, **kwargs):
        return await self.generate_with_context('', [{'role': 'user', 'content': prompt}], **kwargs)

    async def generate_with_context(self, system_message, messages, **kwargs):
        del kwargs
        payload = {'op': 'generate', 'system_message': system_message, 'messages': messages}
        try:
            response = await asyncio.to_thread(self._rpc, payload)
        except TimeoutError:
            raise TimeoutError('run deadline exceeded') from None
        except (OSError, json.JSONDecodeError, UnicodeError):
            raise RuntimeError('adapter service unavailable') from None
        if not isinstance(response, dict) or response.get('model') not in (None, ResponsesAdapter.MODEL) or response.get('tier') not in (None, ResponsesAdapter.TIER):
            raise RuntimeError('Responses attempt failed or was incomplete')
        if not response.get('ok'):
            _raise_for_error(response.get('error'))
        text = response.get('text')
        usage = response.get('usage')
        if response.get('model') != ResponsesAdapter.MODEL or response.get('tier') != ResponsesAdapter.TIER or not isinstance(text, str) or not text or (usage is not None and not isinstance(usage, dict)):
            raise RuntimeError('Responses attempt failed or was incomplete')
        self.last_usage = usage
        return text

    def _rpc(self, payload):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._rpc_timeout)
        try:
            sock.connect(self._socket_path)
            _send_frame(sock, payload, _RESPONSE_FRAME_LIMIT)
            return _recv_frame(sock, _RESPONSE_FRAME_LIMIT)
        finally:
            sock.close()


class AdapterService:
    """Parent-owned Responses service. Do not pickle this object."""

    def __init__(self, store, run_id, *, api_key=None, client=None,
                 deadline_seconds=600, deadline_monotonic=None, max_prompt_bytes=100_000):
        if client is None and not api_key:
            raise ValueError('API key required')
        if deadline_seconds <= 0 or max_prompt_bytes <= 0:
            raise ValueError('limits must be positive')
        self._store = store
        self._run_id = run_id
        self._api_key = None if client is not None else api_key
        self._client = client
        self._deadline_seconds = deadline_seconds
        self._deadline_monotonic = deadline_monotonic
        self._max_prompt_bytes = max_prompt_bytes
        self._dir = None
        self._path = None
        self._thread = None
        self._loop = None
        self._stop = None
        self._ready = threading.Event()
        self._start_error = None
        self._timed_out = False

    def __repr__(self):
        return f'AdapterService(run_id={self._run_id!r})'

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    def start(self, socket_path=None):
        if self._thread is not None:
            raise RuntimeError('service already started')
        if socket_path is None:
            self._dir = tempfile.mkdtemp(prefix='oe-responses-')
            os.chmod(self._dir, 0o700)
            self._path = os.path.join(self._dir, 'sock')
        else:
            self._path = os.fspath(socket_path)
            self._dir = os.path.dirname(self._path)
            os.chmod(self._dir, 0o700)
        self._owns_dir = socket_path is None
        self._thread = threading.Thread(target=self._thread_main, name='oe-responses', daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self._start_error = self._start_error or RuntimeError('adapter service failed')
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise error
        return self._path

    def worker_factory(self):
        if not self._path:
            raise RuntimeError('adapter service unavailable')
        return WorkerFactory(self._path, self._deadline_seconds)

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._timed_out

    def stop(self):
        if getattr(self, '_stopped', False):
            return
        self._stopped = True
        loop = self._loop
        stop = self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._cleanup()

    def _cleanup(self):
        path, directory = self._path, self._dir
        self._path = None
        self._dir = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        if directory and getattr(self, '_owns_dir', True):
            try:
                os.rmdir(directory)
            except OSError:
                pass

    def _thread_main(self):
        try:
            asyncio.run(self._amain())
        except Exception as exc:
            self._start_error = exc
            self._ready.set()

    async def _amain(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        key = self._api_key
        self._api_key = None
        adapter = ResponsesAdapter(
            self._store, self._run_id, api_key=key, client=self._client,
            deadline_seconds=self._deadline_seconds, deadline_monotonic=self._deadline_monotonic,
            max_prompt_bytes=self._max_prompt_bytes)
        key = None
        self._gate = asyncio.Lock()
        self._seen = {}
        if adapter._deadline <= time.monotonic():
            raise TimeoutError('run deadline exceeded')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            sock.bind(self._path)
        finally:
            os.umask(old_umask)
        os.chmod(self._path, 0o600)
        sock.listen(8)
        sock.setblocking(False)
        server = await asyncio.start_unix_server(lambda r, w: self._on_connect(adapter, r, w), sock=sock)
        watcher = asyncio.create_task(self._watch(adapter))
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            watcher.cancel()
            server.close()
            await server.wait_closed()
            self._cleanup()

    async def _watch(self, adapter):
        delay = adapter._deadline - time.monotonic()
        if delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        self._timed_out = True
        self._stop.set()

    async def _on_connect(self, adapter, reader, writer):
        try:
            peer = writer.get_extra_info('socket')
            if peer is None or not peer_allowed(_peer_uid(peer)):
                await self._reply(writer, {'ok': False, 'error': 'unauthorized'})
                return
            payload = await self._read_request(adapter, reader)
            await self._reply(writer, await self._dispatch(adapter, payload))
        except Exception:
            try:
                await self._reply(writer, {'ok': False, 'error': 'invalid request'})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(self, adapter, reader):
        remaining = max(0.1, adapter._deadline - time.monotonic())
        header = await asyncio.wait_for(reader.readexactly(4), timeout=min(5, remaining))
        length = int.from_bytes(header, 'big')
        limit = self._max_prompt_bytes + 65536
        if length <= 0 or length > limit:
            raise ValueError('invalid request')
        body = await asyncio.wait_for(reader.readexactly(length), timeout=min(30, remaining))
        payload = json.loads(body.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('invalid request')
        return payload

    async def _dispatch(self, adapter, payload):
        op = payload.get('op')
        if op == 'shutdown':
            self._stop.set()
            return {'ok': True, 'shutdown': True}
        if op != 'generate':
            return {'ok': False, 'error': 'invalid request'}
        async with self._gate:
            return await self._generate(adapter, payload)

    async def _generate(self, adapter, payload):
        if time.monotonic() >= adapter._deadline:
            self._timed_out = True
            self._stop.set()
            return {'ok': False, 'error': 'run deadline exceeded'}
        system_message = payload.get('system_message')
        messages = payload.get('messages')
        if not isinstance(system_message, str) or not isinstance(messages, list):
            return {'ok': False, 'error': 'invalid request'}
        fingerprint = _fingerprint(system_message, messages)
        seen = self._seen
        prior = seen.get(fingerprint)
        if prior is not None:
            if prior.get('state') == 'success':
                return {'ok': True, 'text': prior['text'], 'usage': prior['usage'],
                        'model': ResponsesAdapter.MODEL, 'tier': ResponsesAdapter.TIER}
            return {'ok': False, 'error': 'duplicate request'}
        seen[fingerprint] = {'state': 'pending'}
        try:
            text = await adapter.generate_with_context(system_message, messages)
        except TimeoutError:
            seen[fingerprint] = {'state': 'failed'}
            self._timed_out = True
            self._stop.set()
            return {'ok': False, 'error': 'run deadline exceeded'}
        except ValueError as exc:
            seen.pop(fingerprint, None)
            return {'ok': False, 'error': _safe_error(str(exc))}
        except RuntimeError as exc:
            seen[fingerprint] = {'state': 'failed'}
            return {'ok': False, 'error': _safe_error(str(exc))}
        except Exception:
            seen[fingerprint] = {'state': 'failed'}
            return {'ok': False, 'error': 'Responses attempt failed or was incomplete'}
        seen[fingerprint] = {'state': 'success', 'text': text, 'usage': adapter.last_usage}
        return {'ok': True, 'text': text, 'usage': adapter.last_usage,
                'model': ResponsesAdapter.MODEL, 'tier': ResponsesAdapter.TIER}

    async def _reply(self, writer, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        writer.write(len(body).to_bytes(4, 'big') + body)
        await writer.drain()


def install_worker_client(model_cfg, service):
    """Attach a picklable factory and strip credentials from the model config.

    Call this after OpenEvolve copies shared LLM settings onto the model. A later
    ``update_model_params`` can copy a key back; do not do that before serialization.
    """
    model_cfg.init_client = service.worker_factory()
    model_cfg.api_key = None
    model_cfg.api_base = _BLACKHOLE_API_BASE
    model_cfg.retries = 0
    model_cfg.name = ResponsesAdapter.MODEL
    model_cfg.provider = None
    model_cfg.reasoning_effort = 'high'
    return model_cfg


def isolated_serve_args(store_root, socket_path, deadline_monotonic, max_prompt_bytes):
    return [
        sys.executable, '-m', 'responses_adapter', '--serve',
        '--store', os.fspath(store_root), '--socket', os.fspath(socket_path),
        '--deadline-monotonic', repr(float(deadline_monotonic)),
        '--max-prompt-bytes', str(int(max_prompt_bytes)),
    ]


def _child_env():
    env = os.environ.copy()
    for name in list(env):
        upper = name.upper()
        if any(part in upper for part in ('KEY', 'TOKEN', 'SECRET', 'PASS', 'CREDENTIAL')):
            env.pop(name, None)
    env.pop('OPENAI_API_KEY', None)
    # The child may be started from an unrelated working directory.
    module_dir = os.path.dirname(os.path.abspath(__file__))
    env['PYTHONPATH'] = module_dir + os.pathsep + env.get('PYTHONPATH', '')
    return env


class IsolatedService:
    def __init__(self, proc, socket_path, rpc_timeout, directory):
        self._proc = proc
        self.socket_path = socket_path
        self._rpc_timeout = rpc_timeout
        self._directory = directory

    def __repr__(self):
        return 'IsolatedService(socket=hidden)'

    def worker_factory(self):
        return WorkerFactory(self.socket_path, self._rpc_timeout)

    def stop(self):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            try:
                sock.connect(self.socket_path)
                _send_frame(sock, {'op': 'shutdown'}, 1024)
                _recv_frame(sock, 1024)
            finally:
                sock.close()
        except OSError:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        shutil.rmtree(self._directory, ignore_errors=True)


def start_isolated_service(store_root, api_key=None, *, credential_file=None, deadline_seconds=600, deadline_monotonic=None, max_prompt_bytes=100_000, resume_sha256=None, canary_id=None):
    """Start the trusted service in a child. The key is written to stdin only."""
    if (credential_file is None) == (api_key is None):
        raise ValueError('exactly one credential source required')
    if api_key is not None and (not isinstance(api_key, str) or not api_key):
        raise ValueError('API key required')
    if deadline_seconds <= 0 or max_prompt_bytes <= 0:
        raise ValueError('limits must be positive')
    directory = tempfile.mkdtemp(prefix='oe-responses-')
    os.chmod(directory, 0o700)
    socket_path = os.path.join(directory, 'sock')
    deadline = time.monotonic() + deadline_seconds if deadline_monotonic is None else deadline_monotonic
    args = isolated_serve_args(store_root, socket_path, deadline, max_prompt_bytes)
    if credential_file is not None:
        args.extend(['--credential-file', os.fspath(credential_file)])
    if resume_sha256 is not None:
        args.extend(['--resume-sha256', resume_sha256])
    if canary_id is not None:
        args.extend(['--canary-id', canary_id])
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env())
    try:
        if api_key is not None:
            proc.stdin.write(api_key.encode('utf-8'))
        proc.stdin.close()
        ready = proc.stdout.readline()
        if ready != b'ready\n':
            raise RuntimeError('adapter service failed')
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return IsolatedService(proc, socket_path, deadline_seconds, directory)


def read_credential_file(path):
    """Read a private regular file only in the isolated provider process."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 4096:
            raise ValueError('credential unavailable')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            value = f.read(4097).decode('ascii').strip()
        if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError('credential unavailable')
        return value
    finally:
        os.close(fd)


def serve_from_memory(store_root, socket_path, deadline_monotonic, max_prompt_bytes, api_key, client=None, ready=None, resume_sha256=None, canary_id=None):
    from receipts import ReceiptStore
    store = ReceiptStore(store_root)
    with store.singleton():
        if canary_id is not None:
            if resume_sha256 is not None:
                raise ValueError('incompatible admission modes')
            admitted, receipt = store.admit_canary(canary_id)
            if not admitted:
                sys.stderr.write('duplicate admission\n')
                return 2
        elif resume_sha256 is not None:
            import datetime as dt
            run_id = dt.datetime.now(dt.timezone.utc).date().isoformat()
            receipt = store.owner_resume(run_id, resume_sha256)
        else:
            admitted, receipt = store.admit()
            if not admitted:
                sys.stderr.write('duplicate admission\n')
                return 2
        service = AdapterService(
            store, receipt['run_id'], api_key=api_key, client=client,
            deadline_monotonic=deadline_monotonic, max_prompt_bytes=max_prompt_bytes)
        service.start(socket_path=socket_path)
        if ready is not None:
            ready()
        service.wait()
        timed_out = service._timed_out
        service.stop()
        return 0 if not timed_out else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description='Trusted Responses adapter service')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--store')
    parser.add_argument('--socket')
    parser.add_argument('--deadline-monotonic', type=float)
    parser.add_argument('--max-prompt-bytes', type=int, default=100_000)
    parser.add_argument('--credential-file')
    parser.add_argument('--resume-sha256')
    parser.add_argument('--canary-id')
    args = parser.parse_args(argv)
    if not args.serve or not args.store or not args.socket or args.deadline_monotonic is None:
        parser.error('isolated serve requires --serve --store --socket --deadline-monotonic')
    raw = sys.stdin.buffer.read()
    try:
        if args.credential_file and raw:
            raise ValueError('credential unavailable')
        api_key = read_credential_file(args.credential_file) if args.credential_file else raw.decode('utf-8')
    except (UnicodeError, OSError, ValueError):
        sys.stderr.write('adapter service failed\n')
        return 1
    finally:
        del raw
    if not api_key:
        sys.stderr.write('adapter service failed\n')
        return 1
    try:
        def ready():
            sys.stdout.write('ready\n')
            sys.stdout.flush()
        code = serve_from_memory(
            args.store, args.socket, args.deadline_monotonic, args.max_prompt_bytes, api_key, ready=ready, resume_sha256=args.resume_sha256, canary_id=args.canary_id)
    except Exception:
        sys.stderr.write('adapter service failed\n')
        return 1
    finally:
        api_key = None
    return code


if __name__ == '__main__':
    raise SystemExit(main())
