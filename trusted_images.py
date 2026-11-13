"""Untrusted raster fetching in a killable, memory-limited spawned process.

This helper is Linux/POSIX-specific. It deliberately has no on-disk cache.
"""
import http.client
import io
import ipaddress
import multiprocessing
import os
import resource
import signal
import socket
import ssl
import threading
import time
import warnings
from urllib.parse import urljoin, urlsplit, urlunsplit

ALLOWED = {'linux.do', 'cdn.linux.do', 'cdn.ldstatic.com', 'cdn3.ldstatic.com'}
MAX_BYTES = 4_000_000
# Keep decoded buffers within the worker's address-space budget.
MAX_PIXELS = 8_000_000
MAX_EDGE = 1600
MAX_REDIRECTS = 2
MEMORY_LIMIT = 128 * 1024 * 1024
DNS_TIMEOUT = 3.0
FETCH_TIMEOUT = 20.0


def target(url):
    try:
        p = urlsplit(url)
        host, port = p.hostname, p.port
    except (ValueError, TypeError):
        raise ValueError('invalid_url') from None
    if (p.scheme != 'https' or not host or host not in ALLOWED or
            p.username is not None or p.password is not None or port not in (None, 443)):
        raise ValueError('untrusted_url')
    if any(ord(x) < 32 or ord(x) == 127 for x in url):
        raise ValueError('invalid_url')
    path = urlunsplit(('', '', p.path or '/', p.query, ''))
    return host, path


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('deadline_exceeded')
    return value


def _resolver(host, send):
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if len(answers) > 64:
            raise ValueError('too_many_addresses')
        send.send(('ok', answers))
    except Exception:
        send.send(('error', 'dns_error'))
    finally:
        send.close()


def resolve_public(host, timeout=DNS_TIMEOUT):
    deadline = time.monotonic() + timeout
    _remaining(deadline)
    ctx = multiprocessing.get_context('spawn')
    recv, send = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_resolver, args=(host, send), daemon=True)
    try:
        worker.start()
        send.close()
        if not recv.poll(_remaining(deadline)):
            raise TimeoutError('dns_timeout')
        try:
            kind, answers = recv.recv()
        except EOFError:
            raise ValueError('dns_error') from None
        if kind != 'ok':
            raise ValueError(answers)
    finally:
        recv.close()
        send.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join()
    addresses = []
    for answer in answers:
        ip = ipaddress.ip_address(answer[4][0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback
                or ip.is_link_local or ip.is_unspecified):
            raise ValueError('nonpublic_address')
        addresses.append((answer[0], answer[4]))
    if not addresses:
        raise ValueError('no_address')
    return addresses[0]


def get_once(url, deadline=None):
    host, path = target(url)
    timeout = min(DNS_TIMEOUT, _remaining(deadline)) if deadline is not None else DNS_TIMEOUT
    family, address = resolve_public(host, timeout=timeout)
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(min(15, _remaining(deadline)) if deadline is not None else 15)
    try:
        sock.connect(address)
        conn = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        try:
            conn.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: ' + host +
                          '\r\nAccept: image/jpeg,image/png,image/webp,image/gif\r\n'
                          'Accept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii'))
            response = http.client.HTTPResponse(conn)
            response.begin()
            headers = {k.lower(): v for k, v in response.getheaders()}
            # Never wait for or buffer redirect/error bodies.
            if response.status != 200:
                return response.status, headers, b''
            if headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('compressed_response')
            length = headers.get('content-length')
            if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise ValueError('too_large')
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('too_large')
            return response.status, headers, body
        finally:
            conn.close()
    finally:
        sock.close()


def _fetch_inner(url, deadline):
    current = url
    for i in range(MAX_REDIRECTS + 1):
        _remaining(deadline)
        target(current)
        status, headers, raw = get_once(current, deadline)
        if status in (301, 302, 303, 307, 308):
            if i == MAX_REDIRECTS or not headers.get('location'):
                raise ValueError('redirect_rejected')
            current = urljoin(current, headers['location'])
            target(current)
            continue
        if status != 200:
            raise ValueError('http_error')
        return sanitize(raw, headers.get('content-type', '').split(';', 1)[0].strip().lower())
    raise ValueError('redirect_rejected')


def _fetch_worker(url, deadline, send):
    try:
        os.setsid()  # A timeout kills the resolver descendant too.
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
        result = _fetch_inner(url, deadline)
        send.send(('ok', result))
    except BaseException as exc:
        try:
            send.send(('timeout' if isinstance(exc, TimeoutError) else 'error',
                       str(exc)[:200] or 'image_worker_failed'))
        except Exception:
            pass
    finally:
        send.close()


def fetch(url, timeout=FETCH_TIMEOUT):
    target(url)
    deadline = time.monotonic() + timeout
    _remaining(deadline)
    ctx = multiprocessing.get_context('spawn')
    recv, send = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_fetch_worker, args=(url, deadline, send))
    result = []
    done = threading.Event()
    def receive():
        try:
            result.append(recv.recv())
        except (EOFError, OSError):
            pass
        finally:
            done.set()
    try:
        worker.start()
        send.close()
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError('deadline_exceeded')
        if not result:
            raise ValueError('image_worker_failed')
        kind, value = result[0]
        if kind == 'timeout':
            raise TimeoutError(value)
        if kind != 'ok':
            raise ValueError(value)
        return value
    finally:
        send.close()
        if worker.pid is not None:
            # Kill the whole private process group, if setsid has completed.
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                if worker.is_alive():
                    worker.kill()
            worker.join()
        recv.close()


def sanitize(raw, mime):
    # Also callable directly for testing; production callers should use fetch.
    from PIL import Image, UnidentifiedImageError
    formats = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP', 'image/gif': 'GIF'}
    if mime not in formats or not raw or len(raw) > MAX_BYTES:
        raise ValueError('mime_or_size')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                if (probe.format != formats[mime] or probe.width * probe.height > MAX_PIXELS
                        or probe.width < 160 or probe.height < 120):
                    raise ValueError('invalid_raster')
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.seek(0)
                image.load()
                image.thumbnail((MAX_EDGE, MAX_EDGE))
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    try:
                        rgb = Image.new('RGB', rgba.size, 'white')
                        rgb.paste(rgba, mask=rgba.getchannel('A'))
                    finally:
                        rgba.close()
                else:
                    rgb = image.convert('RGB')
            try:
                out = io.BytesIO()
                rgb.save(out, format='JPEG', quality=80, optimize=True)
                data = out.getvalue()
            finally:
                rgb.close()
            if len(data) > MAX_BYTES:
                raise ValueError('encoded_too_large')
            return data
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError('invalid_raster') from exc
