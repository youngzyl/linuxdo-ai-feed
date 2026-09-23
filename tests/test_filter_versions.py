"""Batch 1 / unit 2: `source_version` - which bytes a verdict actually judged.

Lead contract exercised here:

  * A version is derived from normalized research-relevant content: title plus the body
    once one exists, otherwise the excerpt. View/reply/like/bump counters and the detail
    bookkeeping flag are excluded - they change on every refresh without changing text.
  * Body enrichment (RSS upsert, detail fetch) changes the version and creates new work.
  * Every verdict records the version it judged; an answer for older bytes is refused and
    the topic stays eligible for its *current* version.
  * Eligibility is independent of the manual override: enriched content is re-judged even
    while an explicit keep/skip still rules the display.
  * Legacy (versionless) data binds each existing verdict to its initial version, so
    migration must not re-judge the whole backlog; later content changes invalidate.
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

import collector as collector_mod  # noqa: E402
import filter as filter_mod  # noqa: E402
import learn as learn_mod  # noqa: E402
import store as store_mod  # noqa: E402
from store import Store  # noqa: E402

VERDICT = {
    "valuable": True,
    "score": 82,
    "category": "模型发布",
    "reason": "有新版本号",
    "summary": "s",
    "model": "deepseek/deepseek-v4.1-flash",
    "prompt_version": "v1",
}


def topic(tid: int = 1, **extra) -> dict:
    record = {
        "id": tid,
        "title": f"标题 {tid}",
        "created_at": "2026-09-20T00:00:00Z",
        "reply_count": 0,
        "views": 10,
        "like_count": 0,
        "bumped_at": "2026-09-20T00:10:00Z",
        "category": "开发调优",
        "tags": ["人工智能"],
        "excerpt": "列表里的短摘录",
        "body_text": "",
        "detail_fetched": False,
        "state": "pending",
        "filter": None,
    }
    record.update(extra)
    return record


def legacy_topic(tid: int, *, state: str, score: int = 82, **extra) -> dict:
    """Record shaped like the live pre-migration file: no source_version anywhere."""
    record = topic(tid, state=state, body_text="正文已经取到", detail_fetched=True)
    record["filter"] = {"score": score, "category": "c", "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"}
    record.update(extra)
    return record


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fresh(self) -> Store:
        return Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def reload(self) -> Store:
        return Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def write_state(self, topics: dict, **extra) -> None:
        blob = {"version": 1, "topics": topics, "queue": [], "feedback": [], "health": {}}
        blob.update(extra)
        (self.dir / "state.json").write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")

    def seed(self, tid: int = 1, **extra) -> Store:
        store = self.fresh()
        store.upsert_topics([topic(tid, **extra)])
        return store


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_shape_is_computed_by_code(self):
        version = store_mod.source_fingerprint(topic())
        pattern = rf"^{re.escape(store_mod.SOURCE_VERSION_SCHEME)}:[0-9a-f]{{{store_mod.SOURCE_VERSION_HEX}}}$"
        self.assertRegex(version, pattern)

    def test_popularity_and_bookkeeping_are_excluded(self):
        base = store_mod.source_fingerprint(topic())
        noisy = topic(views=9999, reply_count=42, like_count=7, bumped_at="2026-09-21T00:00:00Z", detail_fetched=True)
        self.assertEqual(store_mod.source_fingerprint(noisy), base, "counters must not create new work")

    def test_real_content_changes_the_fingerprint(self):
        base = store_mod.source_fingerprint(topic())
        self.assertNotEqual(store_mod.source_fingerprint(topic(title="换个标题")), base)
        self.assertNotEqual(store_mod.source_fingerprint(topic(body_text="新正文")), base)

    def test_whitespace_and_width_variants_are_the_same_version(self):
        base = store_mod.source_fingerprint(topic(title="ＡＩ  发布"))
        self.assertEqual(store_mod.source_fingerprint(topic(title="AI 发布")), base)
        with_newlines = topic(title="AI 发布", body_text="第一行\n\n第二行  结束")
        self.assertEqual(
            store_mod.source_fingerprint(topic(title="AI 发布", body_text="第一行 第二行 结束")),
            store_mod.source_fingerprint(with_newlines),
        )

    def test_excerpt_stands_in_until_a_body_arrives(self):
        listed = topic(body_text="", excerpt="只拿到摘录")
        enriched = dict(listed, body_text="完整正文")
        self.assertNotEqual(
            store_mod.source_fingerprint(listed),
            store_mod.source_fingerprint(enriched),
            "body enrichment must be a new version",
        )

    def test_an_empty_body_falls_back_to_the_excerpt(self):
        self.assertEqual(
            store_mod.source_fingerprint(topic(body_text="", excerpt="摘录")),
            store_mod.source_fingerprint(topic(body_text="   ", excerpt="摘录")),
        )


class TestVersionIsMaintained(TempStore):
    def test_upsert_refreshes_counters_without_moving_the_version(self):
        store = self.seed(1)
        before = store.get(1)["source_version"]
        store.upsert_topics([topic(1, views=500, reply_count=9, bumped_at="2026-09-21T00:00:00Z")])
        self.assertEqual(store.get(1)["source_version"], before)
        self.assertEqual(store.get(1)["views"], 500)

    def test_upsert_with_an_edited_title_moves_the_version(self):
        store = self.seed(2)
        before = store.get(2)["source_version"]
        store.upsert_topics([topic(2, title="运营改了标题")])
        self.assertNotEqual(store.get(2)["source_version"], before)
        self.assertEqual(store.get(2)["source_version"], store.source_version(store.get(2)))

    def test_set_fields_moves_the_version_only_for_content(self):
        store = self.seed(3)
        before = store.get(3)["source_version"]
        store.set_fields(3, detail_fetched=True, detail_source="rss")
        self.assertEqual(store.get(3)["source_version"], before, "a bookkeeping flag is not new content")
        store.set_fields(3, body_text="详情正文")
        self.assertNotEqual(store.get(3)["source_version"], before)

    def test_the_rss_upsert_path_moves_the_version(self):
        store = self.fresh()
        store.upsert_topics([topic(4, excerpt="RSS 之前的摘录")])
        before = store.get(4)["source_version"]
        col = collector_mod.Collector({"_data_dir": str(self.dir)}, store, lambda *_: None)
        col.apply_rss_bodies({4: {"body_text": "RSS 送来的正文"}})
        self.assertTrue(store.get(4)["detail_fetched"])
        self.assertNotEqual(store.get(4)["source_version"], before)
        self.assertTrue(store.needs_model_verdict(store.get(4)))


class TestVerdictVersioning(TempStore):
    def test_a_verdict_records_the_version_it_judged(self):
        store = self.seed(10)
        dispatched = store.get(10)["source_version"]
        self.assertEqual(store.set_verdict(10, dict(VERDICT), source_version=dispatched), "applied")
        self.assertEqual(store.verdict_version(store.get(10)), dispatched)
        self.assertEqual(store.get(10)["filter"]["source_version"], dispatched)

    def test_a_stale_verdict_is_refused_and_the_current_version_stays_eligible(self):
        store = self.seed(11)
        dispatched = store.get(11)["source_version"]
        # the body arrives while the batch is in flight
        store.set_fields(11, body_text="迟到的正文", detail_fetched=True)
        current = store.get(11)["source_version"]
        self.assertNotEqual(dispatched, current)
        self.assertEqual(store.set_verdict(11, dict(VERDICT), source_version=dispatched), "stale")
        record = store.get(11)
        self.assertIsNone(record.get("filter"), "an answer for older bytes must not be written")
        self.assertEqual(record["state"], "pending")
        self.assertEqual([t["id"] for t in store.pending()], [11], "the current version is still pending work")

    def test_a_verdict_for_the_current_version_applies_after_the_stale_one(self):
        store = self.seed(12)
        dispatched = store.get(12)["source_version"]
        store.set_fields(12, body_text="新正文")
        current = store.get(12)["source_version"]
        self.assertEqual(store.set_verdict(12, dict(VERDICT), source_version=current), "applied")
        self.assertEqual(store.get(12)["state"], "picked")
        self.assertEqual(store.pending(), [])

    def test_a_legacy_versionless_caller_still_applies_and_binds_to_current(self):
        store = self.seed(13)
        self.assertEqual(store.set_verdict(13, dict(VERDICT)), "applied")
        self.assertEqual(store.verdict_version(store.get(13)), store.get(13)["source_version"])
        self.assertEqual(store.pending(), [])

    def test_unknown_topic_is_reported_not_silently_ignored(self):
        store = self.seed(14)
        self.assertEqual(store.set_verdict(9999, dict(VERDICT)), "unknown_topic")

    def test_enriched_content_is_re_judged_even_with_an_override(self):
        store = self.seed(15)
        store.set_verdict(15, dict(VERDICT, valuable=False, score=10))
        learn_mod.record(store, 15, "keep")
        self.assertEqual(store.pending(), [], "nothing to do while the bytes are unchanged")
        store.set_fields(15, body_text="正文终于到了", detail_fetched=True)
        self.assertEqual([t["id"] for t in store.pending()], [15], "new bytes are new work")
        self.assertEqual(store.manual_override(store.get(15)), "keep")
        status = store.set_verdict(15, dict(VERDICT), source_version=store.get(15)["source_version"])
        self.assertEqual(status, "applied")
        self.assertEqual(store.get(15)["state"], "picked", "the override still rules the display")
        self.assertTrue(store.model_value(store.get(15)), "and the model verdict is now recorded")


class TestLegacyMigration(TempStore):
    def test_existing_verdicts_are_bound_to_their_initial_version(self):
        self.write_state(
            {
                "500": legacy_topic(500, state="picked", score=82),
                "501": legacy_topic(501, state="rejected", score=15),
            }
        )
        store = self.reload()
        self.assertEqual(store.model_value(store.get(500)), True)
        self.assertEqual(store.model_value(store.get(501)), False)
        self.assertEqual(store.get(500)["state"], "picked")
        self.assertEqual(store.get(501)["state"], "rejected")
        self.assertEqual(store.get(500)["filter"]["source_version"], store.get(500)["source_version"])
        self.assertEqual([t["id"] for t in store.pending()], [], "migration must not re-judge the backlog")

    def test_a_legacy_topic_without_any_verdict_is_still_pending_work(self):
        never_judged = legacy_topic(503, state="pending")
        never_judged["filter"] = None
        self.write_state({"503": never_judged, "504": legacy_topic(504, state="rejected", score=15)})
        store = self.reload()
        self.assertEqual([t["id"] for t in store.pending()], [503])

    def test_a_versionless_verdict_without_a_value_is_eligible_again(self):
        self.write_state({"600": legacy_topic(600, state="pending", score=82), "601": legacy_topic(601, state="rejected", score=15)})
        store = self.reload()
        self.assertIsNone(store.model_value(store.get(600)))
        self.assertEqual([t["id"] for t in store.pending()], [600], "an unknown value has to be judged, not assumed")
        self.assertEqual([t["id"] for t in store.pending()], [600])
        self.assertEqual(store.get(601)["state"], "rejected")

    def test_migration_is_idempotent_across_restarts(self):
        self.write_state({"700": legacy_topic(700, state="picked", score=82), "701": legacy_topic(701, state="pending", score=82)})
        first = self.reload()
        first.save()
        second = self.reload()
        self.assertEqual([t["id"] for t in first.pending()], [701])
        self.assertEqual([t["id"] for t in second.pending()], [701], "a restart must not invent work")
        self.assertEqual(second.get(700)["source_version"], first.get(700)["source_version"])

    def test_only_the_changed_topic_is_invalidated_after_migration(self):
        self.write_state({"800": legacy_topic(800, state="picked", score=82), "801": legacy_topic(801, state="rejected", score=15)})
        store = self.reload()
        store.set_fields(801, body_text="801 的正文被更新了")
        self.assertEqual([t["id"] for t in store.pending()], [801])
        self.assertEqual(store.get(800)["state"], "picked")

    def test_a_restart_after_an_override_still_has_no_extra_work(self):
        self.write_state(
            {"900": legacy_topic(900, state="picked", score=82)},
            feedback=[{"id": 900, "vote": "keep", "at": "2026-09-20T11:00:00Z", "state_at_vote": "rejected"}],
        )
        store = self.reload()
        self.assertEqual(store.manual_override(store.get(900)), "keep")
        self.assertEqual(store.get(900)["state"], "picked")
        self.assertEqual(store.pending(), [])


class TestDispatchVersionSnapshot(unittest.TestCase):
    """The filter must dispatch against a captured version, not "whatever is current"."""

    CFG = {
        "interval_minutes": 15,
        "attention_threshold": 3,
        "filter_consecutive_error_threshold": 3,
        "tag": "人工智能",
        "tag_url": "https://linux.do/tag/444-tag/444.json",
        "tag_page_url": "https://linux.do/tag/444-tag/444",
        "site_url": "https://linux.do",
        "filter": {"model": "deepseek/deepseek-v4.1-flash", "prompt_version": "v1", "batch_size": 8},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _filter(self, call_model):
        f = filter_mod.Filter(self.CFG, self.store, lambda *_: None)
        f.call_model = call_model
        return f

    def test_run_rejects_a_verdict_whose_bytes_changed_mid_flight(self):
        self.store.upsert_topics([topic(30, excerpt="派发时的摘录")])

        def call_model(key, messages):
            # the RSS upsert lands while the provider call is in flight
            self.store.set_fields(30, body_text="模型思考期间到达的正文", detail_fetched=True)
            return json.dumps({"results": [{"i": 1, "valuable": True, "score": 90, "category": "c", "reason": "r", "summary": "s"}]})

        f = self._filter(call_model)
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=("k", "env:test")):
            summary = f.run()
        record = self.store.get(30)
        self.assertIsNone(record.get("filter"), "a verdict for older bytes must not be committed")
        self.assertEqual([t["id"] for t in self.store.pending()], [30])
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["judged"], 0)
        self.assertFalse(summary["ok"], "a run that lands no verdict must not look like a full success")

    def test_run_commits_a_verdict_when_the_bytes_did_not_move(self):
        self.store.upsert_topics([topic(31, excerpt="摘录")])

        def call_model(key, messages):
            return json.dumps({"results": [{"i": 1, "valuable": True, "score": 90, "category": "c", "reason": "r", "summary": "s"}]})

        f = self._filter(call_model)
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=("k", "env:test")):
            summary = f.run()
        record = self.store.get(31)
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["stale"], 0)
        self.assertEqual(record["state"], "picked")
        self.assertEqual(record["filter"]["source_version"], record["source_version"])


if __name__ == "__main__":
    unittest.main()
