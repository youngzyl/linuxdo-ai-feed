"""Batch 1 / unit 1: a manual override is separate from the model verdict.

Lead contract exercised here:

  * `manual_override` is null/keep/skip; keep => picked, skip => rejected, and only
    otherwise does the latest model verdict (or `pending`) decide the display state.
  * An explicit vote wins over a late model update and survives a restart.
  * Clearing the override exposes the latest model verdict again.
  * Migration derives an override from real feedback records only - the `rescued` /
    `skipped` display flags are not feedback (three real topics in data/state.json carry
    them with no vote row at all).
  * Model provenance/value stays in the filter record even while an override rules.
  * No read/bookmark semantics are introduced yet; the queue effect of a vote stays in
    one place so a later confirmed UX can decouple it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import learn as learn_mod  # noqa: E402
import store as store_mod  # noqa: E402
from store import Store  # noqa: E402

REJECTED = {
    "valuable": False,
    "score": 20,
    "category": "求助",
    "reason": "没有信息量",
    "summary": "s",
    "model": "deepseek/deepseek-v4.1-flash",
    "prompt_version": "v1",
}
PICKED = dict(REJECTED, valuable=True, score=82, category="模型发布")


def legacy_state(topics: dict, *, feedback: list | None = None, queue: list | None = None) -> dict:
    """A state.json blob shaped like the live file (no source_version, no override)."""
    return {
        "version": 1,
        "topics": topics,
        "queue": queue or [],
        "feedback": feedback or [],
        "health": {},
    }


def legacy_topic(tid: int, *, state: str, score: int, **extra) -> dict:
    record = {
        "id": tid,
        "title": f"topic {tid}",
        "url": f"https://linux.do/t/topic/{tid}",
        "created_at": "2026-09-20T00:00:00Z",
        "reply_count": 0,
        "views": 10,
        "like_count": 0,
        "category": "开发调优",
        "tags": ["人工智能"],
        "excerpt": "",
        "body_text": "正文",
        "detail_fetched": True,
        "state": state,
        "filter": {"score": score, "category": "c", "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"},
    }
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

    def write_state(self, blob: dict) -> None:
        (self.dir / "state.json").write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")

    def seed(self, tid: int = 1, *, title: str = "t") -> Store:
        store = self.fresh()
        store.upsert_topics([{"id": tid, "title": title, "created_at": "2026-09-20T00:00:00Z"}])
        return store


class TestReferenceRule(unittest.TestCase):
    """The pure rule from the plan, encoded before the implementation."""

    def test_override_beats_the_model(self):
        self.assertEqual(store_mod.effective_state(False, "keep"), "picked")
        self.assertEqual(store_mod.effective_state(True, "skip"), "rejected")

    def test_model_decides_without_an_override(self):
        self.assertEqual(store_mod.effective_state(True, None), "picked")
        self.assertEqual(store_mod.effective_state(False, None), "rejected")

    def test_unknown_model_verdict_is_pending(self):
        self.assertEqual(store_mod.effective_state(None, None), "pending")

    def test_override_vocabulary_is_null_keep_skip_only(self):
        self.assertEqual(store_mod.OVERRIDES, {"keep", "skip"})


class TestOverrideWins(TempStore):
    def test_late_model_verdict_cannot_overwrite_a_keep(self):
        store = self.seed(1)
        store.set_verdict(1, dict(REJECTED))
        learn_mod.record(store, 1, "keep", "这个方向我要看")
        status = store.set_verdict(1, dict(REJECTED))
        topic = store.get(1)
        self.assertEqual(status, "applied")
        self.assertEqual(store.manual_override(topic), "keep")
        self.assertEqual(topic["state"], "picked", "an explicit keep must survive a later model rejection")
        self.assertFalse(store.model_value(topic), "the model verdict is stored, it just does not rule")

    def test_late_model_verdict_cannot_overwrite_a_skip(self):
        store = self.seed(2)
        store.set_verdict(2, dict(PICKED))
        store.queue_add(2)
        learn_mod.record(store, 2, "skip", "又是中转站")
        store.set_verdict(2, dict(PICKED))
        topic = store.get(2)
        self.assertEqual(store.manual_override(topic), "skip")
        self.assertEqual(topic["state"], "rejected")
        self.assertTrue(store.model_value(topic))

    def test_model_provenance_is_kept_while_an_override_rules(self):
        store = self.seed(3)
        learn_mod.record(store, 3, "keep")
        store.set_verdict(3, dict(REJECTED, reason="更新后的判词"))
        filt = store.get(3)["filter"]
        self.assertEqual(filt["model"], REJECTED["model"])
        self.assertEqual(filt["prompt_version"], "v1")
        self.assertIs(filt["valuable"], False)
        self.assertEqual(filt["score"], 20)
        self.assertEqual(filt["reason"], "更新后的判词")
        self.assertTrue(filt["at"])
        self.assertTrue(filt["source_version"])

    def test_clearing_the_override_exposes_the_latest_model_verdict(self):
        store = self.seed(4)
        store.set_verdict(4, dict(REJECTED))
        learn_mod.record(store, 4, "keep")
        store.set_verdict(4, dict(PICKED))
        self.assertEqual(store.get(4)["state"], "picked")
        state = store.clear_manual_override(4)
        self.assertEqual(state, "picked", "the latest model verdict is a pick")
        self.assertIsNone(store.manual_override(store.get(4)))
        # and the other direction: clear a skip on a model-rejected topic
        store.set_verdict(4, dict(REJECTED))
        learn_mod.record(store, 4, "skip")
        self.assertEqual(store.clear_manual_override(4), "rejected")

    def test_clearing_an_override_without_a_model_verdict_returns_to_pending(self):
        store = self.seed(5)
        learn_mod.record(store, 5, "keep")
        self.assertEqual(store.get(5)["state"], "picked")
        self.assertEqual(store.clear_manual_override(5), "pending")


class TestOverrideDurability(TempStore):
    def test_override_survives_a_restart(self):
        store = self.seed(10)
        store.set_verdict(10, dict(REJECTED))
        learn_mod.record(store, 10, "keep", "别丢")
        again = self.reload()
        topic = again.get(10)
        self.assertEqual(again.manual_override(topic), "keep")
        self.assertEqual(topic["state"], "picked")
        self.assertFalse(again.model_value(topic))

    def test_override_survives_a_stale_state_overwrite_via_the_journal(self):
        store = self.seed(11)
        store.set_verdict(11, dict(PICKED))
        learn_mod.record(store, 11, "skip", "不想看")
        # a stale writer replaces state.json with a snapshot taken before the vote
        self.write_state(legacy_state({"11": legacy_topic(11, state="picked", score=82)}))
        again = self.reload()
        topic = again.get(11)
        self.assertEqual(again.manual_override(topic), "skip")
        self.assertEqual(topic["state"], "rejected")

    def test_a_vote_recorded_after_the_override_field_still_applies(self):
        store = self.seed(12)
        store.set_verdict(12, dict(PICKED))
        learn_mod.record(store, 12, "skip")
        again = self.reload()
        self.assertEqual(again.manual_override(again.get(12)), "skip")


class TestMigration(TempStore):
    def test_display_flags_are_not_feedback(self):
        """rescued/skipped are UI hints; without a vote row they must not become intent."""
        self.write_state(
            legacy_state(
                {
                    "100": legacy_topic(100, state="rejected", score=15, rescued=True, skipped=True),
                    "101": legacy_topic(101, state="picked", score=82, rescued=True),
                }
            )
        )
        store = self.reload()
        self.assertIsNone(store.manual_override(store.get(100)))
        self.assertEqual(store.get(100)["state"], "rejected")
        self.assertIsNone(store.manual_override(store.get(101)))
        self.assertEqual(store.get(101)["state"], "picked")

    def test_an_actual_feedback_record_becomes_the_override(self):
        self.write_state(
            legacy_state(
                {
                    "200": legacy_topic(200, state="picked", score=15, rescued=True),
                    "201": legacy_topic(201, state="rejected", score=62, skipped=True),
                },
                feedback=[
                    {"id": 200, "vote": "keep", "note": "", "at": "2026-09-20T11:07:56Z", "state_at_vote": "rejected"},
                    {"id": 201, "vote": "skip", "note": "", "at": "2026-09-20T17:48:21Z", "state_at_vote": "rejected"},
                ],
            )
        )
        store = self.reload()
        kept, skipped = store.get(200), store.get(201)
        self.assertEqual(store.manual_override(kept), "keep")
        self.assertEqual(kept["state"], "picked")
        self.assertIs(store.model_value(kept), False)
        self.assertEqual(store.manual_override(skipped), "skip")
        self.assertEqual(skipped["state"], "rejected")
        self.assertIs(store.model_value(skipped), False, "a score of 62 must not invent a model pick over the recorded vote state")

    def test_a_cleared_vote_leaves_no_override(self):
        self.write_state(
            legacy_state({"300": legacy_topic(300, state="rejected", score=15)}, feedback=[{"id": 300, "vote": "clear", "at": "2026-09-21T00:00:00Z"}])
        )
        store = self.reload()
        self.assertIsNone(store.manual_override(store.get(300)))

    def test_unknown_override_values_are_refused(self):
        store = self.seed(400)
        with self.assertRaises(ValueError):
            store.set_manual_override(400, "maybe")
        with self.assertRaises(ValueError):
            store.set_manual_override(400, True)
        self.assertEqual(store.set_manual_override(9999, "keep"), "unknown_topic")


class TestQueueEffectStaysIsolated(TempStore):
    """The authorized contract: the bookmark list (queue storage) and a preference vote are two
    independent signals - neither one edits the other."""

    def test_keep_and_skip_never_edit_the_bookmark_list(self):
        store = self.seed(500)
        store.queue_add(500)                  # bookmarked first
        learn_mod.record(store, 500, "keep")
        self.assertEqual(store.queue(), [500], "a keep vote never adds or removes a bookmark")
        self.assertEqual(store.manual_override(store.get(500)), "keep")
        learn_mod.record(store, 500, "skip")
        self.assertEqual(store.queue(), [500], "a skip vote never adds or removes a bookmark")
        self.assertEqual(store.manual_override(store.get(500)), "skip")

    def test_a_bookmark_is_a_queue_edit_only(self):
        store = self.seed(501)
        store.queue_add(501)
        self.assertEqual(store.queue(), [501])
        self.assertIsNone(store.manual_override(store.get(501)), "bookmarking is not a vote")
        store.queue_remove(501)
        self.assertEqual(store.queue(), [])
        self.assertIsNone(store.manual_override(store.get(501)))

    def test_clearing_a_vote_leaves_the_queue_alone(self):
        store = self.seed(502)
        store.queue_add(502)
        learn_mod.record(store, 502, "keep")
        learn_mod.remove(store, 502)
        self.assertEqual(store.queue(), [502], "clearing the vote is not a bookmark edit")
        self.assertIsNone(store.manual_override(store.get(502)))

    def test_a_model_verdict_alone_never_touches_the_queue(self):
        store = self.seed(503)
        store.set_verdict(503, dict(PICKED))
        self.assertEqual(store.queue(), [], "a model verdict is not a bookmark")


class TestNoServerSideReadState(TempStore):
    """Authorized reader contract: 已读 lives in the browser and 收藏 reuses the queue storage,
    so the topic record still gains no read/bookmark fields (no schema change)."""

    def test_topic_record_gains_no_read_or_bookmark_state(self):
        store = self.seed(600)
        store.set_verdict(600, dict(PICKED))
        learn_mod.record(store, 600, "keep")
        topic = store.get(600)
        for forbidden in ("read", "read_at", "unread", "bookmark", "bookmarked", "saved", "starred"):
            self.assertNotIn(forbidden, topic, f"{forbidden!r} would be a server-side read state the contract keeps browser-local")
        self.assertIn(topic["manual_override"], (None, "keep", "skip"))

    def test_the_api_payload_still_carries_state_and_flags(self):
        store = self.seed(601)
        store.set_verdict(601, dict(REJECTED))
        learn_mod.record(store, 601, "keep")
        payload = store.api_payload(cfg={"tag": "t", "tag_page_url": "u", "tag_url": "u", "filter": {"model": "m", "prompt_version": "v1"}}, uptime_s=1)
        topic = payload["topics"][0]
        self.assertEqual(topic["state"], "picked")
        self.assertTrue(topic["rescued"])
        self.assertEqual(payload["counts"]["picked"], 1)


if __name__ == "__main__":
    unittest.main()
