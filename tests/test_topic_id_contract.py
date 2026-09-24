"""The /api/topic/<id> path-id contract (CONTRACT.md §1.1).

Real handlers must accept exactly ASCII `[0-9]{1,20}` and nothing else, and must decide that
*before* any int() conversion or store lookup:

  * leading zeros are legal syntax and name the same topic as the canonical id
  * zero, negative, signed, spaced, dotted, hex-ish, empty, slashed ids are 404
  * non-ASCII decimal digits are NOT ids, even though str.isdigit()/int() accept them
    (superscript ² is isdigit() but not int(); Arabic-Indic ١١ and fullwidth １１ are both)
  * a path longer than the length limit (the 4400-digit case that used to raise inside int())
    is 404, never 500
  * the id never reaches the store unless it passed the rules

The browser stub shares the same parser, so this suite also guards handler/stub parity.
Run: python3 -m unittest tests.test_topic_id_contract -v
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as server_mod  # noqa: E402
from store import Store  # noqa: E402

OWNER_TOKEN = "test-owner-token-do-not-log-id-4d2b"
SUPERSCRIPT_TWO = "\u00b2"                      # ²  (isdigit() true, int() raises)
ARABIC_INDIC_11 = "\u0661\u0661"                # ١١ (int() converts to 11)
FULLWIDTH_11 = "\uff11\uff11"                   # １１ (int() converts to 11)
FORTY_FOUR_HUNDRED_DIGITS = "9" * 4400          # beyond the int() conversion limit

# (raw path id, expected status, expected topic id served, may the store be consulted)
# A legal-but-unknown id is a 404 *after* the lookup; a malformed one never reaches the store.
CASES = (
    ("11", 200, 11, True),
    ("00011", 200, 11, True),      # leading zeros: same topic, legal syntax
    ("0", 404, None, False),
    ("00", 404, None, False),
    ("-11", 404, None, False),
    ("+11", 404, None, False),
    ("1.5", 404, None, False),
    ("1e3", 404, None, False),
    ("0x11", 404, None, False),
    ("11%20", 404, None, False),   # trailing space
    ("%2011", 404, None, False),   # leading space
    ("11%20extra", 404, None, False),   # inner space
    ("11/12", 404, None, False),   # slash
    ("11/", 404, None, False),
    ("%E2%80%8B11", 404, None, False),  # zero-width space
    (quote(SUPERSCRIPT_TWO), 404, None, False),
    (quote(ARABIC_INDIC_11), 404, None, False),
    (quote(FULLWIDTH_11), 404, None, False),
    (FORTY_FOUR_HUNDRED_DIGITS, 404, None, False),
    ("9" * 20, 404, None, True),   # legal syntax, unknown topic
    ("9" * 21, 404, None, False),  # one digit past the limit
)

CFG = {
    "host": "127.0.0.1",
    "port": 0,
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444.json",
    "tag_page_url": "https://linux.do/tag/444-tag/444",
    "site_url": "https://linux.do",
    "attention_threshold": 3,
    "filter_consecutive_error_threshold": 3,
    "filter": {"model": "deepseek/deepseek-v4-flash", "prompt_version": "v1"},
}


class TopicIdCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.store.upsert_topics(
            [
                {"id": 11, "title": "eleven", "created_at": "2026-09-21T00:00:00Z", "tags": ["人工智能"]},
                {"id": 44, "title": "forty-four", "created_at": "2026-09-21T00:01:00Z", "tags": ["人工智能"]},
            ]
        )
        self.store.set_fields(11, body_text="正文", detail_fetched=True)
        self.store.save()
        app = server_mod.App(dict(CFG), self.store, mock.Mock(running=False), lambda _m: None)
        patcher = mock.patch.dict(
            os.environ,
            {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN, "LINUXDO_AI_OWNER_TOKEN_FILE": "", "LINUXDO_AI_ALLOWED_ORIGINS": ""},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = server_mod.build_server(dict(CFG), app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def lookup(self, raw_path_id: str, *, method="GET"):
        """(status, json body, ids the store was asked for)."""
        seen: list = []
        original = self.store.get

        def spy(tid):
            seen.append(tid)
            return original(tid)

        self.store.get = spy  # type: ignore[assignment]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, f"/api/topic/{raw_path_id}")
            res = conn.getresponse()
            raw = res.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {"raw": raw[:80].decode("utf-8", "replace")}
            return res.status, payload, seen
        finally:
            conn.close()
            self.store.get = original  # type: ignore[assignment]

    def test_case_table(self):
        for raw, status, served, consulted in CASES:
            with self.subTest(raw=raw if len(raw) <= 24 else f"{len(raw)} digits"):
                got_status, payload, seen = self.lookup(raw)
                self.assertEqual(got_status, status, f"status for {raw!r} (body={payload!r})")
                if status == 404:
                    self.assertEqual(payload.get("error"), "topic not found")
                    if consulted:
                        self.assertEqual(seen, [int(raw)], "a legal id is looked up, then 404s")
                    else:
                        self.assertEqual(seen, [], "a malformed path id must never reach the store")
                else:
                    self.assertEqual(payload.get("id"), served)
                    self.assertEqual(seen, [served], "the store is asked for the converted id once")

    def test_malformed_ids_never_reach_the_store(self):
        for raw in (quote(SUPERSCRIPT_TWO), FORTY_FOUR_HUNDRED_DIGITS, quote(ARABIC_INDIC_11),
                    quote(FULLWIDTH_11), "0", "-11", "11/12", ""):
            with self.subTest(raw=raw[:16]):
                _status, _payload, seen = self.lookup(raw)
                self.assertEqual(seen, [], f"{raw[:16]!r} must not be converted or looked up")

    def test_empty_segment_is_404(self):
        status, payload, _seen = self.lookup("")
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), "topic not found")

    def test_parser_is_the_single_source_of_truth(self):
        self.assertEqual(server_mod.parse_topic_id("11"), 11)
        self.assertEqual(server_mod.parse_topic_id("00011"), 11)
        self.assertEqual(server_mod.parse_topic_id("9" * 20), int("9" * 20))
        for bad in ("", "0", "00", "-1", "+1", "1.5", "1e3", "0x11", " 11", "11 ", "11/12",
                    SUPERSCRIPT_TWO, ARABIC_INDIC_11, FULLWIDTH_11, "9" * 21, FORTY_FOUR_HUNDRED_DIGITS,
                    None, 11):
            with self.subTest(bad=(bad if not isinstance(bad, str) or len(bad) < 20 else "long")):
                self.assertIsNone(server_mod.parse_topic_id(bad))  # type: ignore[arg-type]

    def test_browser_stub_uses_the_same_parser(self):
        spec = importlib.util.spec_from_file_location("browser_check_mod", ROOT / "tests" / "browser_check.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(
            module.parse_topic_id,
            server_mod.parse_topic_id,
            "the stub must share the handler's parser, not re-implement the rules",
        )
        stub_topics = {"topics": [{"id": 11, "body_text": "正文", "excerpt": "摘"}]}
        for raw, status, served, _consulted in CASES:
            with self.subTest(raw=raw if len(raw) <= 24 else "long"):
                stub_status, stub_payload = module.StubHandler.resolve_topic(raw, stub_topics)
                self.assertEqual(stub_status, status)
                if status == 404:
                    self.assertEqual(stub_payload.get("error"), "topic not found")
                else:
                    self.assertEqual(stub_payload["id"], served)
        # the case that used to differ: a zero-padded id means the same topic in both


if __name__ == "__main__":
    unittest.main()
