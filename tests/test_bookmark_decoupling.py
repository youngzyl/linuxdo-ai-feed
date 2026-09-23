"""Authorized reader contract: 收藏 (bookmarks) are independent of preference votes.

Settled contract exercised here:

  * the third column is 收藏 (bookmarks); it reuses the existing queue persistence
    (`state.json` "queue", `GET/POST /api/queue`) and every existing queue entry is
    preserved as a bookmark - no migration invents or drops one,
  * an explicit bookmark add/remove NEVER records keep/skip,
  * an explicit keep/skip NEVER adds or removes a bookmark,
  * bookmark membership does not depend on the model's valuable flag: a rejected
    topic can be bookmarked and keeps showing under 收藏,
  * Batch 1 semantics stay intact: a vote is still the manual override, a late model
    verdict cannot overwrite it and the content source version is not disturbed.

Everything runs against a throwaway Store and a loopback server on 127.0.0.1:0 -
no project state, no outbound network.

Run: python3 -m unittest tests.test_bookmark_decoupling -v
"""
from __future__ import annotations

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

import learn as learn_mod  # noqa: E402
import server as server_mod  # noqa: E402
from store import Store  # noqa: E402

OWNER_TOKEN = "test-owner-token-bookmarks-4c71"
PICKED_ID = 11
REJECTED_ID = 22

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

