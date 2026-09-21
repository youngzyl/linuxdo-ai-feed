"""R4 regression tests: static file containment must use path ancestry, not string prefixes.

Reported defect: `server.serve_file` (server.py:184-198) accepts a file when
`str(target).startswith(str(root))`. A sibling directory whose name shares the root prefix
(`public-other/` next to `public/`) therefore passes the check, and `../public-other/...`
- encoded or not - is served. The traversal only needs the sibling to exist.

Fix shape: resolve the target and the allowed roots, then compare real ancestry
(`root == target or root in target.parents`). Escapes answer 403; legitimate files are
unchanged. No auth semantics, no routing changes.

These tests use a throwaway tree in a TemporaryDirectory and a loopback server on
127.0.0.1:0 - no project files, no secrets, no outbound network.

Run: python3 -m unittest tests.test_server_security -v
"""
from __future__ import annotations

import http.client
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

PUBLIC_OK = "public-content"
NESTED_OK = "nested-public-content"
FIXTURE_OK = '{"fixture":true}'
SIBLING_SECRET = "SECRET-FROM-public-other"
FIXTURE_SIBLING_SECRET = "SECRET-FROM-fixtures-extra"
OUTSIDE_SECRET = "SECRET-FROM-outside"


class StaticContainment(unittest.TestCase):
    """One throwaway file tree + one loopback server per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.public = self.base / "public"
        self.fixtures = self.base / "fixtures"
        self.public.mkdir()
        self.fixtures.mkdir()

        (self.public / "index.html").write_text("<html>index</html>", encoding="utf-8")
        (self.public / "app.txt").write_text(PUBLIC_OK, encoding="utf-8")
        (self.public / "sub").mkdir()
        (self.public / "sub" / "deep.txt").write_text(NESTED_OK, encoding="utf-8")
        (self.fixtures / "fx.json").write_text(FIXTURE_OK, encoding="utf-8")

        # escape targets: siblings whose names share the root prefix (the reported bug)
        (self.base / "public-other").mkdir()
        (self.base / "public-other" / "secret.txt").write_text(SIBLING_SECRET, encoding="utf-8")
        (self.base / "fixtures-extra").mkdir()
        (self.base / "fixtures-extra" / "leak.txt").write_text(FIXTURE_SIBLING_SECRET, encoding="utf-8")
        # a genuinely unrelated directory
        (self.base / "outside").mkdir()
        (self.base / "outside" / "outside.txt").write_text(OUTSIDE_SECRET, encoding="utf-8")
        # symlinks that point out of the served tree
        os.symlink(self.base / "outside" / "outside.txt", self.public / "link.txt")
        os.symlink(self.base / "outside", self.public / "dirlink")

        self.store = Store(self.base / "state.json", self.base / "failures.jsonl")
        self.logs: list[str] = []
        app = server_mod.App(CFG, self.store, mock.Mock(running=False), self.logs.append)

        for target, value in (("PUBLIC_DIR", self.public), ("FIXTURE_DIR", self.fixtures)):
            patcher = mock.patch.object(server_mod, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.httpd = server_mod.build_server(CFG, app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    # ------------------------------------------------------------ legitimate traffic
    def test_public_file_is_served(self):
        status, body = self._get("/app.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), PUBLIC_OK)

    def test_index_is_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"index", body)

    def test_nested_public_file_is_served(self):
        status, body = self._get("/sub/deep.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), NESTED_OK)

    def test_fixture_file_is_served(self):
        status, body = self._get("/fixtures/fx.json")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), FIXTURE_OK)

    def test_api_and_health_routes_are_untouched(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIn(b"attention", body)
        status, _ = self._get("/api/does-not-exist")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------- escapes
    def test_sibling_prefix_traversal_is_refused(self):
        status, body = self._get("/../public-other/secret.txt")
        self.assertEqual(status, 403, f"escaped into a sibling directory (body={body[:40]!r})")
        self.assertNotIn(SIBLING_SECRET.encode(), body)

    def test_encoded_sibling_traversal_is_refused(self):
        for path in ("/%2e%2e/public-other/secret.txt", "/..%2fpublic-other%2fsecret.txt"):
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertEqual(status, 403, f"{path} escaped containment (body={body[:40]!r})")
                self.assertNotIn(SIBLING_SECRET.encode(), body)

    def test_fixture_sibling_prefix_traversal_is_refused(self):
        status, body = self._get("/fixtures/../fixtures-extra/leak.txt")
        self.assertEqual(status, 403, f"escaped into a fixtures sibling (body={body[:40]!r})")
        self.assertNotIn(FIXTURE_SIBLING_SECRET.encode(), body)

    def test_unrelated_outside_directory_is_refused(self):
        status, body = self._get("/../outside/outside.txt")
        self.assertEqual(status, 403)
        self.assertNotIn(OUTSIDE_SECRET.encode(), body)

    def test_symlinked_file_out_of_tree_is_refused(self):
        status, body = self._get("/link.txt")
        self.assertNotEqual(status, 200, "symlink to an outside file was served")
        self.assertNotIn(OUTSIDE_SECRET.encode(), body)

    def test_symlinked_directory_out_of_tree_is_refused(self):
        status, body = self._get("/dirlink/outside.txt")
        self.assertNotEqual(status, 200, "symlinked directory traversal was served")
        self.assertNotIn(OUTSIDE_SECRET.encode(), body)

    def test_directory_target_is_still_404(self):
        status, _ = self._get("/sub")
        self.assertIn(status, (403, 404))


class TestServeFilePathLogic(unittest.TestCase):
    """Direct check of the helper the fix introduces, without going through HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "public").mkdir()
        (self.base / "public-other").mkdir()
        (self.base / "public" / "ok.txt").write_text("ok", encoding="utf-8")
        (self.base / "public-other" / "secret.txt").write_text("secret", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_within_uses_ancestry_not_string_prefix(self):
        from server import _within_roots

        public = (self.base / "public").resolve()
        self.assertTrue(_within_roots(public / "ok.txt", [public]))
        self.assertTrue(_within_roots(public, [public]))
        sibling = (self.base / "public-other" / "secret.txt").resolve()
        self.assertFalse(
            _within_roots(sibling, [public]),
            "a sibling sharing the root's name prefix must not count as inside",
        )
        self.assertFalse(_within_roots((self.base / "public-other").resolve(), [public]))

    def test_within_accepts_any_allowed_root(self):
        from server import _within_roots

        public = (self.base / "public").resolve()
        fixtures = (self.base / "fixtures").resolve()
        self.assertTrue(_within_roots(fixtures / "fx.json", [public, fixtures]))
        self.assertFalse(_within_roots((self.base / "public-other").resolve(), [public, fixtures]))


if __name__ == "__main__":
    unittest.main()
