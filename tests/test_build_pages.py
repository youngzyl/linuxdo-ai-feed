"""Tests for scripts/build_pages.py — the GitHub Pages bundle.

The published site cannot call relative /api/* URLs, so the build injects the API origin
through a generated runtime-config.js. What must hold:
  * exactly the allowed files are shipped (index.html, app.js, styles.css,
    runtime-config.js, .nojekyll) - no fixtures, no data/, no docs/, no .env, no logs
  * the injected base is a validated absolute https origin (localhost http is the only
    exception), never a wildcard and never a query-supplied endpoint
  * index.html loads runtime-config.js before app.js
  * nothing in the output resembles a credential
  * the build refuses to overwrite the source tree and refuses bad bases
  * a reused --out must hold nothing but this build's own output: an unexpected file, a
    nested directory (a stale fixtures/) or a symlink aborts the build instead of being
    silently preserved

Run: python3 -m unittest tests.test_build_pages -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("build_pages", ROOT / "scripts" / "build_pages.py")
build_pages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_pages)

API_BASE = "https://tcstw.youngzyl.me:8443/linuxdo-api"
OWNER_TOKEN_CANARY = "owner-token-canary-3f9a1c-do-not-ship"


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "dist"

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, out: Path | None = None) -> set[str]:
        target = out or self.out
        return {p.name for p in target.iterdir()}


class TestBundleContents(BuildCase):
    def test_exactly_the_allowed_files_are_published(self):
        manifest = build_pages.build(API_BASE, self.out)
        self.assertEqual(
            self.files(),
            {"index.html", "app.js", "styles.css", "runtime-config.js", ".nojekyll"},
            "the bundle must not carry fixtures, data, docs or dotfiles",
        )
        names = {entry["path"] for entry in manifest["files"]}
        self.assertEqual(names, self.files())

    def test_no_fixtures_data_or_secrets_are_copied(self):
        build_pages.build(API_BASE, self.out)
        self.assertFalse((self.out / "fixtures").exists())
        for forbidden in ("data", "docs", "logs", ".env", ".git", "__pycache__", "CONTRACT.md", "config.json"):
            self.assertFalse((self.out / forbidden).exists(), f"{forbidden} must not be published")
        self.assertFalse(any("fixture" in name for name in self.files()))

    def test_runtime_config_carries_the_api_base(self):
        build_pages.build(API_BASE, self.out)
        text = (self.out / "runtime-config.js").read_text(encoding="utf-8")
        self.assertIn("window.LINUXDO_AI_RUNTIME", text)
        self.assertIn(API_BASE, text)
        payload = json.loads(text.split("window.LINUXDO_AI_RUNTIME =", 1)[1].strip().rstrip(";"))
        self.assertEqual(payload, {"apiBase": API_BASE})
        self.assertEqual(list(payload), ["apiBase"], "only apiBase may be injected")
        self.assertNotIn("token", text.lower())

    def test_index_loads_runtime_config_before_app(self):
        build_pages.build(API_BASE, self.out)
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="runtime-config.js?v=20260925-8444"', html)
        self.assertIn('src="app.js?v=20260925-8444"', html)
        self.assertLess(html.index("runtime-config.js"), html.index('src="app.js'))

    def test_optional_read_namespace_preserves_old_key_without_changing_api(self):
        new = "https://tcstw.youngzyl.me:8444/linuxdo-api"
        manifest = build_pages.build(new, self.out, read_state_namespace=API_BASE)
        config = (self.out / "runtime-config.js").read_text()
        payload = json.loads(config.split("window.LINUXDO_AI_RUNTIME =", 1)[1].strip().rstrip(";"))
        self.assertEqual(payload, {"apiBase": new, "readStateNamespace": API_BASE})
        self.assertEqual(manifest["api_base"], new)

    def test_cli_accepts_optional_namespace(self):
        import subprocess
        new = "https://tcstw.youngzyl.me:8444/linuxdo-api"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_pages.py"), "--api-base", new,
             "--read-state-namespace", API_BASE, "--out", str(self.out), "--quiet"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["api_base"], new)
        self.assertIn('"readStateNamespace": "' + API_BASE + '"', (self.out / "runtime-config.js").read_text())

    def test_invalid_optional_namespace_refuses_before_writing(self):
        for bad in ("http://other.example/api", "https://x.test/api?evil=1", "javascript:alert(1)"):
            with self.subTest(bad=bad), self.assertRaises(build_pages.BuildError):
                build_pages.build(API_BASE, self.out, read_state_namespace=bad)
        self.assertFalse(self.out.exists())

    def test_static_assets_are_byte_identical_to_the_sources(self):
        build_pages.build(API_BASE, self.out)
        for name in ("index.html", "app.js", "styles.css"):
            self.assertEqual(
                (self.out / name).read_bytes(),
                (ROOT / "public" / name).read_bytes(),
                f"{name} must be shipped unchanged",
            )

    def test_manifest_is_a_verifiable_inventory(self):
        manifest = build_pages.build(API_BASE, self.out)
        self.assertEqual(manifest["api_base"], API_BASE)
        self.assertTrue(manifest["files"])
        for entry in manifest["files"]:
            data = (self.out / entry["path"]).read_bytes()
            self.assertEqual(entry["bytes"], len(data))
            import hashlib

            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

    def test_rebuild_is_deterministic(self):
        build_pages.build(API_BASE, self.out)
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        build_pages.build(API_BASE, self.out)
        second = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(first, second)

    def test_localhost_http_is_allowed_for_dev(self):
        build_pages.build("http://127.0.0.1:8791", self.out)
        self.assertIn("http://127.0.0.1:8791", (self.out / "runtime-config.js").read_text(encoding="utf-8"))


class TestApiBaseValidation(BuildCase):
    def test_non_localhost_http_is_refused(self):
        with self.assertRaises(build_pages.BuildError):
            build_pages.build("http://tcstw.youngzyl.me:8443/linuxdo-api", self.out)
        self.assertFalse(self.out.exists(), "nothing may be written when the base is invalid")

    def test_query_fragment_credentials_and_weird_schemes_are_refused(self):
        for bad in (
            "https://example.test/api?token=1",
            "https://example.test/api#frag",
            "https://user:pass@example.test/api",
            "javascript:alert(1)",
            "ftp://example.test/api",
            "//example.test/api",
            "example.test/api",
            "",
            "   ",
        ):
            with self.subTest(base=bad):
                with self.assertRaises(build_pages.BuildError):
                    build_pages.build(bad, self.out)
        self.assertFalse(self.out.exists())

    def test_trailing_slash_is_normalized(self):
        manifest = build_pages.build("https://example.test/linuxdo-api/", self.out)
        self.assertEqual(manifest["api_base"], "https://example.test/linuxdo-api")
        self.assertIn('"apiBase": "https://example.test/linuxdo-api"', (self.out / "runtime-config.js").read_text(encoding="utf-8"))


class TestOutputSafety(BuildCase):
    def test_refuses_to_write_into_the_source_tree(self):
        for target in (ROOT, ROOT / "public", ROOT / "public" / "dist"):
            with self.subTest(out=str(target)):
                with self.assertRaises(build_pages.BuildError):
                    build_pages.build(API_BASE, target)
        self.assertTrue((ROOT / "public" / "app.js").is_file(), "sources must be untouched")

    def test_unexpected_files_in_the_output_are_refused(self):
        self.out.mkdir(parents=True)
        (self.out / "leak.txt").write_text("x")
        with self.assertRaises(build_pages.BuildError):
            build_pages.build(API_BASE, self.out)
        self.assertFalse((self.out / "index.html").exists(), "a refused build writes nothing")

    def test_reused_output_with_a_nested_fixture_directory_is_refused(self):
        """Regression: a pre-existing nested dir (fixtures/) must abort, not be preserved.

        This is the case the old check missed - it only looked at files, so a stale
        `fixtures/` tree stayed in the bundle instead of failing the build.
        """
        self.out.mkdir(parents=True)
        (self.out / "fixtures").mkdir()
        (self.out / "fixtures" / "state.sample.json").write_text('{"stale": true}')

        with self.assertRaises(build_pages.BuildError) as ctx:
            build_pages.build(API_BASE, self.out)

        self.assertIn("fixtures/", str(ctx.exception))
        self.assertFalse((self.out / "index.html").exists(), "a refused build writes nothing")
        self.assertTrue(
            (self.out / "fixtures" / "state.sample.json").is_file(),
            "the refused tree is left exactly as it was",
        )

    def test_cli_exits_nonzero_for_a_reused_output_with_a_nested_fixture(self):
        import contextlib
        import io

        self.out.mkdir(parents=True)
        (self.out / "fixtures").mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = build_pages.main(["--api-base", API_BASE, "--out", str(self.out), "--quiet"])
        self.assertNotEqual(code, 0)
        self.assertEqual(code, 2)
        self.assertIn("build failed", err.getvalue())

    def test_reused_output_with_a_symlink_is_refused(self):
        self.out.mkdir(parents=True)
        (self.out / "index.html").symlink_to(ROOT / "public" / "index.html")
        with self.assertRaises(build_pages.BuildError) as ctx:
            build_pages.build(API_BASE, self.out)
        self.assertIn("symlink", str(ctx.exception))

    def test_reused_output_holding_only_build_output_is_still_allowed(self):
        """A legitimate rebuild into its own previous output must keep working."""
        build_pages.build(API_BASE, self.out)
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        build_pages.build(API_BASE, self.out)
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_build_never_contains_a_credential(self):
        canary_env = {
            "LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN_CANARY,
            "DEEPSEEK_API_KEY": "sk-deepseek-canary-1234567890",
        }
        with mock.patch.dict(os.environ, canary_env, clear=False):
            build_pages.build(API_BASE, self.out)
            for name in self.files():
                text = (self.out / name).read_text(encoding="utf-8", errors="replace")
                for canary in canary_env.values():
                    self.assertNotIn(canary, text)
        self.assertNotIn("Bearer ", (self.out / "runtime-config.js").read_text(encoding="utf-8"))

    def test_cli_rejects_a_bad_base_with_a_nonzero_exit(self):
        code = build_pages.main(["--api-base", "http://example.test/api", "--out", str(self.out), "--quiet"])
        self.assertEqual(code, 2)

    def test_cli_writes_the_manifest(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = build_pages.main(["--api-base", API_BASE, "--out", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        manifest = json.loads(buf.getvalue())
        self.assertEqual(manifest["api_base"], API_BASE)
        self.assertEqual({e["path"] for e in manifest["files"]}, self.files())


if __name__ == "__main__":
    unittest.main()
