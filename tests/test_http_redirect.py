"""Gate: a credentialed request must never follow a redirect.

urllib's stock redirect handler rebuilds the request for the `Location` target and
re-attaches the original headers (everything except `Content-*`), so a 302 answered to a
credentialed call hands `Authorization` / `Cookie` to whatever host the response names - and
an `http://` target would also downgrade a https hop. The provider never needs a redirect,
so `http_util._python_request` routes **credentialed** requests through an opener whose
redirect handler refuses *every* redirect (`HttpError(302)` surfaces to the caller), while
the public scraping path keeps its existing behaviour because public fetches legitimately
need redirects.

Fixtures: two loopback plain-HTTP origins - one answers 302 at a target the test chooses,
the second is a capture endpoint that records the headers it is sent. The https direction is
asserted at handler level and by proving the hop is never *attempted* (its port has no
listener, so an attempted hop would surface as a network error instead of HttpError(302)).
No test here disables TLS verification; the source-level checks in test_http_transport.py
stay authoritative for that.

Run: python3 -m unittest tests.test_http_redirect -v
"""
from __future__ import annotations

import shutil
import socket
import ssl
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_util  # noqa: E402

BEARER = "Bearer sk-redirect-canary-do-not-leak"
COOKIE = "session=redirect-canary-do-not-leak"
CAPTURED: list[dict] = []
ANSWERED: list[dict] = []


class _CaptureHandler(BaseHTTPRequestHandler):
    """The endpoint a leaked credential would land on. Records everything it is sent."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - stdlib naming
        CAPTURED.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        body = b"captured"
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


def _redirect_handler(location: str):
    """A fixture origin that answers 302 + Location (and records what it received)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib naming
            ANSWERED.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
            body = b"redirecting"
            self.send_response(302)
            self.send_header("location", location)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def _status_handler(code: int, headers: dict | None = None):
    """A fixture origin that answers a fixed status (401/429) with optional headers."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib naming
            body = b"status body"
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class _Fixture:
    """A loopback HTTP origin on an ephemeral port."""

    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str = "/start") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


def _closed_port() -> int:
    """A port nothing is listening on (bound then released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class RedirectLeakCase(unittest.TestCase):
    def setUp(self):
        CAPTURED.clear()
        ANSWERED.clear()
        http_util.reset_cooldown()
        self._fixtures: list[_Fixture] = []

    def tearDown(self):
        for fixture in self._fixtures:
            fixture.stop()
        http_util.reset_cooldown()

    def fixture(self, handler_cls) -> _Fixture:
        fixture = _Fixture(handler_cls)
        self._fixtures.append(fixture)
        return fixture

    def redirecting_to(self, target: str) -> _Fixture:
        return self.fixture(_redirect_handler(target))


class TestCredentialedRequestsRefuseRedirects(RedirectLeakCase):
    def test_bearer_is_never_replayed_at_the_redirect_target(self):
        capture = self.fixture(_CaptureHandler)
        origin = self.redirecting_to(capture.url("/capture"))

        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/start"), headers={"authorization": BEARER})

        self.assertEqual(ctx.exception.status, 302, "the caller must see the redirect itself")
        self.assertNotIn("sk-redirect-canary", str(ctx.exception))
        self.assertEqual(CAPTURED, [], "the credentialed hop must never be attempted")
        self.assertEqual(len(ANSWERED), 1, "the first origin is the only one contacted")
        self.assertEqual(ANSWERED[0]["headers"].get("authorization"), BEARER)

    def test_cookie_is_never_replayed_at_the_redirect_target(self):
        capture = self.fixture(_CaptureHandler)
        origin = self.redirecting_to(capture.url("/capture"))

        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/start"), headers={"cookie": COOKIE})

        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(CAPTURED, [], "a session cookie must not follow a redirect")
        self.assertEqual(ANSWERED[0]["headers"].get("cookie"), COOKIE)

    def test_redirect_to_an_https_target_is_never_attempted(self):
        """https->... hop stays local: a follow would raise a network error, not 302."""
        port = _closed_port()
        origin = self.redirecting_to(f"https://127.0.0.1:{port}/capture")

        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/start"), headers={"authorization": BEARER})

        self.assertEqual(ctx.exception.status, 302, "following would have failed to connect, not 302")

    def test_env_override_cannot_make_a_credentialed_request_follow(self):
        capture = self.fixture(_CaptureHandler)
        origin = self.redirecting_to(capture.url("/capture"))
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "curl"}, clear=False):
            with self.assertRaises(http_util.HttpError) as ctx:
                http_util.request(origin.url("/start"), headers={"authorization": BEARER})
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(CAPTURED, [])


