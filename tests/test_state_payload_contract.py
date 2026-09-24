"""The /api/state ↔ /api/topic/<id> split and the gzip transport contract.

Contract under test (CONTRACT.md §1, §1.1, §5 "Transport"):
  * the DEFAULT /api/state read is the pre-change shape: every row still carries body_text.
    A tab built from an earlier release keeps working across this rollout - it is not
    silently upgraded (and it gets gzip too, so the rollout byte count goes down as well).
  * the opt-in `?view=list` read is the body-free shape: no body_text, `has_body` on every row
  * GET /api/topic/<id> returns the body, 404 JSON for an unknown or non-numeric id, needs no
    owner token, and keeps the /api/state CORS rules
  * gzip: negotiated by Accept-Encoding (explicit gzip/x-gzip, positive wildcard, conservative
    minimum over duplicate same-codec tokens, invalid q = refusal), applied only at >= 1024
    bytes, never twice, with Content-Length on the wire, Vary: Origin + Accept-Encoding, and
    the same headers on HEAD

Everything runs against a throwaway Store and a loopback server on port 0.
Run: python3 -m unittest tests.test_state_payload_contract -v
"""
from __future__ import annotations

import gzip
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as server_mod  # noqa: E402
from store import Store  # noqa: E402

OWNER_TOKEN = "test-owner-token-do-not-log-5c1e"
PAGES_ORIGIN = "https://youngzyl.github.io"
BIG_BODY = "正文内容 " * 400          # > 1024 bytes once encoded as JSON
SMALL_BODY = "短正文"

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


