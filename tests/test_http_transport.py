"""R10-minimum tests: credentialed requests must never put secrets on curl's argv.

`http_util` uses curl for linux.do because Cloudflare fingerprints the TLS handshake, but
curl receives headers as command-line arguments (`-H "authorization: Bearer ..."`), which
any process in the same namespace can read. So a request that carries Authorization/Cookie
must go through the in-process stdlib transport instead, while public linux.do scraping
keeps the curl fingerprint. TLS verification stays on in both paths.

Run: python3 -m unittest tests.test_http_transport -v
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_util  # noqa: E402

PROVIDER = "https://api.commandcode.ai/provider/v1/chat/completions"
LINUXDO = "https://linux.do/tag/444-tag/444.json"


class TestTransportChoice(unittest.TestCase):
    def setUp(self):
        http_util.reset_cooldown()

    def test_public_request_keeps_the_curl_transport(self):
        self.assertEqual(http_util.transport_for({"accept": "application/json"}), "curl")
        self.assertEqual(http_util.transport_for(None), "curl")
        self.assertEqual(http_util.transport(), "curl")  # legacy signature

    def test_credential_headers_force_the_in_process_transport(self):
        for header in ("authorization", "Authorization", "AUTHORIZATION", "cookie", "Cookie", "x-api-key"):
            with self.subTest(header=header):
                self.assertTrue(http_util.carries_credentials({header: "secret"}))
                self.assertEqual(http_util.transport_for({header: "secret"}), "python")

    def test_env_override_cannot_put_credentials_on_curl(self):
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "curl"}, clear=False):
            self.assertEqual(http_util.transport_for({"authorization": "Bearer x"}), "python")
            self.assertEqual(http_util.transport_for({}), "curl")
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "python"}, clear=False):
            self.assertEqual(http_util.transport_for({}), "python")

    def test_public_headers_are_not_treated_as_credentials(self):
        headers = dict(http_util.BROWSER_HEADERS)
        headers.update({"x-cmd-zdr": "1", "content-type": "application/json"})
        self.assertFalse(http_util.carries_credentials(headers))


class TestRequestRouting(unittest.TestCase):
    """request() must dispatch on the headers it is about to send."""

    def setUp(self):
        http_util.reset_cooldown()

    def _patched(self, curl_result=(200, b"curl", {}), python_result=(200, b"python", {})):
        return (
            mock.patch.object(http_util, "_curl_request", return_value=curl_result),
            mock.patch.object(http_util, "_python_request", return_value=python_result),
        )

    def test_credentialed_request_never_reaches_curl(self):
        curl, python = self._patched()
        with curl as curl_mock, python as python_mock:
            status, body, _ = http_util.request(PROVIDER, method="POST", headers={"authorization": "Bearer sk-secret"}, body=b"{}")
        self.assertEqual(body, b"python")
        curl_mock.assert_not_called()
        python_mock.assert_called_once()
        # the secret only ever existed as an in-process header
        self.assertNotIn("sk-secret", " ".join(str(c) for c in curl_mock.call_args_list))

    def test_cookie_request_never_reaches_curl(self):
        curl, python = self._patched()
        with curl as curl_mock, python as python_mock:
            http_util.request(LINUXDO, headers={"cookie": "session=abc"})
        curl_mock.assert_not_called()
        python_mock.assert_called_once()

    def test_public_request_still_uses_curl(self):
        curl, python = self._patched()
        with curl as curl_mock, python as python_mock:
            status, body, _ = http_util.request(LINUXDO, headers=http_util.BROWSER_HEADERS)
        self.assertEqual(body, b"curl")
        python_mock.assert_not_called()
        curl_mock.assert_called_once()

    def test_curl_helper_refuses_credentials_as_defence_in_depth(self):
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util._curl_request(PROVIDER, method="POST", headers={"authorization": "Bearer sk-secret"}, body=None, timeout=5)
        self.assertIn("curl", str(ctx.exception).lower())
        self.assertNotIn("sk-secret", str(ctx.exception))

    def test_no_secret_is_placed_on_argv_even_when_curl_is_forced(self):
        """End-to-end: with the curl binary present, a bearer request spawns no subprocess."""
        with mock.patch.object(http_util.subprocess, "run") as run_mock, mock.patch.object(
            http_util, "_python_request", return_value=(200, b"ok", {})
        ):
            http_util.request(PROVIDER, method="POST", headers={"authorization": "Bearer sk-secret"}, body=b"{}")
        run_mock.assert_not_called()


class _FakeResponse:
    """Minimal urllib response stand-in."""

    status = 200
    headers = {}

    def read(self):
        return b"[]"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTlsVerification(unittest.TestCase):
    def test_module_never_disables_certificate_verification(self):
        source = inspect.getsource(http_util).lower()
        for forbidden in ("_create_unverified_context", "verify=false", "check_hostname = false", "ssl._create_default_https_context = ssl._create_unverified"):
            self.assertNotIn(forbidden, source, f"{forbidden} would disable TLS verification")

    def test_public_python_transport_uses_the_default_verified_context(self):
        """The public path keeps calling urlopen, with no explicit SSL context."""
        calls = {}

        def fake_urlopen(req, timeout=None, **kwargs):
            calls["req"] = req
            calls["kwargs"] = kwargs
            return _FakeResponse()

        with mock.patch.object(http_util.urllib.request, "urlopen", side_effect=fake_urlopen):
            status, raw, _ = http_util._python_request(LINUXDO, method="GET", headers={"accept": "application/json"}, body=None, timeout=5)
        self.assertEqual(status, 200)
        self.assertNotIn("context", calls["kwargs"], "an explicit SSL context could bypass verification")

    def test_credentialed_python_transport_uses_the_refusing_opener(self):
        """Credentialed requests go through the opener, never the redirect-following urlopen.

        The opener's TLS handler keeps the default (verifying) context - asserted in
        tests.test_http_redirect - and no explicit context is ever passed here.
        """
        calls = {}

        def fake_open(req, timeout=None, **kwargs):
            calls["req"] = req
            calls["kwargs"] = kwargs
            return _FakeResponse()

        opener = mock.Mock()
        opener.open = fake_open
        with mock.patch.object(http_util, "_credentialed_opener", return_value=opener), mock.patch.object(
            http_util.urllib.request, "urlopen"
        ) as urlopen_mock:
            status, raw, _ = http_util._python_request(LINUXDO, method="GET", headers={"authorization": "Bearer x"}, body=None, timeout=5)
        self.assertEqual(status, 200)
        urlopen_mock.assert_not_called()
        self.assertNotIn("context", calls["kwargs"], "an explicit SSL context could bypass verification")
        self.assertEqual(calls["req"].get_header("Authorization"), "Bearer x")


if __name__ == "__main__":
    unittest.main()
