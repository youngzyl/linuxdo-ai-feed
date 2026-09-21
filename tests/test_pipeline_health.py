"""R1 regression tests: fetch and filter failure streaks must be tracked separately.

Reported defect: `Pipeline.run_cycle` calls `Store.record_success()` (store.py:361) right
after a *fetch* success, and that call resets `filter_consecutive_errors`. A collector that
answers fine while the filter keeps failing therefore never reaches
`filter_consecutive_error_threshold`, so `/health.attention` (and the 20-minute watchdog that
reads it) stays silent forever.

Fix shape: fetch success resets only the fetch streak, filter success resets only the filter
streak, and the filter streak survives a successful fetch. Additive health fields only -
the existing health contract (keys, meaning of `consecutive_failures`,
`filter_consecutive_errors`, `fetch`/`filter` sections, `attention`) is unchanged.

Run: python3 -m unittest tests.test_pipeline_health -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline as pipeline_mod  # noqa: E402
from store import Store  # noqa: E402

CFG = {
    "interval_minutes": 15,
    "attention_threshold": 3,
    "filter_consecutive_error_threshold": 3,
    "request_timeout_s": 30,
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444.json",
    "tag_page_url": "https://linux.do/tag/444-tag/444",
    "site_url": "https://linux.do",
    "fetch": {
        "rss_delay_s": 0,
        "detail_max_per_cycle": 5,
        "pages": 1,
        "attempts": 1,
        "backoff_s": [0],
    },
    "filter": {"model": "deepseek/deepseek-v4.1-flash", "prompt_version": "v1"},
}

# Keys the watchdog / frontend contract already relies on (scripts/monitor.py reads the state
# file directly, public/app.js reads /api/state). They must keep existing.
BASE_HEALTH_PAYLOAD_KEYS = {
    "status",
    "attention",
    "uptime_s",
    "last_success_at",
    "last_attempt_at",
    "consecutive_failures",
    "next_retry_at",
    "cycles_total",
    "cycles_failed",
    "counts",
    "fetch",
    "filter",
    "last_error",
}


class FakeCollector:
    """Always answers: the fetch stage is healthy in every R1 scenario."""

    def __init__(self, cfg, store, log):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.categories = {"开发调优": 1}
        self.seen = 0

    def fetch_list(self, pages=None):
        topics = [{"id": 1000 + self.seen + i, "title": f"t{i}", "created_at": "2026-09-21T00:00:00Z"} for i in range(2)]
        self.seen += len(topics)
        return {"topics": topics, "pages": 1, "page_errors": [], "list_source": "direct"}

    def fetch_rss_bodies(self):
        return {}

    def apply_rss_bodies(self, rss):
        return {"rss_items": 0, "rss_matched": 0, "rss_applied": 0}

    def ensure_details(self, ids):
        return {"detail_ok": 0, "detail_failed": 0, "detail_via_jina": 0}


class FakeFilter:
    """Filter outcome is scripted per call; fetch is independent of it."""

    def __init__(self, cfg, store, log, outcomes):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, *, limit=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else {"ok": True}
        summary = {
            "ok": outcome.get("ok", True),
            "model": CFG["filter"]["model"],
            "key_present": outcome.get("key_present", True),
            "key_source": "env:test",
            "batches_ok": 0 if not outcome.get("ok", True) else 1,
            "batches_failed": 0 if outcome.get("ok", True) else 1,
            "judged": 0,
            "unjudged": 0,
            "picked": 0,
            "error": outcome.get("error"),
            "finished_at": "2026-09-21T00:00:00Z",
        }
        return summary


class PipelineHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _pipeline(self, filter_outcomes):
        cfg = dict(CFG)
        cfg["fetch"] = dict(CFG["fetch"])
        cfg["filter"] = dict(CFG["filter"])
        fake_filter = FakeFilter(cfg, self.store, self.logs.append, filter_outcomes)
        patches = [
            mock.patch.object(pipeline_mod, "Collector", FakeCollector),
            mock.patch.object(pipeline_mod, "Filter", lambda *a, **k: fake_filter),
            mock.patch.object(pipeline_mod, "resolve_api_key", lambda: ("test-key", "env:test")),
            # keep the cycle lock out of the production data directory
            mock.patch.object(pipeline_mod, "DATA_DIR", self.dir),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        pipe = pipeline_mod.Pipeline(cfg, self.store, self.logs.append)
        return pipe, fake_filter


class TestFilterStreakSurvivesFetchSuccess(PipelineHarness):
    def test_three_fetch_ok_filter_fail_cycles_raise_attention(self):
        pipe, fake = self._pipeline([{"ok": False, "error": "HTTP 503 from provider"}] * 3)
        for _ in range(3):
            result = pipe.run_cycle()
            self.assertTrue(result["ok"], "fetch stage must have succeeded")
            self.assertFalse(result["filter"]["ok"])
        health = self.store.health()
        self.assertEqual(health["consecutive_failures"], 0, "fetch never failed")
        self.assertEqual(
            health["filter_consecutive_errors"],
            3,
            "filter streak must accumulate across fetch-ok cycles",
        )
        att = self.store.attention(CFG)
        self.assertTrue(att["needed"], "3 fetch-ok/filter-fail cycles must need attention")
        self.assertEqual(att["kind"], "filter_failing")
        # the watchdog reads attention.needed from /health
        payload = self.store.health_payload(cfg=CFG, uptime_s=1)
        self.assertTrue(payload["attention"]["needed"])

    def test_filter_streak_is_visible_in_the_state_file_the_watchdog_reads(self):
        pipe, _ = self._pipeline([{"ok": False, "error": "boom"}] * 3)
        for _ in range(3):
            pipe.run_cycle()
        self.store.save()
        # scripts/monitor.py file mode reads data/state.json health directly
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertGreaterEqual(int(reloaded.health().get("filter_consecutive_errors") or 0), 3)

    def test_filter_success_clears_only_the_filter_streak(self):
        pipe, _ = self._pipeline(
            [
                {"ok": False, "error": "boom"},
                {"ok": False, "error": "boom"},
                {"ok": True},
            ]
        )
        for _ in range(2):
            pipe.run_cycle()
        self.assertEqual(self.store.health()["filter_consecutive_errors"], 2)
        pipe.run_cycle()
        health = self.store.health()
        self.assertEqual(health["filter_consecutive_errors"], 0)
        self.assertFalse(self.store.attention(CFG)["needed"])

    def test_missing_key_still_reports_degraded_without_counting_a_filter_error(self):
        pipe, _ = self._pipeline([{"ok": False, "key_present": False, "error": "no API key"}] * 2)
        pipe.run_cycle()
        health = self.store.health()
        self.assertEqual(health["filter_consecutive_errors"], 0)
        self.assertEqual(health["status"], "degraded")
        # no_key attention still comes from pending topics + missing key
        self.store.upsert_topics([{"id": 4242, "title": "pending one"}])
        with mock.patch("store.live_key_present", return_value=False):
            att = self.store.attention(CFG)
        self.assertTrue(att["needed"])
        self.assertEqual(att["kind"], "no_key")


class TestStageCountersAtStoreLevel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_success_resets_fetch_streak_only(self):
        self.store.record_failure("fetch boom")
        self.store.record_failure("fetch boom")
        self.store.record_filter_failure("filter boom")
        self.store.record_filter_failure("filter boom")
        self.store.record_fetch_success()
        health = self.store.health()
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(
            health["filter_consecutive_errors"],
            2,
            "a successful fetch must not clear the filter failure streak",
        )

    def test_filter_success_resets_filter_streak_only(self):
        self.store.record_failure("fetch boom")
        self.store.record_filter_failure("filter boom")
        self.store.record_filter_success()
        health = self.store.health()
        self.assertEqual(health["filter_consecutive_errors"], 0)
        self.assertEqual(
            health["consecutive_failures"],
            1,
            "a successful filter must not clear the fetch failure streak",
        )

    def test_legacy_record_success_behaves_as_fetch_success(self):
        """Old callers/tests keep working: record_success() is the fetch-stage success."""
        self.store.record_failure("fetch boom")
        self.store.record_filter_failure("filter boom")
        self.store.record_success()
        health = self.store.health()
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(
            health["filter_consecutive_errors"],
            1,
            "the legacy alias must keep the new stage separation",
        )

    def test_filter_failure_records_the_stage_error_message(self):
        self.store.record_filter_failure("HTTP 401 from provider")
        self.assertEqual(self.store.health()["filter"]["last_error"], "HTTP 401 from provider")

    def test_health_contract_keys_are_additive_only(self):
        self.store.upsert_topics([{"id": 1, "title": "a"}])
        payload = self.store.health_payload(cfg=CFG, uptime_s=3)
        missing = BASE_HEALTH_PAYLOAD_KEYS - set(payload)
        self.assertEqual(missing, set(), f"health contract lost keys: {sorted(missing)}")
        self.assertIsInstance(payload["attention"]["needed"], bool)
        self.assertIn("kind", payload["attention"])
        health = self.store.health()
        for key in ("consecutive_failures", "filter_consecutive_errors", "cycles_total", "cycles_failed"):
            self.assertIn(key, health)
        for key in ("ok", "key_present", "batches_ok", "batches_failed", "judged", "finished_at"):
            self.assertIn(key, health["filter"])
        for key in ("ok", "pages", "topics_seen", "new", "finished_at"):
            self.assertIn(key, health["fetch"])

    def test_fetch_streak_still_drives_fetch_attention(self):
        for _ in range(3):
            self.store.record_failure("fetch boom")
        att = self.store.attention(CFG)
        self.assertTrue(att["needed"])
        self.assertEqual(att["kind"], "fetch_failing")
        self.assertEqual(self.store.health_payload(cfg=CFG, uptime_s=1)["status"], "failing")


if __name__ == "__main__":
    unittest.main()