class TestPublicPathUnchanged(RedirectLeakCase):
    def test_public_python_request_still_follows_the_redirect(self):
        capture = self.fixture(_CaptureHandler)
        origin = self.redirecting_to(capture.url("/capture"))

        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "python"}, clear=False):
            status, body, _ = http_util.request(origin.url("/start"), headers={"accept": "text/plain"})

        self.assertEqual(status, 200)
        self.assertEqual(body, b"captured")
        self.assertEqual(len(CAPTURED), 1, "public redirects keep working exactly as before")
        self.assertEqual(CAPTURED[0]["path"], "/capture")

    @unittest.skipUnless(shutil.which("curl"), "curl transport requires the curl binary")
    def test_public_curl_request_is_still_not_followed(self):
        """No -L was added: the default transport returns the 302 itself, as it always did."""
        capture = self.fixture(_CaptureHandler)
        origin = self.redirecting_to(capture.url("/capture"))

        self.assertEqual(http_util.transport_for({"accept": "*/*"}), "curl")
        status, _, _ = http_util.request(origin.url("/start"), headers={"accept": "*/*"})

        self.assertEqual(status, 302)
        self.assertEqual(CAPTURED, [], "curl still does not follow redirects on its own")


class TestHandlerLevelPolicy(unittest.TestCase):
    def test_handler_refuses_http_to_https_and_https_to_http(self):
        handler = http_util._RefuseAllRedirects()
        req = urllib.request.Request("https://api.example.test/v1/chat/completions", headers={"authorization": BEARER})
        for target in (
            "https://api.example.test/v1/other",
            "http://api.example.test/v1/other",
            "https://other.example.test/v1",
            "http://other.example.test/v1",
        ):
            with self.subTest(target=target):
                self.assertIsNone(
                    handler.redirect_request(req, None, 302, "Found", {"location": target}, target),
                    "every redirect must be refused, not just cross-host ones",
                )

    def test_stock_handler_would_have_replayed_the_credential(self):
        """Documents the leak this gate closes (the reason the override exists)."""
        stock = urllib.request.HTTPRedirectHandler()
        req = urllib.request.Request(
            "https://api.example.test/v1/chat/completions",
            headers={"Authorization": BEARER, "Cookie": COOKIE},
        )
        replayed = stock.redirect_request(req, None, 302, "Found", {}, "https://evil.example.test/capture")
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed.full_url, "https://evil.example.test/capture")
        self.assertEqual(replayed.get_header("Authorization"), BEARER)
        self.assertEqual(replayed.get_header("Cookie"), COOKIE)

    def test_credentialed_opener_replaces_the_redirect_handler_and_keeps_tls_verification(self):
        opener = http_util._credentialed_opener()
        names = [type(h).__name__ for h in opener.handlers]
        self.assertIn("_RefuseAllRedirects", names)
        self.assertNotIn("HTTPRedirectHandler", names, "the stock redirect handler must be gone")

        https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        for handler in https:
            context = getattr(handler, "_context", None)
            if context is None:
                # 3.11 defers to the default context at request time
                self.assertIs(ssl._create_default_https_context, ssl.create_default_context)
            else:
                # 3.12 builds it eagerly from http.client._create_https_context
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
            self.assertNotEqual(
                getattr(context, "verify_mode", ssl.CERT_REQUIRED),
                ssl.CERT_NONE,
                "the opener must never carry an unverified SSL context",
            )


class TestErrorAndRetrySurfacePreserved(RedirectLeakCase):
    """401/429 must keep flowing through the same error surface as before the fix."""

    def test_401_still_surfaces_with_its_status_and_headers(self):
        origin = self.fixture(_status_handler(401, {"www-authenticate": 'Bearer realm="x"'}))
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/api/feedback"), headers={"authorization": BEARER})
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.headers.get("www-authenticate"), 'Bearer realm="x"')
        self.assertFalse(ctx.exception.is_challenge, "401 is not a rate-limit challenge")

    def test_429_still_sets_the_cooldown_and_honours_retry_after(self):
        origin = self.fixture(_status_handler(429, {"retry-after": "30"}))
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/v1/chat/completions"), headers={"authorization": BEARER})
        self.assertEqual(ctx.exception.status, 429)
        self.assertTrue(ctx.exception.is_challenge)
        self.assertEqual(ctx.exception.retry_after, 30.0)
        self.assertGreater(http_util.cooldown_remaining(origin.url("/")), 0.0, "the 429 must still open a cooldown")
        self.assertEqual(http_util.cooldown_level(origin.url("/")), 1)

    def test_403_challenge_handling_is_unchanged(self):
        origin = self.fixture(_status_handler(403, {}, ))
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util.request(origin.url("/x"), headers={"authorization": BEARER})
        self.assertEqual(ctx.exception.status, 403)
        self.assertTrue(ctx.exception.is_challenge)


if __name__ == "__main__":
    unittest.main()