MODEL_PICKED = {"valuable": True, "score": 80, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"}
MODEL_REJECTED = {"valuable": False, "score": 10, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"}


class BookmarkContract(unittest.TestCase):
    """One throwaway store + one loopback server per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.store.upsert_topics(
            [
                {"id": PICKED_ID, "title": "picked one", "created_at": "2026-09-21T00:00:00Z", "tags": ["人工智能"]},
                {"id": REJECTED_ID, "title": "rejected one", "created_at": "2026-09-21T00:01:00Z", "tags": ["人工智能"]},
            ]
        )
        self.store.set_verdict(PICKED_ID, dict(MODEL_PICKED))
        self.store.set_verdict(REJECTED_ID, dict(MODEL_REJECTED))
        self.store.save()
        self.logs: list[str] = []
        self.cfg = dict(CFG)
        app = server_mod.App(self.cfg, self.store, mock.Mock(running=False), self.logs.append)
        patcher = mock.patch.dict(os.environ, {"LINUXDO_AI_OWNER_TOKEN": OWNER_TOKEN, "LINUXDO_AI_ALLOWED_ORIGINS": ""}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = server_mod.build_server(self.cfg, app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def req(self, method, path, body=None, *, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        payload = json.dumps(body) if body is not None else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        data = json.loads(raw.decode("utf-8")) if raw.strip() else None
        return response.status, data

    # --------------------------------------------------------------- the contract

    def test_keep_vote_does_not_add_a_bookmark(self):
        status, payload = self.req("POST", "/api/feedback", {"id": REJECTED_ID, "vote": "keep"}, token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["topic"]["state"], "picked", "keep still rescues a rejected topic")
        self.assertTrue(payload["topic"]["rescued"])
        self.assertEqual(payload["queue"], [], "keep must not bookmark the topic")
        self.assertEqual(self.store.queue(), [], "no bookmark was added")
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(reloaded.queue(), [])

    def test_skip_vote_does_not_remove_a_bookmark(self):
        self.store.queue_add(PICKED_ID)  # 收藏 it explicitly
        self.store.save()
        status, payload = self.req("POST", "/api/feedback", {"id": PICKED_ID, "vote": "skip"}, token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["topic"]["state"], "rejected", "skip still drops the pick")
        self.assertTrue(payload["topic"]["skipped"])
        self.assertEqual(payload["queue"], [PICKED_ID], "skip must not remove the bookmark")
        self.assertEqual(self.store.queue(), [PICKED_ID])

    def test_clear_vote_leaves_bookmarks_alone(self):
        self.store.queue_add(PICKED_ID)
        self.store.save()
        status, payload = self.req("POST", "/api/feedback", {"id": PICKED_ID, "vote": "clear"}, token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"], [PICKED_ID])
        self.assertEqual(self.store.queue(), [PICKED_ID])

    def test_a_rejected_topic_can_be_bookmarked_and_stays_bookmarked(self):
        status, payload = self.req("POST", "/api/queue", {"add": REJECTED_ID}, token=OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"], [REJECTED_ID], "a rejected topic is a first-class bookmark")
        self.assertEqual(payload["counts"]["queue"], 1)
        # its display state is still the model's verdict (or the manual override)
        self.assertEqual(self.store.get(REJECTED_ID)["state"], "rejected")
        # and a later vote on it does not drop the bookmark
        self.req("POST", "/api/feedback", {"id": REJECTED_ID, "vote": "keep"}, token=OWNER_TOKEN)
        self.assertEqual(self.store.queue(), [REJECTED_ID])

    def test_bookmark_toggle_records_no_vote_and_no_override(self):
        self.req("POST", "/api/queue", {"add": PICKED_ID}, token=OWNER_TOKEN)
        self.req("POST", "/api/queue", {"remove": PICKED_ID}, token=OWNER_TOKEN)
        self.assertEqual(self.store.queue(), [])
        self.assertEqual(self.store.feedback_rows(), [], "bookmarking is not a preference signal")
        topic = self.store.get(PICKED_ID)
        self.assertIsNone(topic.get("manual_override"), "bookmarking must not create an override")
        self.assertEqual(topic["state"], "picked", "the model verdict still decides the column")

    def test_bookmarked_topic_keeps_its_display_state(self):
        self.req("POST", "/api/queue", {"add": REJECTED_ID}, token=OWNER_TOKEN)
        self.req("POST", "/api/queue", {"add": PICKED_ID}, token=OWNER_TOKEN)
        self.assertEqual(self.store.get(REJECTED_ID)["state"], "rejected")
        self.assertEqual(self.store.get(PICKED_ID)["state"], "picked")
        status, payload = self.req("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(payload["queue"]), sorted([PICKED_ID, REJECTED_ID]))

    def test_existing_queue_entries_are_the_initial_bookmarks(self):
        """Migration must not invent or drop bookmarks: the stored list is the list."""
        self.store.queue_add(PICKED_ID)
        self.store.queue_add(REJECTED_ID)
        self.store.save()
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(reloaded.queue(), [PICKED_ID, REJECTED_ID])

    def test_vote_does_not_disturb_the_content_source_version(self):
        before = self.store.source_version(self.store.get(PICKED_ID))
        learn_mod.record(self.store, PICKED_ID, "skip", "no")
        after = self.store.source_version(self.store.get(PICKED_ID))
        self.assertEqual(before, after, "Batch 1: a vote never moves the content version")

    # --------------------------------------------------------- learn (unit level)

    def test_learn_effects_are_override_only(self):
        learn_mod.record(self.store, REJECTED_ID, "keep")
        self.assertEqual(self.store.get(REJECTED_ID)["state"], "picked")
        self.assertEqual(self.store.queue(), [], "learn.keep must not bookmark")
        learn_mod.record(self.store, PICKED_ID, "skip")
        self.assertEqual(self.store.get(PICKED_ID)["state"], "rejected")
        self.assertEqual(self.store.queue(), [], "learn.skip must not touch bookmarks")

    def test_late_model_verdict_still_cannot_overwrite_a_vote(self):
        learn_mod.record(self.store, PICKED_ID, "skip")
        self.store.set_verdict(PICKED_ID, dict(MODEL_REJECTED, model="m2", prompt_version="v2"))
        self.assertEqual(self.store.get(PICKED_ID)["state"], "rejected")
        self.assertEqual(self.store.manual_override(self.store.get(PICKED_ID)), "skip")


if __name__ == "__main__":
    unittest.main()
