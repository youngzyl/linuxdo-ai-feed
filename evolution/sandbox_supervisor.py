"""Container lifetime supervisor producing bounded UNTRUSTED integer IDs.

Candidate code may control its result bytes. This channel does not certify
function-return semantics or fitness: the trusted outer scorer MUST validate
IDs against the original rows and compute every metric independently.

Container PID-1 supervisor for one untrusted choose_batch invocation.

Interface: stdin is one UTF-8 JSON object with exactly code (str) and rows
(list). On success stdout is one JSON array of at most eight integer IDs and
exit status is zero. On any failure stdout is empty and exit status is nonzero.
The container must run as root with only CAP_SETUID and CAP_SETGID, with
no-new-privileges, no network, and a read-only filesystem; this program is not
a substitute for container isolation. Hard deadline includes reading stdin.
"""
import ctypes
import json
import os
import selectors
import signal
import struct
import sys
import time

DEADLINE = 5.0
MAX_INPUT = 1024 * 1024
MAX_RESULT = 4096
MAX_IDS = 8
libc = ctypes.CDLL(None, use_errno=True)


class CapHeader(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32),
                ('inheritable', ctypes.c_uint32)]


def clear_caps():
    # PR_CAP_AMBIENT_CLEAR_ALL, followed by Linux capability version 3 capset.
    if libc.prctl(47, 4, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'ambient capability clear failed')
    header = CapHeader(0x20080522, 0)
    data = (CapData * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), 'capset failed')
    checked = (CapData * 2)()
    if libc.capget(ctypes.byref(header), ctypes.byref(checked)) != 0:
        raise OSError(ctypes.get_errno(), 'capget failed')
    if any(x.effective or x.permitted or x.inheritable for x in checked):
        raise RuntimeError('capabilities remain')
    with open('/proc/self/status', encoding='ascii') as status:
        fields = dict(line.split(':', 1) for line in status if ':' in line)
    if int(fields['CapAmb'].strip(), 16) != 0 or fields['NoNewPrivs'].strip() != '1':
        raise RuntimeError('ambient capabilities or no-new-privileges invalid')


def json_load(raw):
    def reject_constant(_):
        raise ValueError('non-finite JSON')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate key')
            obj[key] = value
        return obj
    return json.loads(raw.decode('utf-8'), parse_constant=reject_constant,
                      object_pairs_hook=unique)


def valid_ids(value):
    return type(value) is list and len(value) <= MAX_IDS and all(type(x) is int for x in value)


def child(payload, result_fd, stdout_fd, stderr_fd):
    try:
        os.setsid()
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        null = os.open('/dev/null', os.O_RDONLY)
        os.dup2(null, 0)
        for fd in set((stdout_fd, stderr_fd, null)) - {0, 1, 2, result_fd}:
            os.close(fd)
        # No inherited descriptors other than the output channels.
        for name in os.listdir('/proc/self/fd'):
            fd = int(name)
            if fd > 2 and fd != result_fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
        clear_caps()
        original_stdout, original_stderr = sys.stdout, sys.stderr
        namespace = {'__name__': '__candidate__'}
        exec(compile(payload['code'], '<candidate>', 'exec'), namespace)
        result = namespace['choose_batch'](payload['rows'])
        # os._exit does not flush Python's standard streams; flush explicitly so
        # buffered print() calls are observed by the supervisor as violations.
        original_stdout.flush()
        original_stderr.flush()
        if not valid_ids(result):
            os._exit(1)
        raw = json.dumps(result, allow_nan=False, separators=(',', ':')).encode('ascii')
        if len(raw) > MAX_RESULT:
            os._exit(1)
        framed = struct.pack('!I', len(raw)) + raw
        while framed:
            framed = framed[os.write(result_fd, framed):]
        os._exit(0)
    except BaseException:
        os._exit(1)


def main():
    if os.getpid() != 1:
        raise RuntimeError('supervisor must run as PID 1 in its own PID namespace')
    if os.getuid() != 0 or os.geteuid() != 0:
        raise RuntimeError('supervisor requires root')
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.setitimer(signal.ITIMER_REAL, DEADLINE)
    started = time.monotonic()
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError('input too large')
    payload = json_load(raw)
    if type(payload) is not dict or set(payload) != {'code', 'rows'} or type(payload['code']) is not str or type(payload['rows']) is not list:
        raise ValueError('invalid input')
    result_r, result_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(result_r); os.close(out_r); os.close(err_r)
        child(payload, result_w, out_w, err_w)
        os._exit(1)
    os.close(result_w); os.close(out_w); os.close(err_w)
    # Parent remains UID 0 but permanently drops effective/permitted/inheritable
    # and ambient capabilities before waiting for or reading from the child.
    clear_caps()
    sel = selectors.DefaultSelector()
    for fd in (result_r, out_r, err_r):
        os.set_blocking(fd, False)
        sel.register(fd, selectors.EVENT_READ)
    result = bytearray()
    while sel.get_map():
        remaining = DEADLINE - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError()
        for key, _ in sel.select(remaining):
            data = os.read(key.fd, MAX_RESULT + 5)
            if not data:
                sel.unregister(key.fd)
                os.close(key.fd)
            elif key.fd != result_r:
                raise ValueError('candidate wrote to stdout or stderr')
            else:
                result.extend(data)
                if len(result) > MAX_RESULT + 4:
                    raise ValueError('result too large')
    _, status = os.waitpid(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise ValueError('candidate failed')
    if len(result) < 4 or struct.unpack('!I', result[:4])[0] != len(result) - 4:
        raise ValueError('invalid result frame')
    value = json_load(bytes(result[4:]))
    if not valid_ids(value):
        raise ValueError('invalid result')
    # Canonical output only; never forward candidate-controlled bytes verbatim.
    sys.stdout.write(json.dumps(value, separators=(',', ':')))
    sys.stdout.flush()
    signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        os._exit(1)