class PayloadContractCase(unittest.TestCase):
    """One loopback server per test; the environment is patched, never the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        rows = [
            {
                "id": 11,
                "title": "has a body",
                "url": "https://linux.do/t/topic/11",
                "created_at": "2026-09-21T00:00:00Z",
                "bumped_at": "2026-09-21T00:00:00Z",
                "reply_count": 3,
                "views": 40,
                "category": "开发调优",
                "tags": ["人工智能"],
                "excerpt": "只拿到摘录",
            },
            {"id": 22, "title": "no body at all", "url": "https://linux.do/t/topic/22", "created_at": "2026-09-21T00:01:00Z", "tags": ["人工智能"]},
            {"id": 33, "title": "small body", "url": "https://linux.do/t/topic/33", "created_at": "2026-09-21T00:02:00Z", "tags": ["人工智能"]},
        ]
        # enough rows that a body-free payload still clears the 1024-byte compression floor
        for n in range(40, 60):
            rows.append(
                {
                    "id": n,
                    "title": f"row {n}",
                    "url": f"https://linux.do/t/topic/{n}",
                    "created_at": f"2026-09-21T00:{n % 60:02d}:00Z",
                    "category": "开发调优",
                    "tags": ["人工智能", "纯水"],
                    "excerpt": "摘录 " * 6,
                }
            )
        self.store.upsert_topics(rows)
        self.store.set_fields(11, body_text=BIG_BODY, detail_fetched=True)
        self.store.set_fields(33, body_text=SMALL_BODY, detail_fetched=True)
        self.store.save()
        self.logs: list[str] = []
        self.cfg = dict(CFG)
        app = server_mod.App(self.cfg, self.store, mock.Mock(running=False), self.logs.append)
        patcher = mock.patch.dict(
            os.environ,
            {
                "LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN,
                "LINUXDO_AI_OWNER_TOKEN_FILE": "",
                "LINUXDO_AI_ALLOWED_ORIGINS": PAGES_ORIGIN,
            },
            clear=False,
        )
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

    def req(self, path, *, accept=None, origin=None, method="GET", token=None):
        """(status, headers dict (lower-cased), raw wire bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if accept is not None:
            headers["Accept-Encoding"] = accept
        if origin is not None:
            headers["Origin"] = origin
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            conn.request(method, path, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, raw
        finally:
            conn.close()

    def body_json(self, raw: bytes, headers: dict):
        if headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------- both /api/state contracts
    def test_default_state_read_keeps_body_text_for_older_readers(self):
        status, headers, raw = self.req("/api/state")
        self.assertEqual(status, 200)
        payload = self.body_json(raw, headers)
        by_id = {t["id"]: t for t in payload["topics"]}
        self.assertEqual(by_id[11]["body_text"], BIG_BODY, "an older tab must still get the body")
        self.assertEqual(by_id[33]["body_text"], SMALL_BODY)
        self.assertEqual(by_id[11]["excerpt"], "只拿到摘录")
        for topic in payload["topics"]:
            self.assertIn("has_body", topic, "has_body is additive on the legacy shape too")
            self.assertIsInstance(topic["has_body"], bool)
        self.assertIs(by_id[11]["has_body"], True)
        self.assertIs(by_id[22]["has_body"], False)

    def test_view_list_is_body_free_with_has_body(self):
        status, headers, raw = self.req("/api/state?view=list")
        self.assertEqual(status, 200)
        payload = self.body_json(raw, headers)
        self.assertTrue(payload["topics"])
        for topic in payload["topics"]:
            self.assertNotIn("body_text", topic, "the list view must not ship bodies")
            self.assertIn("has_body", topic, "every row must say whether a body exists")
            self.assertIsInstance(topic["has_body"], bool)
        by_id = {t["id"]: t for t in payload["topics"]}
        self.assertIs(by_id[11]["has_body"], True)
        self.assertIs(by_id[22]["has_body"], False, "an empty body is reported as has_body false")
        # every field a row renders is still there - only body_text was replaced
        for kept in ("title", "url", "excerpt", "created_at", "bumped_at", "reply_count", "views", "category", "tags", "state", "filter"):
            self.assertIn(kept, by_id[11], f"{kept} must stay in the list payload")
        self.assertEqual(by_id[11]["excerpt"], "只拿到摘录", "every other field is kept")

    def test_list_view_is_much_smaller_than_the_legacy_default(self):
        _s, legacy_headers, legacy_raw = self.req("/api/state")
        _s, list_headers, list_raw = self.req("/api/state?view=list")
        legacy = self.body_json(legacy_raw, legacy_headers)
        listing = self.body_json(list_raw, list_headers)
        self.assertEqual(len(listing["topics"]), len(legacy["topics"]), "same rows, same order")
        self.assertLess(len(list_raw), len(legacy_raw), "dropping the bodies must shrink the payload")

    def test_other_view_values_keep_the_legacy_shape(self):
        for path in ("/api/state?view=bodies", "/api/state?view=", "/api/state?other=1"):
            with self.subTest(path=path):
                _s, headers, raw = self.req(path)
                payload = self.body_json(raw, headers)
                by_id = {t["id"]: t for t in payload["topics"]}
                self.assertIn("body_text", by_id[11], "an unknown view falls back to the legacy read")

    def test_api_payload_include_body_flag(self):
        with_body = self.store.api_payload(cfg=self.cfg, uptime_s=1.0)
        without = self.store.api_payload(cfg=self.cfg, uptime_s=1.0, include_body=False)
        self.assertEqual(with_body["topics"][0].get("body_text", ""), "", "fixture rows may be body-less")
        self.assertIn("body_text", {t["id"]: t for t in with_body["topics"]}[11])
        self.assertNotIn("body_text", {t["id"]: t for t in without["topics"]}[11])
        self.assertTrue(all("has_body" in t for t in without["topics"]))
        # the store's own state is never mutated by either shape
        self.assertEqual(self.store.get(11)["body_text"], BIG_BODY)

    # ------------------------------------------------------------ /api/topic/<id>
    def test_topic_route_returns_the_body(self):
        status, headers, raw = self.req("/api/topic/11")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        payload = self.body_json(raw, headers)
        self.assertEqual(payload["body_text"], BIG_BODY)
        self.assertEqual(payload["excerpt"], "只拿到摘录")
        self.assertIs(payload["has_body"], True)
        self.assertEqual(payload["id"], 11)
        self.assertEqual(payload["state"], self.store.get(11)["state"])
        self.assertEqual(
            sorted(payload),
            sorted(
                [
                    "id", "title", "url", "body_text", "excerpt", "has_body", "state", "filter",
                    "created_at", "bumped_at", "reply_count", "views", "category", "tags",
                ]
            ),
            "the field set is fixed",
        )
        self.assertEqual(payload["tags"], ["人工智能"])

    def test_topic_route_needs_no_owner_token(self):
        status, _headers, _raw = self.req("/api/topic/11")
        self.assertEqual(status, 200)
        status, _headers, _raw = self.req("/api/topic/11", origin=PAGES_ORIGIN)
        self.assertEqual(status, 200)

    def test_topic_route_404s_for_an_unknown_id(self):
        status, headers, raw = self.req("/api/topic/999999")
        self.assertEqual(status, 404)
        payload = self.body_json(raw, headers)
        self.assertEqual(payload["error"], "topic not found")
        self.assertEqual(payload["status"], 404)

    def test_topic_route_404s_for_a_non_numeric_id(self):
        for bad in ("abc", "11abc", "1.5", "-11"):
            with self.subTest(id=bad):
                status, headers, raw = self.req(f"/api/topic/{bad}")
                self.assertEqual(status, 404)
                self.assertEqual(self.body_json(raw, headers)["error"], "topic not found")

    def test_topic_route_refuses_other_methods(self):
        status, headers, _raw = self.req("/api/topic/11", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("GET", headers["allow"])

    # ------------------------------------------------------------------- gzip
    def test_gzip_round_trip_is_the_identical_json(self):
        plain_status, plain_headers, plain_raw = self.req("/api/topic/11")
        self.assertEqual(plain_status, 200)
        self.assertNotIn("content-encoding", plain_headers, "no Accept-Encoding => identity")
        status, headers, raw = self.req("/api/topic/11", accept="gzip, deflate, br, zstd")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(int(headers["content-length"]), len(raw), "Content-Length is the compressed length")
        self.assertLess(len(raw), len(plain_raw), "the compressed body is smaller")
        self.assertEqual(gzip.decompress(raw), plain_raw, "it decompresses to the identical JSON")
        self.assertEqual(self.body_json(raw, headers), json.loads(plain_raw.decode()))

    def test_gzip_is_not_applied_twice(self):
        _status, headers, raw = self.req("/api/topic/11", accept="gzip")
        self.assertEqual(headers["content-encoding"], "gzip")
        # a single gzip member: decompressing once yields JSON, not another gzip stream
        self.assertEqual(gzip.decompress(raw)[:1], b"{")

    def test_both_state_shapes_gzip(self):
        for path in ("/api/state", "/api/state?view=list"):
            with self.subTest(path=path):
                _s, plain_headers, plain_raw = self.req(path)
                self.assertGreaterEqual(len(plain_raw), server_mod.MIN_GZIP_BYTES, "the fixture payload must clear the floor")
                status, headers, raw = self.req(path, accept="gzip")
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-encoding"], "gzip")
                self.assertEqual(int(headers["content-length"]), len(raw))
                self.assertLess(len(raw), len(plain_raw))
                plain = json.loads(plain_raw.decode("utf-8"))
                unpacked = json.loads(gzip.decompress(raw).decode("utf-8"))
                for volatile in ("generated_at", "uptime_s"):
                    plain.pop(volatile, None)
                    unpacked.pop(volatile, None)
                self.assertEqual(unpacked, plain)
                if path.endswith("view=list"):
                    self.assertTrue(all("body_text" not in t for t in unpacked["topics"]))
                else:
                    self.assertTrue(all("body_text" in t for t in unpacked["topics"]))

    def test_head_carries_the_compressed_length(self):
        _s, get_headers, get_raw = self.req("/api/state?view=list", accept="gzip")
        status, headers, raw = self.req("/api/state?view=list", accept="gzip", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(headers["content-length"], get_headers["content-length"])
        self.assertEqual(raw, b"", "HEAD writes no body")
        self.assertIn("Accept-Encoding", headers["vary"])

    def test_no_gzip_without_accept_encoding(self):
        for accept in (None, "deflate, br", "identity", "gzip;q=0", "gzip;q=0, *;q=1"):
            with self.subTest(accept=accept):
                status, headers, raw = self.req("/api/state?view=list", accept=accept)
                self.assertEqual(status, 200)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(int(headers["content-length"]), len(raw))
                self.assertEqual(raw[:1], b"{", "the identity body is the JSON itself")

    def test_small_body_stays_uncompressed(self):
        status, headers, raw = self.req("/api/topic/33", accept="gzip")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers, "under 1024 bytes stays identity-encoded")
        self.assertLess(len(raw), server_mod.MIN_GZIP_BYTES)
        self.assertEqual(self.body_json(raw, headers)["body_text"], SMALL_BODY)
        # the same rule on another JSON route: a short /api/queue answer
        status, headers, raw = self.req("/api/queue", accept="gzip")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        self.assertLess(len(raw), server_mod.MIN_GZIP_BYTES)

    def test_vary_keeps_origin_and_adds_accept_encoding(self):
        _status, headers, _raw = self.req("/api/state", accept="gzip")
        vary = headers["vary"]
        self.assertIn("Origin", vary)
        self.assertIn("Accept-Encoding", vary)
        # and the CORS grant itself is still exact-origin only
        _status, headers, _raw = self.req("/api/state", accept="gzip", origin=PAGES_ORIGIN)
        self.assertEqual(headers["access-control-allow-origin"], PAGES_ORIGIN)
        self.assertIn("Accept-Encoding", headers["vary"])
        _status, headers, _raw = self.req("/api/state", accept="gzip", origin="https://evil.example")
        self.assertNotIn("access-control-allow-origin", headers)

    # ------------------------------------------------- compression negotiation rules
    def test_accepts_gzip_explicit_tokens(self):
        self.assertTrue(server_mod.accepts_gzip("gzip"))
        self.assertTrue(server_mod.accepts_gzip("gzip, deflate, br, zstd"))
        self.assertTrue(server_mod.accepts_gzip("deflate, gzip;q=0.5"))
        self.assertTrue(server_mod.accepts_gzip("x-gzip"))
        self.assertTrue(server_mod.accepts_gzip("X-GZIP;q=0.1"))
        self.assertFalse(server_mod.accepts_gzip(None))
        self.assertFalse(server_mod.accepts_gzip(""))
        self.assertFalse(server_mod.accepts_gzip("   "))
        self.assertFalse(server_mod.accepts_gzip("br, zstd"))
        self.assertFalse(server_mod.accepts_gzip("gzip;q=0"))
        self.assertFalse(server_mod.accepts_gzip("x-gzip;q=0"))

    def test_accepts_gzip_wildcard(self):
        self.assertTrue(server_mod.accepts_gzip("*"), "a positive wildcard permits gzip")
        self.assertTrue(server_mod.accepts_gzip("*;q=1"))
        self.assertTrue(server_mod.accepts_gzip("*;q=0.5"))
        self.assertTrue(server_mod.accepts_gzip("deflate, *;q=0.8"))
        self.assertFalse(server_mod.accepts_gzip("*;q=0"))
        self.assertFalse(server_mod.accepts_gzip("*, gzip;q=0"), "an explicit refusal is not bypassed by a wildcard")
        self.assertTrue(server_mod.accepts_gzip("*;q=0,gzip;q=1"), "an explicit positive token wins over a wildcard")

    def test_accepts_gzip_quality_edge_cases(self):
        for bad in ("gzip;q=abc", "gzip;q", "gzip;q=", "gzip;q=nan", "gzip;q=inf", "gzip;q=-inf",
                    "gzip;q=2", "gzip;q=1.0001", "gzip;q=-1", "gzip;q=", "*;q=nan", "*;q=2"):
            with self.subTest(header=bad):
                self.assertFalse(server_mod.accepts_gzip(bad), f"{bad!r} must not compress")
        for ok in ("gzip;q=0.0001", "gzip;Q=1", "gzip ;q=0.5", "gzip;q=1", "gzip;q=0.9999"):
            with self.subTest(header=ok):
                self.assertTrue(server_mod.accepts_gzip(ok), f"{ok!r} must compress")

    def test_accepts_gzip_duplicate_tokens_use_the_minimum(self):
        # conservative: q=0 cannot be bypassed by reordering
        self.assertFalse(server_mod.accepts_gzip("gzip;q=0, gzip;q=1"))
        self.assertFalse(server_mod.accepts_gzip("gzip;q=1, gzip;q=0"))
        self.assertFalse(server_mod.accepts_gzip("gzip;q=0.5, gzip;q=0"))
        self.assertTrue(server_mod.accepts_gzip("gzip;q=0.5, gzip;q=0.5"))
        self.assertTrue(server_mod.accepts_gzip("gzip, gzip"))
        self.assertFalse(server_mod.accepts_gzip("gzip;q=1, x-gzip;q=0"), "the alias shares the codec")
        self.assertFalse(server_mod.accepts_gzip("*;q=1, *;q=0"))

    def test_compressible_type_helper(self):
        self.assertTrue(server_mod.compressible_type("application/json; charset=utf-8"))
        self.assertTrue(server_mod.compressible_type("text/plain; version=0.0.4; charset=utf-8"))
        self.assertTrue(server_mod.compressible_type("application/javascript; charset=utf-8"))
        self.assertFalse(server_mod.compressible_type("image/svg+xml"))
        self.assertFalse(server_mod.compressible_type("application/octet-stream"))


if __name__ == "__main__":
    unittest.main()
