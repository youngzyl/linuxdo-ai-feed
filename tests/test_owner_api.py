"""Owner boundary, CORS and mutation-contract tests for the HTTP server.

Contract under test (GitHub Pages frontend + authenticated cross-origin backend):
  * public (no credentials): GET /api/state, /health, /api/queue, static, fixtures
  * owner-only: POST /api/queue, POST /api/feedback, POST /api/refresh,
               GET /api/feedback, GET /api/failures
  * the token is read from LINUXDO_AI_OWNER_TOKEN or LINUXDO_AI_OWNER_TOKEN_FILE only -
    never from config.json, a payload or a log line
  * no token configured => every owner-only endpoint answers 503 (fail closed)
  * a valid bearer is still refused when the Origin is not in
    LINUXDO_AI_ALLOWED_ORIGINS (exact match, no wildcard, no reflection)
  * a request with no Origin at all is allowed only when authenticated
  * CORS: exact ACAO, Vary: Origin, no credentials header, preflight limited to
    GET/POST and Content-Type/Authorization
  * bad JSON -> 400, oversized body -> 413, unexpected verbs -> 405
  * /api/failures answers only the effective GET (do_HEAD maps to GET) and OPTIONS; any other
    dispatched method is a 405 before the owner gate and never reads the failure feed
  * health must not echo raw errors or any credential

Everything runs against a throwaway Store and a loopback server on port 0.
Run: python3 -m unittest tests.test_owner_api -v
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import server as server_mod  # noqa: E402
from store import Store  # noqa: E402

OWNER_TOKEN = "test-owner-token-do-not-log-9f3a"
PAGES_ORIGIN = "https://youngzyl.github.io"
LOCAL_ORIGIN = "http://127.0.0.1"
RAW_SECRET = "sk-raw-upstream-key-must-not-leak"

CFG = {
    "host": "127.0.0.1",
    "port": 0,
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444.json",
    "tag_page_url": "https://linux.do/tag/444-tag/444",
    "site_url": "https://linux.do",
    "attention_threshold": 3,
    "filter_consecutive_error_threshold": 3,
    "filter": {"model": "deepseek/deepseek-v4.1-flash", "prompt_version": "v1"},
}


class OwnerApiCase(unittest.TestCase):
    """One loopback server per test; the environment is patched, never the real one."""

    env: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.store.upsert_topics(
            [
                {"id": 11, "title": "picked one", "created_at": "2026-09-21T00:00:00Z", "tags": ["人工智能"]},
                {"id": 22, "title": "rejected one", "created_at": "2026-09-21T00:01:00Z", "tags": ["人工智能"]},
            ]
        )
        self.store.set_verdict(11, {"valuable": True, "score": 80, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"})
        self.store.set_verdict(22, {"valuable": False, "score": 10, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"})
        self.store.save()
        self.logs: list[str] = []
        self.cfg = dict(CFG)
        app = server_mod.App(self.cfg, self.store, mock.Mock(running=False), self.logs.append)
        env = {
            "LINUXDO_AI_OWNER_TOKEN": None,
            "LINUXDO_AI_OWNER_TOKEN_FILE": None,
            "LINUXDO_AI_ALLOWED_ORIGINS": "",
        }
        env.update(self.env)
        patcher = mock.patch.dict(os.environ, {k: ("" if v is None else v) for k, v in env.items()}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = server_mod.build_server(self.cfg, app)
        self.port = self.httpd.server_address[1]
        self.cfg["port"] = self.port  # the default allowlist follows the bound port
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def req(self, method, path, body=None, *, token=None, origin=None, raw_body=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(extra_headers or {})
        payload = None
        if raw_body is not None:
            payload = raw_body
            headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            payload = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), raw
        finally:
            conn.close()

    def raw_request(self, path, body, headers) -> bytes:
        """Send one request over a raw socket and return every byte the server writes.

        Needed where http.client would either raise (server hangs up on an oversized body)
        or only surface the first of several responses (which is how the fail-open bug in a
        refusal path became visible).
        """
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        head = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{self.port}", f"Content-Length: {len(payload)}"]
        head += [f"{k}: {v}" for k, v in headers.items()]
        request = ("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + payload
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            try:
                sock.sendall(request)
            except OSError:
                pass  # the server may answer and hang up mid-body (oversized case)
            sock.settimeout(2.5)
            got = b""
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
            return got
        finally:
            sock.close()

    def body(self, response) -> dict:
        try:
            return json.loads(response[2].decode("utf-8"))
        except Exception:
            return {}


class TestPublicReads(OwnerApiCase):
    """GET state/health/queue stay anonymous, and fixture/static GETs are unchanged."""

    def test_public_reads_work_without_credentials(self):
        status, headers, raw = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.body((status, headers, raw))["topics"]), 2)
        self.assertEqual(self.req("GET", "/health")[0], 200)
        status, _, raw = self.req("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(self.body((status, {}, raw))["queue"], [])

    def test_disallowed_origin_on_a_public_read_gets_no_cors_grant(self):
        status, headers, _ = self.req("GET", "/api/state", origin="https://evil.example")
        self.assertEqual(status, 200)  # public data, browser enforces CORS
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in headers})
        self.assertNotIn("*", str(headers.get("Access-Control-Allow-Origin", "")))

    def test_health_payload_carries_a_summary(self):
        payload = self.body(self.req("GET", "/health"))
        self.assertIn("summary", payload)
        self.assertIn("auth", payload)
        self.assertIn("attention", payload)


class TestFailClosedWithoutToken(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": None, "LINUXDO_AI_OWNER_TOKEN_FILE": None}

    def test_every_owner_endpoint_is_503_when_no_token_is_configured(self):
        for method, path, body in (
            ("POST", "/api/queue", {"add": 11}),
            ("POST", "/api/feedback", {"id": 11, "vote": "keep"}),
            ("POST", "/api/refresh", {}),
            ("GET", "/api/feedback", None),
            ("GET", "/api/failures", None),
        ):
            with self.subTest(path=path):
                status, _, raw = self.req(method, path, body, token=OWNER_TOKEN)
                self.assertEqual(status, 503, raw[:120])
                self.assertIn("token", self.body((status, {}, raw))["error"])

    def test_writes_do_not_mutate_anything(self):
        self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN)
        self.assertEqual(self.store.queue(), [])

    def test_health_report_says_writes_are_disabled(self):
        payload = self.body(self.req("GET", "/health"))
        self.assertEqual(payload["auth"]["token_configured"], False)
        self.assertEqual(payload["auth"]["writes"], "disabled")


class TestTokenResolution(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN}

    def test_missing_bearer_is_401(self):
        for method, path, body in (
            ("POST", "/api/queue", {"add": 11}),
            ("POST", "/api/feedback", {"id": 11, "vote": "keep"}),
            ("POST", "/api/refresh", {}),
            ("GET", "/api/feedback", None),
            ("GET", "/api/failures", None),
        ):
            with self.subTest(path=path):
                status, _, raw = self.req(method, path, body)
                self.assertEqual(status, 401, raw[:120])
                self.assertNotIn(OWNER_TOKEN, raw.decode("utf-8", "replace"))

    def test_wrong_bearer_is_401_and_mutates_nothing(self):
        status, _, raw = self.req("POST", "/api/queue", {"add": 11}, token="not-the-token")
        self.assertEqual(status, 401)
        self.assertNotIn(OWNER_TOKEN, raw.decode())
        self.assertEqual(self.store.queue(), [])

    def test_token_can_come_from_a_file(self):
        path = self.dir / "owner-token"
        path.write_text(OWNER_TOKEN + "\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"LINUXDO_AI_OWNER_TOKEN": "", "LINUXDO_AI_OWNER_TOKEN_FILE": str(path)},
            clear=False,
        ):
            token, source = config.owner_token()
        self.assertEqual(token, OWNER_TOKEN)
        self.assertTrue(source.startswith("file:"))
        self.assertNotIn(OWNER_TOKEN, source)  # the source label never contains the secret

    def test_unreadable_token_file_fails_closed(self):
        with mock.patch.dict(
            os.environ,
            {"LINUXDO_AI_OWNER_TOKEN": "", "LINUXDO_AI_OWNER_TOKEN_FILE": str(self.dir / "missing")},
            clear=False,
        ):
            token, source = config.owner_token()
        self.assertIsNone(token)
        self.assertTrue(source.startswith("unreadable:"))


class TestOwnerWrites(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN}

    def test_queue_add_then_remove_returns_the_authoritative_queue(self):
        status, _, raw = self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN)
        self.assertEqual(status, 200, raw[:200])
        payload = self.body((status, {}, raw))
        self.assertEqual(payload["queue"], [11])
        self.assertEqual(payload["counts"]["queue"], 1)
        status, _, raw = self.req("POST", "/api/queue", {"remove": 11}, token=OWNER_TOKEN)
        self.assertEqual(self.body((status, {}, raw))["queue"], [])

    def test_queue_is_durable_before_the_response(self):
        self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN)
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(reloaded.queue(), [11])

    def test_keep_vote_moves_the_topic_into_the_queue_in_one_request(self):
        status, _, raw = self.req("POST", "/api/feedback", {"id": 22, "vote": "keep", "note": "架构分析我要"}, token=OWNER_TOKEN)
        self.assertEqual(status, 200, raw[:200])
        payload = self.body((status, {}, raw))
        self.assertEqual(payload["queue"], [22], "keep must queue the topic in the same locked update")
        self.assertEqual(payload["votes"]["keep"], 1)
        self.assertEqual(payload["counts"]["queue"], 1)
        self.assertEqual(payload["topic"]["state"], "picked")
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(reloaded.queue(), [22])
        self.assertEqual(reloaded.get(22)["state"], "picked")

    def test_a_refused_write_sends_exactly_one_response_and_mutates_nothing(self):
        """Regression: a refusal whose helper returned None let the route continue, so a
        denied POST answered 403 *and then* performed the write (fail open)."""
        raw = self.raw_request(
            "/api/queue",
            {"add": 11},
            {"Origin": "https://evil.example", "Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        self.assertEqual(raw.count(b"HTTP/1.1"), 1, raw.decode("utf-8", "replace")[:300])
        self.assertIn(b"403", raw.split(b"\r\n", 1)[0])
        self.assertEqual(self.store.queue(), [], "a refused write must not mutate state")

    def test_skip_vote_removes_the_topic_from_the_queue(self):
        self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN)
        status, _, raw = self.req("POST", "/api/feedback", {"id": 11, "vote": "skip"}, token=OWNER_TOKEN)
        self.assertEqual(status, 200, raw[:200])
        payload = self.body((status, {}, raw))
        self.assertEqual(payload["queue"], [])
        self.assertEqual(payload["topic"]["state"], "rejected")

    def test_clear_vote_is_authenticated(self):
        self.assertEqual(self.req("POST", "/api/feedback", {"id": 11, "vote": "clear"})[0], 401)
        self.assertEqual(self.req("POST", "/api/feedback", {"id": 11, "vote": "clear"}, token=OWNER_TOKEN)[0], 200)

    def test_refresh_requires_a_token(self):
        self.assertEqual(self.req("POST", "/api/refresh", {})[0], 401)
        with mock.patch.object(server_mod.App, "trigger_refresh", return_value={"ok": True, "running": True, "job": {}}):
            self.assertEqual(self.req("POST", "/api/refresh", {}, token=OWNER_TOKEN)[0] in (200, 202), True)

    def test_owner_reads_expose_votes_and_failures(self):
        self.req("POST", "/api/feedback", {"id": 11, "vote": "keep", "note": "私人备注"}, token=OWNER_TOKEN)
        self.store.add_failure("filter_call", f"upstream said {RAW_SECRET}")
        status, _, raw = self.req("GET", "/api/feedback", token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("私人备注", raw.decode("utf-8"))
        status, _, raw = self.req("GET", "/api/failures", token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("filter_call", raw.decode("utf-8"))
        # anonymous callers never see the notes
        self.assertEqual(self.req("GET", "/api/feedback")[0], 401)


class TestCors(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN, "LINUXDO_AI_ALLOWED_ORIGINS": PAGES_ORIGIN}

    def test_allowed_origin_gets_an_exact_acao_and_vary(self):
        status, headers, _ = self.req("GET", "/api/state", origin=PAGES_ORIGIN)
        lower = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(status, 200)
        self.assertEqual(lower.get("access-control-allow-origin"), PAGES_ORIGIN)
        self.assertIn("origin", lower.get("vary", "").lower())
        self.assertNotIn("access-control-allow-credentials", lower)

    def test_writes_from_the_allowed_origin_are_accepted(self):
        status, headers, _ = self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN, origin=PAGES_ORIGIN)
        self.assertEqual(status, 200)
        self.assertEqual({k.lower(): v for k, v in headers.items()}.get("access-control-allow-origin"), PAGES_ORIGIN)

    def test_valid_bearer_from_a_disallowed_origin_is_refused(self):
        for origin in ("https://evil.example", "https://youngzyl.github.io.evil.example", "null"):
            with self.subTest(origin=origin):
                status, _, raw = self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN, origin=origin)
                self.assertEqual(status, 403, raw[:120])
                self.assertEqual(self.req("GET", "/api/feedback", token=OWNER_TOKEN, origin=origin)[0], 403)
        self.assertEqual(self.store.queue(), [], "a refused write must not mutate state")

    def test_default_allowlist_is_the_service_origin_only(self):
        with mock.patch.dict(os.environ, {"LINUXDO_AI_ALLOWED_ORIGINS": ""}, clear=False):
            origins = config.allowed_origins(self.cfg)
        self.assertIn(f"http://127.0.0.1:{self.port}", origins)
        self.assertNotIn("*", origins)
        with mock.patch.dict(os.environ, {"LINUXDO_AI_ALLOWED_ORIGINS": ""}, clear=False):
            status, _, _ = self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)

    def test_no_origin_is_allowed_only_when_authenticated(self):
        self.assertEqual(self.req("POST", "/api/queue", {"add": 11})[0], 401)
        self.assertEqual(self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN)[0], 200)

    def test_preflight_for_a_write_origin(self):
        status, headers, _ = self.req(
            "OPTIONS",
            "/api/queue",
            origin=PAGES_ORIGIN,
            extra_headers={
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, authorization",
            },
        )
        lower = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(status, 204)
        self.assertEqual(lower.get("access-control-allow-origin"), PAGES_ORIGIN)
        self.assertEqual(lower.get("access-control-allow-methods"), "GET, POST")
        self.assertIn("Authorization", lower.get("access-control-allow-headers", ""))
        self.assertIn("origin", lower.get("vary", "").lower())
        self.assertNotIn("access-control-allow-credentials", lower)

    def test_preflight_is_denied_for_disallowed_origin_method_and_header(self):
        cases = {
            "origin": {"origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
            "method": {"origin": PAGES_ORIGIN, "Access-Control-Request-Method": "DELETE"},
            "header": {
                "origin": PAGES_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-owner-token",
            },
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                extra = dict(headers)
                origin = extra.pop("origin")
                status, resp_headers, _ = self.req("OPTIONS", "/api/queue", origin=origin, extra_headers=extra)
                self.assertEqual(status, 403)
                self.assertNotIn("access-control-allow-origin", {k.lower() for k in resp_headers})

    def test_wildcard_entry_never_matches(self):
        with mock.patch.dict(os.environ, {"LINUXDO_AI_ALLOWED_ORIGINS": "*"}, clear=False):
            self.assertEqual(config.allowed_origins(self.cfg), set())
            status, headers, _ = self.req("POST", "/api/queue", {"add": 11}, token=OWNER_TOKEN, origin=PAGES_ORIGIN)
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in headers})


class TestBodyAndMethodLimits(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN}

    def test_bad_json_is_400(self):
        status, _, raw = self.req("POST", "/api/queue", token=OWNER_TOKEN, raw_body="{not json")
        self.assertEqual(status, 400, raw[:120])

    def test_non_object_json_is_400(self):
        status, _, raw = self.req("POST", "/api/feedback", token=OWNER_TOKEN, raw_body="[1,2,3]")
        self.assertEqual(status, 400, raw[:120])

    def test_oversized_body_is_413(self):
        huge = json.dumps({"queue": list(range(20000))})
        raw = self.raw_request(
            "/api/queue",
            {"queue": list(range(20000))},
            {"Content-Type": "application/json", "Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        self.assertGreater(len(huge), 64 * 1024)
        self.assertIn(b"413", raw.split(b"\r\n", 1)[0], raw[:200])
        self.assertIn(b"too large", raw)
        self.assertEqual(self.store.queue(), [])

    def test_queue_needs_one_of_the_documented_shapes(self):
        status, _, raw = self.req("POST", "/api/queue", {"nope": 1}, token=OWNER_TOKEN)
        self.assertEqual(status, 400)
        self.assertIn("expected", self.body((status, {}, raw))["error"])

    def test_unexpected_verbs_are_405_with_allow(self):
        for method, path in (("PUT", "/api/queue"), ("DELETE", "/api/queue"), ("PATCH", "/api/feedback")):
            with self.subTest(method=method):
                status, headers, _ = self.req(method, path, token=OWNER_TOKEN)
                self.assertEqual(status, 405)
                self.assertIn("POST", {k.upper(): v for k, v in headers.items()}.get("ALLOW", ""))

    def test_get_on_a_state_changing_endpoint_is_405(self):
        status, headers, _ = self.req("GET", "/api/refresh")
        self.assertEqual(status, 405)
        self.assertIn("POST", {k.upper(): v for k, v in headers.items()}.get("ALLOW", ""))
        # reads keep working
        self.assertEqual(self.req("GET", "/api/state")[0], 200)
        self.assertEqual(self.req("GET", "/api/feedback", token=OWNER_TOKEN)[0], 200)


class TestNoSecretLeakage(OwnerApiCase):
    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN}

    def test_health_never_echoes_raw_errors_or_credentials(self):
        with mock.patch.dict(os.environ, {"COMMANDCODE_APIKEY": RAW_SECRET}, clear=False):
            self.store.health_update(last_error=f"CommandCode rejected the key ({RAW_SECRET}); token={OWNER_TOKEN}")
            self.store.section_update("filter", last_error=f"HTTP 401 bearer {OWNER_TOKEN} body {RAW_SECRET}", ok=False)
            self.store.record_failure(f"boom {RAW_SECRET} {OWNER_TOKEN}")
            status, _, raw = self.req("GET", "/health")
        text = raw.decode("utf-8", "replace")
        self.assertEqual(status, 200)
        self.assertNotIn(OWNER_TOKEN, text)
        self.assertNotIn(RAW_SECRET, text, "a configured credential leaked through /health")
        payload = json.loads(text)
        self.assertIn("attention", payload)
        self.assertIn("summary", payload)
        # the public error fields are bounded signatures, never the raw upstream text
        for value in (payload.get("last_error"), (payload.get("filter") or {}).get("last_error")):
            if value is not None:
                self.assertLessEqual(len(value), 70)

    def test_no_response_or_log_line_contains_the_token(self):
        for method, path, body in (
            ("POST", "/api/queue", {"add": 11}),
            ("POST", "/api/feedback", {"id": 11, "vote": "keep"}),
            ("GET", "/api/feedback", None),
            ("GET", "/api/failures", None),
            ("POST", "/api/queue", {"add": 11}),
        ):
            status, _, raw = self.req(method, path, body, token=OWNER_TOKEN)
            self.assertNotIn(OWNER_TOKEN, raw.decode("utf-8", "replace"))
        joined = "\n".join(self.logs)
        self.assertNotIn(OWNER_TOKEN, joined)

    def test_unauthorized_bodies_stay_generic(self):
        status, _, raw = self.req("POST", "/api/queue", {"add": 11}, token="wrong")
        text = raw.decode()
        self.assertEqual(status, 401)
        self.assertEqual(self.body((status, {}, raw))["error"], "unauthorized")
        self.assertNotIn("bearer", text.lower())


SENSITIVE_MARKER = "sensitive-owner-marker-7b21-do-not-expose"


class FailuresMethodPolicyCase(OwnerApiCase):
    """Regression: an unauthenticated POST could read the raw failure feed.

    `OWNER_ROUTES` guards the pair ("GET", "/api/failures"), but `_route` dispatched on
    `path == "/api/failures"` for every method in ALLOWED_METHODS. A POST therefore never
    reached the owner gate, fell through to the read branch and returned the same raw
    failures the gate exists to protect - with no token, no bearer and no method check.

    Policy (authored by the lead): /api/failures supports only the effective GET (do_HEAD
    maps to GET) plus OPTIONS. Every other dispatched method is a 405 *before* the owner
    gate and before any payload access, and a refused request that still carries a body
    must not leave the connection able to parse that body as the next request.
    """

    def setUp(self):
        super().setUp()
        self.store.add_failure("filter_call", f"upstream credential {SENSITIVE_MARKER}")

    def spy_on_feed(self):
        """A feed read is a test failure, not an observation: patch asserts via side_effect."""
        return mock.patch.object(
            self.store, "recent_failures", mock.Mock(side_effect=AssertionError("the failure feed was read"))
        )

    def assert_refused(self, response):
        status, headers, raw = response
        text = raw.decode("utf-8", "replace")
        self.assertEqual(status, 405, text[:200])
        lower = {k.lower(): v for k, v in headers.items()}
        allow = lower.get("allow", "")
        self.assertIn("GET", allow)
        self.assertIn("OPTIONS", allow)
        self.assertNotIn(SENSITIVE_MARKER, text, "a refused method leaked the failure feed")
        self.assertEqual(json.loads(text)["error"], "method not allowed")

    @staticmethod
    def read_response(sock) -> bytes:
        """One response, tolerating a reset: the refused request's body is left unread."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return buf
            if not chunk:
                return buf
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        match = re.search(rb"content-length: *(\d+)", head, re.I)
        want = int(match.group(1)) if match else 0
        while len(rest) < want:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest


