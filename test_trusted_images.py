import io
import multiprocessing
import os
import resource
import time
import unittest
from unittest.mock import Mock, patch

import trusted_images as m
from PIL import Image


def _limit_probe(url, deadline):
    return repr(resource.getrlimit(resource.RLIMIT_AS)).encode('ascii')


def _run_limit_probe(send):
    m._fetch_inner = _limit_probe
    m._fetch_worker('https://linux.do/', time.monotonic() + 5, send)


def _decode_probe(url, deadline):
    raw = bytes.fromhex(url)
    output = m.sanitize(raw, 'image/jpeg')
    return ('%d,%d' % (len(output), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)).encode('ascii')

def _run_decode_probe(raw, send):
    m._fetch_inner = _decode_probe
    m._fetch_worker(raw.hex(), time.monotonic() + 20, send)


def _blocked_worker(url, deadline, send):
    os.setsid()
    time.sleep(10)


class Tests(unittest.TestCase):
    def test_spawned_worker_hard_address_space_limit(self):
        ctx = multiprocessing.get_context('spawn')
        recv, send = ctx.Pipe(duplex=False)
        worker = ctx.Process(target=_run_limit_probe, args=(send,))
        worker.start()
        send.close()
        try:
            self.assertTrue(recv.poll(5))
            kind, value = recv.recv()
            self.assertEqual(kind, 'ok')
            self.assertEqual(value, repr((128 * 1024 * 1024, 128 * 1024 * 1024)).encode('ascii'))
        finally:
            if worker.is_alive():
                worker.kill()
            worker.join()
            recv.close()

    def test_real_raster_decode_inside_limited_spawned_worker(self):
        raw = self.jpg((2800, 2800));ctx = multiprocessing.get_context('spawn');recv,send=ctx.Pipe(duplex=False)
        worker=ctx.Process(target=_run_decode_probe,args=(raw,send));worker.start();send.close()
        try:
            self.assertTrue(recv.poll(20));kind,value=recv.recv();self.assertEqual(kind,'ok');size,rss=(int(x) for x in value.decode().split(','));self.assertGreater(size,0);self.assertLessEqual(rss,128*1024)
        finally:
            if worker.is_alive():worker.kill()
            worker.join();recv.close()

    def test_blocked_worker_is_killed_at_deadline(self):
        started = time.monotonic()
        with patch.object(m, '_fetch_worker', _blocked_worker):
            with self.assertRaises(TimeoutError):
                m.fetch('https://linux.do/a.jpg', timeout=0.3)
        self.assertLess(time.monotonic() - started, 2)

    def jpg(self, size=(240, 180), fmt='JPEG'):
        b = io.BytesIO()
        Image.new('RGB', size, 'red').save(b, format=fmt)
        return b.getvalue()

    def test_ldstatic_hosts_are_exactly_allowed_and_extracted(self):
        from op_images import extract_op_images
        hosts = ('cdn.ldstatic.com', 'cdn3.ldstatic.com')
        urls = [f'https://{host}/uploads/image.jpg?size=large' for host in hosts]
        for host, url in zip(hosts, urls):
            with self.subTest(host=host):
                self.assertEqual(m.target(url), (host, '/uploads/image.jpg?size=large'))
        html = ''.join(f'<img src="{url}">' for url in urls)
        self.assertEqual(extract_op_images(html), {'images': urls, 'omitted': 0, 'ignored': 0})
        for url in ('https://ldstatic.com/a.jpg', 'https://cdn2.ldstatic.com/a.jpg',
                    'https://cdn.ldstatic.com.evil.example/a.jpg',
                    'http://cdn3.ldstatic.com/a.jpg', 'https://cdn.ldstatic.com:444/a.jpg'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.target(url)

    def test_ldstatic_redirect_to_untrusted_host_is_rejected(self):
        for host in ('cdn.ldstatic.com', 'cdn3.ldstatic.com'):
            with self.subTest(host=host), patch.object(m, 'get_once', return_value=(
                    302, {'location': 'https://evil.example/image.jpg'}, b'')) as get:
                with self.assertRaises(ValueError):
                    m._fetch_inner(f'https://{host}/image.jpg', time.monotonic() + 5)
                self.assertEqual(get.call_count, 1)

    def test_ldstatic_fetch_uses_public_resolution_and_hostname_tls(self):
        for host in ('cdn.ldstatic.com', 'cdn3.ldstatic.com'):
            with self.subTest(host=host):
                sock, tls = Mock(), Mock()
                response = Mock(status=200)
                response.getheaders.return_value = []
                response.read.return_value = b'image'
                with patch.object(m, 'resolve_public', return_value=(2, ('8.8.8.8', 443))) as resolve, \
                     patch.object(m.socket, 'socket', return_value=sock), \
                     patch.object(m.ssl, 'create_default_context') as context, \
                     patch.object(m.http.client, 'HTTPResponse', return_value=response):
                    context.return_value.wrap_socket.return_value = tls
                    self.assertEqual(m.get_once(f'https://{host}/a.jpg')[2], b'image')
                    resolve.assert_called_once_with(host, timeout=m.DNS_TIMEOUT)
                    sock.connect.assert_called_once_with(('8.8.8.8', 443))
                    context.return_value.wrap_socket.assert_called_once_with(sock, server_hostname=host)
                    self.assertIn(f'Host: {host}\r\n'.encode(), tls.sendall.call_args[0][0])

    def test_url_and_dns_reject_private(self):
        for url in ('http://linux.do/test', 'https://linux.do.evil.example/test',
                    'https://user@linux.do/test', 'https://user:password@linux.do/',
                    'https://linux.do:444/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.target(url)
        # localhost resolves locally and must never be accepted as public.
        with self.assertRaises(ValueError):
            m.resolve_public('localhost', timeout=2)

    def test_raster_sanitization_rejects_svg_mismatch_and_tiny(self):
        out = m.sanitize(self.jpg(), 'image/jpeg')
        self.assertEqual(out[:2], b'\xff\xd8')
        for raw, mime in ((b'<svg/>', 'image/svg+xml'), (self.jpg(), 'image/png'),
                          (self.jpg((10, 10)), 'image/jpeg'), (b'not an image', 'image/jpeg'),
                          (self.jpg()[:100], 'image/jpeg'), (self.jpg(), 'text/html'),
                          (b'x' * (m.MAX_BYTES + 1), 'image/jpeg')):
            with self.subTest(mime=mime, size=len(raw)), self.assertRaises(ValueError):
                m.sanitize(raw, mime)

    def test_pixel_cap_before_decode(self):
        with patch.object(m, 'MAX_PIXELS', 100):
            with self.assertRaises(ValueError):
                m.sanitize(self.jpg(), 'image/jpeg')

    def test_op_extract_ignores_icons_caps_and_counts_untrusted(self):
        from op_images import extract_op_images
        html = ('<img class="emoji" src="https://linux.do/emoji/a.png">'
                '<img width="10" height="10" src="https://linux.do/a.png">'
                + ''.join('<img src="https://linux.do/uploads/%s.jpg">' % i for i in range(6))
                + '<img src="https://untrusted.example/a.jpg">')
        r = extract_op_images(html)
        self.assertEqual(len(r['images']), 4)
        self.assertEqual(r['omitted'], 3)
        self.assertEqual(r['ignored'], 2)

    def test_multimodal_mapping_failure_and_cap_are_incomplete(self):
        from multimodal_payload import build_image_parts
        def fake(x):
            if x == 'broken':
                raise ValueError('failed')
            return self.jpg()
        result = build_image_parts([{'images': ['ok', 'broken'], 'images_omitted': 1},
                                    {'images': ['ok2']}], fake)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['attached'], 2)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['omitted'], 1)
        parts = result['parts']
        self.assertIn('index 1', parts[0]['text'])
        self.assertIn('index 2', parts[2]['text'])
        self.assertTrue(parts[1]['image_url'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(parts[3]['type'], 'input_image')

    def test_dns_deadline(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            m.resolve_public('localhost', timeout=0)
        self.assertLess(time.monotonic() - started, 2)

    def test_fetch_deadline(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            m.fetch('https://linux.do/image.jpg', timeout=0)
        self.assertLess(time.monotonic() - started, 2)

    def test_redirect_body_is_not_read(self):
        # get_once is exercised in this process; patches are never expected to
        # cross the spawn boundary used by fetch and the resolver.
        sock, tls = Mock(), Mock()
        response = Mock(status=302)
        response.getheaders.return_value = [('Location', 'https://linux.do/second')]
        response.read.side_effect = AssertionError('redirect body was read')
        with patch.object(m, 'resolve_public', return_value=(2, ('8.8.8.8', 443))), \
             patch.object(m.socket, 'socket', return_value=sock), \
             patch.object(m.ssl, 'create_default_context') as context, \
             patch.object(m.http.client, 'HTTPResponse', return_value=response):
            context.return_value.wrap_socket.return_value = tls
            status, headers, body = m.get_once('https://linux.do/first')
            self.assertEqual(status, 302)
            self.assertEqual(headers['location'], 'https://linux.do/second')
            self.assertEqual(body, b'')
            response.read.assert_not_called()
            sock.connect.assert_called_once_with(('8.8.8.8', 443))
            request = tls.sendall.call_args[0][0].lower()
            self.assertNotIn(b'authorization:', request)
            self.assertNotIn(b'cookie:', request)


if __name__ == '__main__':
    unittest.main()
