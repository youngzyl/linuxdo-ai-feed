"""Batch 1 / unit 3: strict result normalization and honest partial accounting.

Lead contract exercised here:

  * A model row is accepted only when it is uniquely identified and well typed: indices
    are integers (a bool is not an index, a string is not an index), `valuable` is a real
    bool, and `score` - when present - is a finite number inside the documented policy
    range. Wrong scalar types are refused, never coerced into a verdict.
  * Duplicate indices make *that index* ambiguous. The whole index is refused; there is no
    silent last-wins.
  * Out-of-range/unknown indices are counted. A partial answer is not a success: the
    summary is ok=false and carries explicit partial/missing/invalid/ambiguous counts, so
    the filter failure streak is not reset. Topics without an accepted verdict stay
    eligible.
  * No extra provider request is made: the configured batch budget is untouched.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import filter as filter_mod  # noqa: E402
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
    "filter": {"model": "deepseek/deepseek-v4.1-flash", "prompt_version": "v1", "batch_size": 3},
}

PICK = {"valuable": True, "score": 82, "category": "模型发布", "reason": "r", "summary": "s"}


def batch_of(n: int) -> list[dict]:
    return [{"id": 100 + i, "title": f"t{i}", "source_version": f"sv1:{i:016x}"} for i in range(n)]


def tid_of(batch: list[dict], index: int) -> int:
    """The topic id at 1-based `index` of a dispatched batch (index 1 -> batch[0])."""
    return int(batch[index - 1]["id"])


def row(index, **extra) -> dict:
    return dict({"i": index, **PICK}, **extra)


class TestStrictRowNormalization(unittest.TestCase):
    def norm(self, value):
        return filter_mod.normalize_result_row(value, "m", "v1")

    def test_a_bool_is_not_an_index(self):
        verdict, reason = self.norm(row(True))
        self.assertIsNone(verdict)
        self.assertEqual(reason, "bad_index")

    def test_a_string_or_float_is_not_an_index(self):
        for bad in ("2", 2.0, None, [2]):
            verdict, reason = self.norm(row(bad))
            self.assertIsNone(verdict, f"{bad!r} must not be accepted as an index")
            self.assertEqual(reason, "bad_index")

    def test_index_aliases_are_accepted_when_they_are_integers(self):
        self.assertEqual(self.norm({"index": 3, **PICK})[0]["i"], 3)
        self.assertEqual(self.norm({"id": 4, **PICK})[0]["i"], 4)

    def test_valuable_must_be_a_real_bool(self):
        for bad in (1, 0, "true", "yes", [True], {"value": True}, None):
            verdict, reason = self.norm({"i": 1, "valuable": bad})
            self.assertIsNone(verdict, f"valuable={bad!r} must be refused, not coerced")
            self.assertEqual(reason, "bad_valuable")

    def test_valuable_absent_without_a_valid_score_is_invalid(self):
        self.assertEqual(self.norm({"i": 1})[1], "bad_valuable")
        self.assertEqual(self.norm({"i": 1, "score": "82"})[1], "bad_score")

    def test_the_documented_score_policy_decides_when_valuable_is_absent(self):
        high, reason = self.norm({"i": 1, "score": 88})
        low, _ = self.norm({"i": 1, "score": 20})
        self.assertEqual(reason, "ok")
        self.assertTrue(high["valuable"])
        self.assertFalse(low["valuable"])
        self.assertEqual(high["score"], 88)

    def test_score_must_be_finite_and_inside_the_policy_range(self):
        for bad in ("82", True, float("nan"), float("inf"), -1, 101, [80]):
            verdict, reason = self.norm({"i": 1, "valuable": True, "score": bad})
            self.assertIsNone(verdict, f"score={bad!r} must be refused")
            self.assertEqual(reason, "bad_score")

    def test_score_is_optional_when_valuable_is_a_bool(self):
        verdict, reason = self.norm({"i": 1, "valuable": True})
        self.assertEqual(reason, "ok")
        self.assertIsNone(verdict["score"])
        self.assertTrue(verdict["valuable"])

    def test_an_integral_float_score_keeps_its_integer_form(self):
        verdict, _ = self.norm({"i": 1, "valuable": True, "score": 75.0})
        self.assertEqual(verdict["score"], 75)

    def test_text_fields_are_capped_and_never_stringified_from_containers(self):
        verdict, reason = self.norm({"i": 1, "valuable": True, "score": 70, "reason": "x" * 500, "summary": "y" * 500})
        self.assertEqual(reason, "ok")
        self.assertLessEqual(len(verdict["reason"]), 120)
        self.assertLessEqual(len(verdict["summary"]), 220)

    def test_containers_in_text_fields_are_dropped_not_stringified(self):
        rows = [{"i": 1, "valuable": True, "score": 70, "reason": ["a"], "summary": {"b": 1}, "category": "c"}]
        result = filter_mod.normalize_batch(rows, batch_of(1), "m", "v1")
        verdict = result["verdicts"][0]
        self.assertEqual(verdict["reason"], "")
        self.assertEqual(verdict["summary"], "")
        self.assertEqual(result["counts"]["text_type_rejected"], 2)

    def test_the_historical_permissive_helper_is_unchanged(self):
        """`normalize_verdict` stays as the legacy surface existing callers/tests use."""
        verdict = filter_mod.normalize_verdict({"i": "3", "valuable": "true", "score": "75.0"}, "m", "v1")
        self.assertEqual(verdict["i"], 3)
        self.assertTrue(verdict["valuable"])
        self.assertEqual(verdict["score"], 75)


class TestBatchAccounting(unittest.TestCase):
    def test_duplicate_indices_are_ambiguous_not_last_wins(self):
        batch = batch_of(3)
        rows = [row(2, valuable=False, score=10), row(2, valuable=True, score=90)]
        result = filter_mod.normalize_batch(rows, batch, "m", "v1")
        self.assertEqual([v["i"] for v in result["verdicts"]], [], "a duplicated index must not be resolved by order")
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["unjudged_reasons"][tid_of(batch, 2)], "ambiguous")
        self.assertEqual([t["id"] for t in result["unjudged"]], [t["id"] for t in batch])

    def test_three_rows_for_one_index_are_still_one_ambiguous_index(self):
        result = filter_mod.normalize_batch([row(1), row(1), row(1)], batch_of(1), "m", "v1")
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertEqual(result["counts"]["accepted"], 0)

    def test_a_duplicate_does_not_disturb_its_neighbours(self):
        batch = batch_of(3)
        rows = [row(1), row(2, valuable=False, score=10), row(2, valuable=True, score=90), row(3)]
        result = filter_mod.normalize_batch(rows, batch, "m", "v1")
        self.assertEqual([v["i"] for v in result["verdicts"]], [1, 3])
        self.assertEqual([t["id"] for t in result["unjudged"]], [tid_of(batch, 2)])
        self.assertEqual(result["counts"]["missing"], 0, "an ambiguous index is not also a missing one")

    def test_out_of_range_indices_are_counted_unknown(self):
        result = filter_mod.normalize_batch([row(0), row(99)], batch_of(2), "m", "v1")
        self.assertEqual(result["counts"]["unknown"], 2)
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["missing"], 2)

    def test_valid_rows_are_accepted_and_gaps_are_classified(self):
        batch = batch_of(3)
        rows = [row(1), {"i": 3, "valuable": "yes", "score": 90}]
        result = filter_mod.normalize_batch(rows, batch, "m", "v1")
        self.assertEqual([v["i"] for v in result["verdicts"]], [1])
        self.assertEqual(result["counts"]["accepted"], 1)
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["counts"]["missing"], 2, "the refused row leaves its own index unanswered too")
        self.assertEqual(result["counts"]["returned"], 2)
        self.assertEqual(result["unjudged_reasons"][tid_of(batch, 2)], "missing")
        self.assertEqual(result["unjudged_reasons"][tid_of(batch, 3)], "missing")

    def test_the_dispatch_snapshot_travels_with_the_verdict(self):
        snapshot = {100: "sv1:aaa", 101: "sv1:bbb"}
        result = filter_mod.normalize_batch([row(2)], batch_of(2), "m", "v1", snapshot=snapshot)
        self.assertEqual(result["verdicts"][0]["source_version"], "sv1:bbb")
        self.assertEqual(result["verdicts"][0]["topic_id"], 101)


class RunHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, n: int) -> list[int]:
        ids = [200 + i for i in range(n)]
        self.store.upsert_topics([{"id": i, "title": f"t{i}", "excerpt": "摘录", "created_at": "2026-09-20T00:00:00Z"} for i in ids])
        return ids

    def run_filter(self, payload, *, calls=None):
        def call_model(key, messages):
            if calls is not None:
                calls.append(messages)
            return payload if isinstance(payload, str) else json.dumps(payload)

        f = filter_mod.Filter(CFG, self.store, lambda *_: None)
        f.call_model = call_model
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=("k", "env:test")):
            return f.run()

    def failures(self) -> list[str]:
        return [row.get("stage") for row in self.store.recent_failures(50)]


class TestRunSummary(RunHarness):
    def test_a_partial_answer_is_not_ok_and_says_what_is_missing(self):
        self.seed(3)
        summary = self.run_filter({"results": [row(1)]})
        self.assertFalse(summary["ok"], "one of three verdicts is not a full success")
        self.assertTrue(summary["partial"])
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["picked"], 1)
        self.assertEqual(summary["missing"], 2)
        self.assertEqual(summary["invalid"], 0)
        self.assertEqual(summary["unjudged"], 2)
        self.assertIn("partial", (summary["error"] or "").lower())
        # pending() is newest-first, so do not assume which seed got the single verdict
        judged = sorted(t["id"] for t in self.store.topics() if t.get("filter"))
        self.assertEqual(len(judged), 1)
        self.assertEqual(
            sorted(t["id"] for t in self.store.pending()),
            sorted({200, 201, 202} - set(judged)),
            "the omitted topics stay eligible for the next batch",
        )

    def test_a_complete_answer_is_ok(self):
        self.seed(3)
        summary = self.run_filter({"results": [row(1), row(2, valuable=False, score=10), row(3)]})
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["partial"])
        self.assertEqual(summary["judged"], 3)
        self.assertEqual(summary["picked"], 2)
        self.assertEqual(summary["missing"] + summary["invalid"] + summary["ambiguous"] + summary["unknown"], 0)
        self.assertEqual(self.store.pending(), [])

    def test_an_entirely_invalid_answer_leaves_every_topic_eligible(self):
        self.seed(2)
        summary = self.run_filter({"results": [{"i": True, "valuable": True}, {"i": "2", "valuable": True}]})
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["invalid"], 2)
        self.assertEqual(len(self.store.pending()), 2)

    def test_ambiguous_batches_count_and_keep_topics_eligible(self):
        self.seed(2)
        summary = self.run_filter({"results": [row(1), row(1)]})
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(len(self.store.pending()), 2)

    def test_gaps_are_journalled_with_distinct_stages(self):
        self.seed(3)
        self.run_filter({"results": [row(1), {"i": 2, "valuable": "yes", "score": 90}]})
        stages = self.failures()
        self.assertIn("filter_missing_index", stages)
        self.assertIn("filter_invalid_result", stages)

    def test_no_extra_provider_requests_beyond_the_batch_budget(self):
        self.seed(9)
        calls = []
        self.run_filter({"results": [row(1), row(2), row(3)]}, calls=calls)
        self.assertEqual(len(calls), 3, "9 topics at batch_size 3 is exactly three provider calls")

    def test_a_failed_call_keeps_the_batch_retryable(self):
        self.seed(2)

        def boom(key, messages):
            raise RuntimeError("HTTP 503 from provider")

        f = filter_mod.Filter(CFG, self.store, lambda *_: None)
        f.call_model = boom
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=("k", "env:test")):
            summary = f.run()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["batches_failed"], 1)
        self.assertEqual(len(self.store.pending()), 2)

    def test_batches_ok_and_partial_are_counted_per_batch_not_from_cumulative_gaps(self):
        """A later complete batch must not inherit the first batch's holes.

        Four topics, batch_size 2: first answer misses one index, second is complete.
        Cycle totals stay honest (ok=false, partial, missing=1, judged=3) while
        batches_partial / batches_ok split 1/1.
        """
        self.seed(4)
        payloads = [
            json.dumps({"results": [row(1)]}),
            json.dumps({"results": [row(1, valuable=False, score=10), row(2)]}),
        ]
        calls = []

        def call_model(key, messages):
            calls.append(messages)
            return payloads[len(calls) - 1]

        cfg = json.loads(json.dumps(CFG))
        cfg["filter"]["batch_size"] = 2
        f = filter_mod.Filter(cfg, self.store, lambda *_: None)
        f.call_model = call_model
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=("k", "env:test")):
            with mock.patch.object(filter_mod.time, "sleep"):
                summary = f.run()
        self.assertEqual(len(calls), 2)
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["partial"])
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["judged"], 3)
        self.assertEqual(summary["invalid"] + summary["ambiguous"] + summary["unknown"], 0)
        self.assertEqual(summary["batches_partial"], 1, "only the gapped batch is partial")
        self.assertEqual(summary["batches_ok"], 1, "the later complete batch is still ok")
        self.assertEqual(len(self.store.pending()), 1)


class FakeCollector:
    def __init__(self, cfg, store, log):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.categories = {"开发调优": 1}
        self.seen = 0

    def fetch_list(self, pages=None):
        topics = [{"id": 900 + self.seen + i, "title": f"t{i}", "excerpt": "摘录", "created_at": "2026-09-21T00:00:00Z"} for i in range(2)]
        self.seen += len(topics)
        return {"topics": topics, "pages": 1, "page_errors": [], "list_source": "direct"}

    def fetch_rss_bodies(self):
        return {}

    def apply_rss_bodies(self, rss):
        return {"rss_items": 0, "rss_matched": 0, "rss_applied": 0}

    def ensure_details(self, ids):
        return {"detail_ok": 0, "detail_failed": 0, "detail_via_jina": 0}


class TestPartialKeepsTheFailureStreak(unittest.TestCase):
    """A partial batch is a filter failure, not a fresh start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _pipeline(self, payload):
        patches = [
            mock.patch.object(pipeline_mod, "Collector", FakeCollector),
            mock.patch.object(pipeline_mod, "DATA_DIR", self.dir),
            mock.patch.object(pipeline_mod, "resolve_api_key", lambda: ("k", "env:test")),
            mock.patch.object(filter_mod, "resolve_api_key", lambda: ("k", "env:test")),
            mock.patch.object(filter_mod.Filter, "call_model", lambda self, key, messages: payload),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return pipeline_mod.Pipeline(dict(CFG), self.store, self.logs.append)

    def test_three_partial_cycles_raise_filter_attention(self):
        pipe = self._pipeline(json.dumps({"results": [{"i": 1, "valuable": True, "score": 88}]}))
        for _ in range(3):
            result = pipe.run_cycle()
            self.assertTrue(result["ok"], "the fetch stage is healthy")
            self.assertFalse(result["filter"]["ok"], "a partial filter result must not report success")
        health = self.store.health()
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["filter_consecutive_errors"], 3, "partial results must keep growing the filter streak")
        self.assertTrue(health["filter"]["partial"])
        self.assertIn("missing", health["filter"])
        att = self.store.attention(CFG)
        self.assertTrue(att["needed"])
        self.assertEqual(att["kind"], "filter_failing")

    def test_a_partial_cycle_is_journalled_and_logged(self):
        pipe = self._pipeline(json.dumps({"results": [{"i": 1, "valuable": True, "score": 88}]}))
        pipe.run_cycle()
        stages = [row.get("stage") for row in self.store.recent_failures(50)]
        self.assertIn("filter_partial", stages, "the watchdog RCA journal must show a degraded filter run")
        self.assertTrue(any("partial" in line for line in self.logs), self.logs[-3:])
        section = self.store.health()["filter"]
        self.assertTrue(section["partial"])
        for key in ("missing", "invalid", "ambiguous", "unknown"):
            self.assertIn(key, section, "the counters must reach /health, not just the cycle result")

    def test_a_complete_cycle_still_resets_the_filter_streak(self):
        partial = json.dumps({"results": [{"i": 1, "valuable": True, "score": 88}]})
        pipe = self._pipeline(partial)
        pipe.run_cycle()
        pipe.run_cycle()
        self.assertEqual(self.store.health()["filter_consecutive_errors"], 2)

        def answer_everything(self, key, messages):
            # answer every numbered entry actually sent, whatever the batch size turned out
            indexes = [int(m) for m in re.findall(r"^\[(\d+)\] 标题", messages[-1]["content"], flags=re.M)]
            results = [
                {"i": i, "valuable": i % 2 == 0, "score": 80 if i % 2 == 0 else 20, "category": "c", "reason": "r", "summary": "s"}
                for i in indexes
            ]
            return json.dumps({"results": results})

        with mock.patch.object(filter_mod.Filter, "call_model", answer_everything):
            result = pipe.run_cycle()
        health = self.store.health()
        self.assertTrue(result["filter"]["ok"], "a complete answer is a successful filter run")
        self.assertFalse(result["filter"]["partial"])
        self.assertEqual(health["filter_consecutive_errors"], 0)
        self.assertFalse(health["filter"].get("partial"))


if __name__ == "__main__":
    unittest.main()
