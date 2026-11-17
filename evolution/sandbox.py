"""Bounded Docker execution returning UNTRUSTED IDs, never fitness.

The fixed host scorer must validate IDs against the original input and compute
all metrics. A candidate can choose its output bytes; no channel is assumed to
certify Python return semantics or make a score trustworthy.
"""
import base64
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import uuid

IMAGE = 'sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db'
ROOT = Path(__file__).resolve().parent
OUTPUT_LIMIT = 16384
INPUT_LIMIT = 1_000_000
DEADLINE_SECONDS = 12
BLOCKED = ROOT / '.sandbox-cleanup-blocked'


class SandboxError(RuntimeError):
    pass


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('invalid constant')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=constant)


def _exchange(cid, payload):
    deadline = time.monotonic() + DEADLINE_SECONDS
    proc = subprocess.Popen(['docker', 'start', '-ai', cid], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sel = selectors.DefaultSelector()
    output, errors = bytearray(), bytearray()
    offset = 0
    try:
        for stream, kind in ((proc.stdin, 'input'), (proc.stdout, 'output'),
                             (proc.stderr, 'error')):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_WRITE if kind == 'input'
                         else selectors.EVENT_READ, kind)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxError('candidate deadline exceeded')
            for event, _ in sel.select(remaining):
                stream, kind = event.fileobj, event.data
                try:
                    if kind == 'input':
                        offset += os.write(stream.fileno(), payload[offset:offset+16384])
                        if offset == len(payload):
                            sel.unregister(stream); stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 4096)
                        if not chunk:
                            sel.unregister(stream); stream.close()
                        else:
                            (output if kind == 'output' else errors).extend(chunk)
                            if len(output) + len(errors) > OUTPUT_LIMIT:
                                raise SandboxError('candidate output exceeded limit')
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    sel.unregister(stream); stream.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or proc.wait(timeout=remaining) != 0 or errors:
            raise SandboxError('candidate execution rejected')
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise SandboxError('candidate deadline exceeded') from None
    finally:
        sel.close()
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _cleanup(cid):
    try:
        removed = subprocess.run(['docker', 'rm', '-f', cid], capture_output=True,
                                 timeout=8, check=False)
        checked = subprocess.run(['docker', 'inspect', '--type=container', cid],
                                 capture_output=True, timeout=8, check=False)
        absent = checked.returncode == 1 and any(x in checked.stderr for x in
                                               (b'No such object', b'No such container'))
        if removed.returncode != 0 or not absent:
            raise SandboxError('container cleanup unverified')
    except (OSError, subprocess.TimeoutExpired, SandboxError):
        BLOCKED.write_text(cid + '\n', encoding='ascii')
        raise SandboxError('container cleanup unverified; further runs blocked') from None


def run_candidate(code, rows):
    """Return a bounded untrusted list; caller MUST run fixed trusted scoring."""
    if BLOCKED.exists():
        raise SandboxError('prior container cleanup requires operator review')
    if not isinstance(code, str) or not isinstance(rows, list):
        raise SandboxError('invalid candidate input')
    payload = json.dumps({'code': code, 'rows': rows}, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    if len(payload) > INPUT_LIMIT:
        raise SandboxError('candidate input exceeded limit')
    source = (ROOT / 'sandbox_supervisor.py').read_bytes()
    encoded = base64.b64encode(source).decode('ascii')
    supervisor = 'import base64;exec(compile(base64.b64decode(' + repr(encoded) + '),"<supervisor>","exec"))'
    name = 'candidate-' + uuid.uuid4().hex
    command = ['docker', 'create', '-i', '--name', name, '--network', 'none',
               '--read-only', '--user', '0:0', '--pids-limit', '16', '--memory', '96m',
               '--cpus', '.25', '--tmpfs', '/tmp:rw,noexec,nosuid,size=4m',
               '--cap-drop', 'ALL', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
               '--security-opt', 'no-new-privileges', IMAGE,
               'python', '-I', '-c', supervisor]
    try:
        created = subprocess.run(command, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        # Reconcile the deterministic name: a lost create response must not
        # strand a container or be mistaken for a successful admission.
        BLOCKED.write_text(name + '\n', encoding='ascii')
        raise SandboxError('container creation uncertain; further runs blocked') from None
    cid = created.stdout.decode('ascii', errors='replace').strip()
    if created.returncode != 0 or not re.fullmatch(r'[0-9a-f]{64}', cid):
        if created.returncode == 0:
            BLOCKED.write_text(name + '\n', encoding='ascii')
        raise SandboxError('container creation rejected')
    try:
        value = _json(_exchange(cid, payload))
        if type(value) is not list or len(value) > 8 or any(type(x) is not int for x in value):
            raise SandboxError('candidate output is not bounded integer IDs')
        return value
    except (ValueError, UnicodeError, RecursionError, OSError):
        raise SandboxError('invalid candidate output') from None
    finally:
        _cleanup(cid)