class TestFailuresMethodPolicyWithoutToken(FailuresMethodPolicyCase):
    """No token configured: the method policy must answer 405, not the fail-closed 503."""

    env = {"LINUXDO_AI_OWNER_TOKEN": None, "LINUXDO_AI_OWNER_TOKEN_FILE": None}

    def test_post_is_405_for_every_credential_variant(self):
        for name, kwargs in (("no bearer", {}), ("a bearer", {"token": OWNER_TOKEN})):
            with self.subTest(name=name):
                with self.spy_on_feed() as spy:
                    self.assert_refused(self.req("POST", "/api/failures", **kwargs))
                self.assertEqual(spy.call_count, 0, "a refused method read the failure feed")

    def test_post_with_a_body_is_405_over_a_raw_socket(self):
        body = json.dumps({"n": 1}).encode("utf-8")
        request = (
            f"POST /api/failures HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode("utf-8") + body
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(request)
        first = self.read_response(sock)
        # Loud, not conditional: the wire probe shows the 405 arrives intact and the server
        # then hangs up. If a reset ever swallowed it, this must fail, not silently skip.
        self.assertIn(b"405", first.split(b"\r\n", 1)[0], first[:200])
        self.assertNotIn(SENSITIVE_MARKER.encode("utf-8"), first)
        follow_up = f"GET /api/failures HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode("utf-8")
        try:
            sock.sendall(follow_up)
        except OSError:
            return  # already closed: the refused request ended the connection
        sock.settimeout(2.0)
        try:
            trailing = sock.recv(65536)
        except OSError:
            trailing = b""
        self.assertEqual(trailing, b"", "the connection was reused after an unread body")

    def test_get_and_head_still_fail_closed_through_the_gate(self):
        self.assertEqual(self.req("GET", "/api/failures")[0], 503)
        status, _, raw = self.req("HEAD", "/api/failures")
        self.assertEqual(status, 503)
        self.assertEqual(raw, b"")

    def test_options_preflight_is_still_supported(self):
        status, headers, _ = self.req(
            "OPTIONS",
            "/api/failures",
            origin=f"http://127.0.0.1:{self.port}",
            extra_headers={"Access-Control-Request-Method": "GET"},
        )
        lower = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(status, 204)
        self.assertIn("GET", lower.get("access-control-allow-methods", ""))


class TestFailuresMethodPolicyWithToken(FailuresMethodPolicyCase):
    """Token configured: the 405 wins over 401/403 for a POST; GET and HEAD still work."""

    env = {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN}

    def test_post_is_405_for_no_wrong_and_valid_bearer(self):
        cases = (
            ("no bearer", {}),
            ("wrong bearer", {"token": "not-the-token"}),
            ("valid bearer", {"token": OWNER_TOKEN}),
            ("valid bearer, disallowed origin", {"token": OWNER_TOKEN, "origin": "https://evil.example"}),
        )
        for name, kwargs in cases:
            with self.subTest(name=name):
                with self.spy_on_feed() as spy:
                    self.assert_refused(self.req("POST", "/api/failures", **kwargs))
                self.assertEqual(spy.call_count, 0, "a refused method read the failure feed")

    def test_a_refused_post_never_mutates_the_failure_feed(self):
        before = len(self.store.failures)
        with self.spy_on_feed():
            self.req("POST", "/api/failures", {"n": 1}, token=OWNER_TOKEN)
        self.assertEqual(len(self.store.failures), before)

    def test_post_variants_all_hit_the_same_guard(self):
        """The guard sits on the same decoded path the read branch uses, so a query string or
        a percent-encoded spelling cannot route around it."""
        for path in ("/api/failures", "/api/failures?n=1", "/api/failures?n=not-a-number", "/api/%66ailures"):
            with self.subTest(path=path):
                with self.spy_on_feed() as spy:
                    self.assert_refused(self.req("POST", path, token=OWNER_TOKEN))
                self.assertEqual(spy.call_count, 0, "a refused method read the failure feed")

    def test_get_still_passes_through_the_owner_gate(self):
        self.assertEqual(self.req("GET", "/api/failures")[0], 401)
        self.assertEqual(self.req("GET", "/api/failures", token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/api/failures", token=OWNER_TOKEN, origin="https://evil.example")[0], 403)
        status, _, raw = self.req("GET", "/api/failures", token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn(SENSITIVE_MARKER, raw.decode("utf-8"), "the authenticated read must not change")

    def test_head_maps_to_get_and_is_still_gated(self):
        self.assertEqual(self.req("HEAD", "/api/failures")[0], 401)
        status, headers, raw = self.req("HEAD", "/api/failures", token=OWNER_TOKEN)
        lower = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"", "HEAD must not carry a body")
        self.assertGreater(int(lower["content-length"]), 0)
        self.assertNotIn("access-control-allow-origin", lower)  # no Origin was sent


if __name__ == "__main__":
    unittest.main()
