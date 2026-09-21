"""Monitor target selection tests (remote deployment cutover readiness).

`scripts/monitor.py` is the deterministic probe a Hermes cron monitor runs. With an explicit
`LINUXDO_AI_HEALTH_URL` it must watch ONLY that endpoint: a remote failure has to read as
source=http/service=down/attention=1/stale=1 even when a fresh, healthy local state.json
exists - falling back there would mask a remote outage. Without the override the historical
behaviour (local HTTP, then the state file) must be unchanged. An unusable configured URL is
rejected loudly (source=config), never treated as "no override".

`scripts/host_monitor_wrapper.py` exports the URL from `deploy/active-target.json` when the
owner has written that file (the wrapper never creates it) and otherwise behaves as before.

Run: python3 -m unittest tests.test_monitor_targets -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REMOTE_URL = "https://tcstw.youngzyl.me:8443/linuxdo-api/health"
HEALTHY = {
    "status": "ok",
    "attention": {"needed": False, "reason": "", "kind": None},
    "last_success_at": None,
    "uptime_s": 30.0,
    "consecutive_failures": 0,
    "counts": {},
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = _load("linuxdo_monitor", ROOT / "scripts" / "monitor.py")
wrapper = _load("linuxdo_wrapper", ROOT / "scripts" / "host_monitor_wrapper.py")


def parse(output: str) -> dict:
    out = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


class MonitorCase(unittest.TestCase):
    """A throwaway project whose state.json is fresh and healthy (the tempting fallback)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / "data").mkdir()
        self.state = self.project / "data" / "state.json"
        self.state.write_text(
            json.dumps(
                {
                    "topics": {},
                    "health": {
                        "consecutive_failures": 0,
                        "filter_consecutive_errors": 0,
                        "fetch": {"ok": True},
                        "filter": {"ok": True, "key_present": True},
                        "last_success_at": "2026-09-21T00:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        os.environ.pop(monitor.HEALTH_URL_ENV, None)
        self.addCleanup(os.environ.pop, monitor.HEALTH_URL_ENV, None)
        self.project_patcher = mock.patch.object(monitor, "ROOT", self.project)
        self.project_patcher.start()
        self.addCleanup(self.project_patcher.stop)
        # the fresh state file must not be consulted unless the probe is in file mode
        self.files_patcher = mock.patch.object(
            monitor, "from_files", side_effect=AssertionError("local file fallback used")
        )
        self.files_patcher.start()
        self.addCleanup(self.files_patcher.stop)

    def run_probe(self) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = monitor.main()
        self.assertEqual(code, 0)
        return parse(buf.getvalue())


class TestExplicitRemoteTarget(MonitorCase):
    def setUp(self):
        super().setUp()
        os.environ[monitor.HEALTH_URL_ENV] = REMOTE_URL

    def test_remote_healthy(self):
        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=_Resp(HEALTHY)) as opener:
            out = self.run_probe()
        self.assertEqual(out["source"], "http")
        self.assertEqual(out["service"], "up")
        self.assertEqual(out["attention"], "0")
        self.assertEqual(out["stale"], "0")
        self.assertEqual(opener.call_args.args[0], REMOTE_URL)
        self.assertLessEqual(opener.call_args.kwargs.get("timeout", 99), 10, "timeout must be bounded")

    def test_remote_healthy_but_attention_needed_still_reports_it(self):
        payload = dict(HEALTHY, attention={"needed": True, "reason": "filter errored 3 cycles in a row", "kind": "filter_failing"})
        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=_Resp(payload)):
            out = self.run_probe()
        self.assertEqual(out["service"], "up")
        self.assertEqual(out["attention"], "1")
        self.assertIn("filter errored", out["last_error"])

    def test_remote_failure_never_falls_back_to_a_healthy_local_file(self):
        """The cutover failure mode: remote down, local snapshot still fresh and green."""
        os.environ[monitor.HEALTH_URL_ENV] = REMOTE_URL
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            out = self.run_probe()
        self.assertEqual(out["source"], "http")
        self.assertEqual(out["service"], "down")
        self.assertEqual(out["attention"], "1")
        self.assertEqual(out["stale"], "1")
        self.assertIn("health_unreachable", out["last_error"])
        self.assertIn("tcstw.youngzyl.me", out["last_error"])
        self.assertNotIn("no_state_file", out["last_error"])

    def test_remote_timeout_is_reported_as_down(self):
        os.environ[monitor.HEALTH_URL_ENV] = REMOTE_URL
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            out = self.run_probe()
        self.assertEqual((out["source"], out["service"], out["attention"], out["stale"]), ("http", "down", "1", "1"))

    def test_remote_garbage_body_is_down_not_healthy(self):
        os.environ[monitor.HEALTH_URL_ENV] = REMOTE_URL
        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=_Resp(b"{not json")):
            out = self.run_probe()
        self.assertEqual(out["service"], "down")
        self.assertEqual(out["attention"], "1")


class TestConfiguredUrlValidation(MonitorCase):
    def test_non_https_and_malformed_urls_are_rejected_without_fallback(self):
        for bad in (
            "http://tcstw.youngzyl.me:8443/linuxdo-api/health",
            "http://127.0.0.1:8791/health",
            "ftp://example.test/health",
            "tcstw.youngzyl.me:8443/linuxdo-api/health",
            "//tcstw.youngzyl.me/health",
            "https://",
            "https://user:pass@tcstw.youngzyl.me/health",
            "not a url",
        ):
            with self.subTest(url=bad):
                os.environ[monitor.HEALTH_URL_ENV] = bad
                with mock.patch.object(monitor.urllib.request, "urlopen") as opener:
                    out = self.run_probe()
                self.assertEqual(out["source"], "config", out)
                self.assertEqual(out["service"], "down")
                self.assertEqual(out["attention"], "1")
                self.assertEqual(out["stale"], "1")
                self.assertIn("bad_health_url", out["last_error"])
                opener.assert_not_called()

    def test_empty_value_means_not_configured(self):
        for empty in ("", "   "):
            with self.subTest(value=repr(empty)):
                os.environ[monitor.HEALTH_URL_ENV] = empty
                with mock.patch.object(monitor.urllib.request, "urlopen", return_value=_Resp(HEALTHY)) as opener:
                    out = self.run_probe()
                self.assertEqual(out["service"], "up")
                self.assertEqual(opener.call_args.args[0], monitor.local_health_url())

    def test_validation_helper_accepts_the_canonical_target(self):
        self.assertIsNone(monitor.health_url_error(REMOTE_URL))
        self.assertIsNotNone(monitor.health_url_error("http://example.test/health"))
        self.assertIsNotNone(monitor.health_url_error("https://user@example.test/health"))


class TestLocalBehaviourPreserved(unittest.TestCase):
    """Without an override the historical behaviour must be byte-for-byte the same."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.tmp_patch = mock.patch.object(monitor, "ROOT", self.project)
        self.tmp_patch.start()
        self.addCleanup(self.tmp_patch.stop)
        os.environ.pop(monitor.HEALTH_URL_ENV, None)
        self.addCleanup(os.environ.pop, monitor.HEALTH_URL_ENV, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_http_is_used_when_no_override_is_set(self):
        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=_Resp(HEALTHY)) as opener:
            buf = io.StringIO()
            with redirect_stdout(buf):
                monitor.main()
        out = parse(buf.getvalue())
        self.assertEqual(out["source"], "http")
        self.assertEqual(out["service"], "up")
        self.assertEqual(opener.call_args.args[0], "http://127.0.0.1:8791/health")

    def test_local_file_fallback_survives_when_no_override_is_set(self):
        (self.project / "data").mkdir()
        (self.project / "data" / "state.json").write_text(
            json.dumps(
                {
                    "topics": {},
                    "health": {
                        "consecutive_failures": 3,
                        "filter": {"key_present": True},
                        "last_success_at": "2026-09-21T00:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("no route")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                monitor.main()
        out = parse(buf.getvalue())
        self.assertEqual(out["source"], "file")
        self.assertEqual(out["service"], "unknown")
        self.assertEqual(out["attention"], "1")
        self.assertEqual(out["consecutive_failures"], "3")


class TestTlsStaysVerified(unittest.TestCase):
    def test_self_signed_https_endpoint_is_refused(self):
        """A forged health endpoint must not be able to look healthy."""
        if not shutil.which("openssl"):
            self.skipTest("openssl not available")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cert, key = tmp / "cert.pem", tmp / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        import http.server
        import ssl

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps(HEALTHY).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))

        # the endpoint answers, but not with a certificate any client should trust
        self.assertIsNone(monitor.fetch_health(f"https://127.0.0.1:{port}/health"))
        self.assertNotIn("verify", inspect_source().lower().replace("verifies", ""))


def inspect_source() -> str:
    return (ROOT / "scripts" / "monitor.py").read_text(encoding="utf-8")


class _Resp:
    """Minimal urlopen stand-in."""

    def __init__(self, payload):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestHostWrapper(unittest.TestCase):
    """The host-side wrapper injects the target URL; it never invents one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / "scripts").mkdir()
        (self.project / "deploy").mkdir()
        (self.project / "scripts" / "monitor.py").write_text(
            "import os\n"
            "print('source=http')\n"
            "print('service=up')\n"
            "print('attention=0')\n"
            "print('consecutive_failures=0')\n"
            "print('stale=0')\n"
            "print('env_health_url=' + (os.environ.get('LINUXDO_AI_HEALTH_URL') or 'unset'))\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        self.env = mock.patch.dict(
            os.environ, {"LINUXDO_AI_DIR": str(self.project)}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def target_path(self) -> Path:
        return self.project / "deploy" / "active-target.json"

    def run_wrapper(self) -> dict:
        """Run the wrapper with the child's output captured into stdout.

        The wrapper hands its stdout to the child process, so a redirect_stdout around
        main() would miss it: run the child with capture and replay it.
        """

        def fake_call(cmd, env=None, **kwargs):
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
            sys.stdout.write(proc.stdout)
            if proc.stderr:
                sys.stdout.write(proc.stderr)
            return proc.returncode

        buf = io.StringIO()
        with mock.patch.object(wrapper.subprocess, "call", side_effect=fake_call):
            with redirect_stdout(buf):
                code = wrapper.main()
        out = parse(buf.getvalue())
        out["_code"] = code
        return out

    def test_no_target_file_keeps_the_previous_behaviour(self):
        out = self.run_wrapper()
        self.assertEqual(out["env_health_url"], "unset")
        self.assertEqual(out["source"], "http")
        self.assertEqual(out["_code"], 7, "the probe's exit code is passed through")

    def test_valid_target_file_is_exported_to_the_probe(self):
        self.target_path().write_text(json.dumps({"health_url": REMOTE_URL}), encoding="utf-8")
        out = self.run_wrapper()
        self.assertEqual(out["env_health_url"], REMOTE_URL)
        self.assertEqual(out["_code"], 7)

    def test_inherited_override_is_dropped_without_a_target_file(self):
        with mock.patch.dict(os.environ, {wrapper.HEALTH_URL_ENV: REMOTE_URL}, clear=False):
            out = self.run_wrapper()
        self.assertEqual(out["env_health_url"], "unset")

    def test_unusable_target_file_fails_loudly_and_never_runs_the_local_probe(self):
        for payload, text in (
            ("{not json", "unreadable"),
            (json.dumps({}), "missing"),
            (json.dumps({"health_url": "http://tcstw.youngzyl.me/health"}), "https"),
            (json.dumps({"health_url": "not-a-url"}), "https"),
            (json.dumps({"health_url": "https://user:pass@x.test/health"}), "https"),
        ):
            with self.subTest(payload=text):
                self.target_path().write_text(payload, encoding="utf-8")
                out = self.run_wrapper()
                self.assertEqual(out["source"], "config")
                self.assertEqual(out["service"], "down")
                self.assertEqual(out["attention"], "1")
                self.assertEqual(out["stale"], "1")
                self.assertIn("bad_active_target", out["last_error"])
                self.assertNotIn("env_health_url", out, "the local probe must not run after a bad cutover file")

    def test_wrapper_never_creates_or_edits_the_target_file(self):
        self.assertFalse(self.target_path().exists())
        self.run_wrapper()
        self.assertFalse(self.target_path().exists(), "the wrapper must not create the cutover file")
        self.target_path().write_text(json.dumps({"health_url": REMOTE_URL}), encoding="utf-8")
        before = self.target_path().read_bytes()
        self.run_wrapper()
        self.assertEqual(self.target_path().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
