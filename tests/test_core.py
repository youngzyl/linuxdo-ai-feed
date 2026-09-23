"""Unit tests for linuxdo-ai-feed. Run: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collector  # noqa: E402
import config  # noqa: E402
import filter as filter_mod  # noqa: E402
import http_util  # noqa: E402
from store import Store, default_health, parse_iso  # noqa: E402

CFG = {
    "interval_minutes": 15,
    "attention_threshold": 3,
    "filter_consecutive_error_threshold": 3,
    "request_timeout_s": 30,
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444.json",
    "tag_page_url": "https://linux.do/tag/444-tag/444",
    "site_url": "https://linux.do",
    "filter": {"model": "deepseek/deepseek-v4.1-flash", "prompt_version": "v1"},
}


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "state.json", self.dir / "failures.jsonl")

    def tearDown(self):
        self.tmp.cleanup()


# --------------------------------------------------------------------- http_util
class TestBackoff(unittest.TestCase):
    def test_delay_within_jitter_bounds(self):
        rng = random.Random(7)
        schedule = [5, 20, 60, 180, 420]
        for i in range(len(schedule) + 2):
            for _ in range(50):
                delay = http_util.backoff_delay(i, schedule, 0.2, rng)
                base = schedule[min(i, len(schedule) - 1)]
                self.assertGreaterEqual(delay, base * 0.8 - 1e-9)
                self.assertLessEqual(delay, base * 1.2 + 1e-9)

    def test_monotonic_growth(self):
        rng = random.Random(1)
        values = [http_util.backoff_delay(i, [5, 20, 60, 180, 420], 0.2, rng) for i in range(5)]
        self.assertEqual(values, sorted(values))

    def test_empty_schedule_is_zero(self):
        self.assertEqual(http_util.backoff_delay(3, [], 0.2), 0.0)


class TestRetries(unittest.TestCase):
    def test_abort_check_stops_retrying(self):
        slept = []
        ok, value, err = http_util.with_retries(
            lambda i: (_ for _ in ()).throw(RuntimeError("still failing")),
            attempts=5,
            schedule=[1, 2, 4, 8],
            jitter=0,
            abort_check=lambda: True,
            sleep=slept.append,
        )
        self.assertFalse(ok)
        self.assertEqual(len(slept), 0)  # aborted before the first sleep
        self.assertIn("still failing", str(err))

    def test_success_first_try(self):
        calls = []
        ok, value, err = http_util.with_retries(
            lambda i: calls.append(i) or "ok", attempts=3, schedule=[1], jitter=0, sleep=lambda s: None
        )
        self.assertTrue(ok)
        self.assertEqual(value, "ok")
        self.assertEqual(calls, [0])

    def test_retries_then_success(self):
        state = {"n": 0}
        slept = []

        def fn(_i):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("boom")
            return "late"

        ok, value, _ = http_util.with_retries(fn, attempts=5, schedule=[1, 2, 4], jitter=0, sleep=slept.append)
        self.assertTrue(ok)
        self.assertEqual(value, "late")
        self.assertEqual(len(slept), 2)

    def test_exhausted_reports_failures(self):
        seen = []

        def on_fail(attempt, delay, exc):
            seen.append((attempt, delay, str(exc)))

        ok, value, err = http_util.with_retries(
            lambda i: (_ for _ in ()).throw(RuntimeError("nope")),
            attempts=3,
            schedule=[1, 2, 4],
            jitter=0,
            on_attempt_failure=on_fail,
            sleep=lambda s: None,
        )
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertIn("nope", str(err))
        self.assertEqual([s[0] for s in seen], [1, 2, 3])
        self.assertEqual(seen[-1][1], 0.0)  # no sleep after the final attempt


# -------------------------------------------------------------------- collector
class TestHtmlPipeline(unittest.TestCase):
    def test_html_to_text_strips_and_breaks(self):
        raw = '<p>Hello <b>world</b></p><script>var x=1;</script><p>Line2</p><br>:dog_face:'
        text = collector.html_to_text(raw)
        self.assertIn("Hello world", text)
        self.assertIn("Line2", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("dog_face", text)

    def test_entities_unescaped(self):
        self.assertIn("a<b", collector.html_to_text("<p>a&lt;b</p>"))

    def test_extract_json_blob_raw_and_jina(self):
        payload = '{"post_stream":{"posts":[{"cooked":"<p>hi</p>"}]}}'
        self.assertEqual(collector.extract_json_blob(payload)["post_stream"]["posts"][0]["cooked"], "<p>hi</p>")
        wrapped = f"Title: \n\nURL Source: https://linux.do/t/topic/1.json\n\nMarkdown Content:\n{payload}"
        self.assertEqual(collector.extract_json_blob(wrapped)["post_stream"]["posts"], json.loads(payload)["post_stream"]["posts"])

    def test_extract_json_blob_raises_on_garbage(self):
        with self.assertRaises(ValueError):
            collector.extract_json_blob("Just a moment... cloudflare")

    def test_body_uses_op_post_and_caps_length(self):
        class FakeStore:
            def add_failure(self, *a, **k):
                pass

        cfg = dict(CFG)
        cfg["fetch"] = {"body_cap": 10}
        col = collector.Collector(cfg, FakeStore(), lambda *_: None)
        out = col._body_from_topic_json(
            {"title": "t", "post_stream": {"posts": [{"cooked": "<p>0123456789ABCDEF</p>", "username": "u"}]}}
        )
        self.assertEqual(out["body_text"], "0123456789")
        self.assertEqual(out["author"], "u")

    def test_normalize_resolves_author_from_users_map(self):
        cfg = dict(CFG)
        cfg["fetch"] = {}
        col = collector.Collector(cfg, mock.Mock(), lambda *_: None)
        col.categories = {11: "搞七捻三"}
        topic = col._normalize(
            {
                "id": 5,
                "title": "a &amp; b",
                "created_at": "2026-09-20T09:00:00.000Z",
                "category_id": 11,
                "tags": [{"id": 444, "name": "人工智能"}],
                "posters": [{"user_id": 99}],
                "views": 3,
            },
            {99: "someone"},
        )
        self.assertEqual(topic["author"], "someone")
        self.assertEqual(topic["title"], "a & b")
        self.assertEqual(topic["category"], "搞七捻三")
        self.assertEqual(topic["url"], "https://linux.do/t/topic/5")


class TestRssBodies(unittest.TestCase):
    RSS = """<?xml version="1.0"?><rss><channel>
    <item>
      <title>手机agent为什么发展的那么慢！</title>
      <link>https://linux.do/t/topic/2927215</link>
      <description><![CDATA[ <p>想问一下自从豆包手机被封杀后，为什么手机agent发展的那么慢</p><p>25 个帖子</p> ]]></description>
      <pubDate>Sat, 20 Sep 2026 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>post level item</title>
      <link>https://linux.do/t/topic/2916350?page=18#post_362</link>
      <description>plain body</description>
    </item>
    </channel></rss>"""

    def _collector(self, cfg=None):
        cfg = cfg or dict(CFG)
        cfg["fetch"] = {"body_cap": 4000, "use_rss": True}
        col = collector.Collector(cfg, mock.Mock(), lambda *_: None)
        return col

    def test_rss_items_parsed_with_bodies(self):
        col = self._collector()
        with mock.patch.object(http_util, "get", return_value=(200, self.RSS.encode(), {})):
            bodies = col.fetch_rss_bodies()
        self.assertEqual(set(bodies), {2927215, 2916350})
        self.assertIn("为什么手机agent发展的那么慢", bodies[2927215]["body_text"])
        self.assertIn("25 个帖子", bodies[2927215]["body_text"])
        self.assertNotIn("CDATA", bodies[2927215]["body_text"])
        self.assertEqual(bodies[2916350]["body_text"], "plain body")
        self.assertEqual(bodies[2927215]["rss_title"], "手机agent为什么发展的那么慢！")

    def test_rss_url_derived_from_tag_page(self):
        cfg = dict(CFG)
        cfg["fetch"] = {}
        col = collector.Collector(cfg, mock.Mock(), lambda *_: None)
        self.assertEqual(col.rss_url(), "https://linux.do/tag/444-tag/444.rss")

    def test_apply_rss_bodies_only_fills_missing(self):
        store = mock.Mock()
        known = {1: {"id": 1, "detail_fetched": False}, 2: {"id": 2, "detail_fetched": True}}
        store.get.side_effect = lambda tid: known.get(int(tid))
        col = self._collector()
        col.store = store
        result = col.apply_rss_bodies({1: {"body_text": "a"}, 2: {"body_text": "b"}, 99: {"body_text": "c"}})
        self.assertEqual(result, {"rss_items": 3, "rss_matched": 2, "rss_applied": 1})
        store.set_fields.assert_called_once()
        self.assertEqual(store.set_fields.call_args.kwargs["detail_source"], "rss")

    def test_rss_disabled_returns_empty(self):
        cfg = dict(CFG)
        cfg["fetch"] = {"use_rss": False}
        col = collector.Collector(cfg, mock.Mock(), lambda *_: None)
        self.assertEqual(col.fetch_rss_bodies(), {})


class TestListFetchFallback(unittest.TestCase):
    """Cloudflare challenges the tag list JSON while RSS / topic details still answer."""

    BLOB = {
        "topic_list": {
            "topics": [{"id": 7, "title": "hello &amp; world", "posters": []}],
            "users": [],
            "more_topics_url": "/tag/444-tag/444.json?page=1",
        }
    }

    def _collector(self, **fetch):
        cfg = dict(CFG)
        cfg["fetch"] = {
            "jina_prefix": "https://r.jina.ai/",
            "request_timeout_s": 30,
            "max_pages": 6,
            "pages": 1,
            "attempts": 1,
            "backoff_s": [0],
            "jitter": 0.0,
        }
        cfg["fetch"].update(fetch)
        return collector.Collector(cfg, mock.Mock(), lambda *_: None)

    def test_jina_url_encodes_the_target(self):
        col = self._collector()
        url = col.list_jina_url(0)
        self.assertTrue(url.startswith("https://r.jina.ai/https%3A%2F%2Flinux.do%2Ftag%2F444-tag%2F444.json"))
        self.assertIn("%3Forder%3Dcreated%26ascending%3Dfalse%26page%3D0", url)

    def test_direct_success_never_calls_the_proxy(self):
        col = self._collector()
        with mock.patch.object(http_util, "get_json", return_value=self.BLOB) as gj, mock.patch.object(
            http_util, "get"
        ) as g:
            page = col.fetch_page(0)
        self.assertEqual([t["id"] for t in page["topics"]], [7])
        self.assertEqual(page["topics"][0]["title"], "hello & world")
        self.assertEqual(col.last_list_source, "direct")
        g.assert_not_called()
        self.assertEqual(gj.call_args.args[0], "https://linux.do/tag/444-tag/444.json?order=created&ascending=false&page=0")

    def test_challenge_falls_back_to_jina_then_sticks(self):
        col = self._collector()
        challenged = http_util.HttpError(403, "https://linux.do/tag/444-tag/444.json", "error", "Just a moment...")
        wrapped = ("Title: x\nURL Source: y\nMarkdown Content: " + json.dumps(self.BLOB)).encode()
        with mock.patch.object(http_util, "get_json", side_effect=challenged) as gj, mock.patch.object(
            http_util, "get", return_value=(200, wrapped, {})
        ) as g:
            page = col.fetch_page(0)
            self.assertEqual([t["id"] for t in page["topics"]], [7])
            self.assertEqual(col.last_list_source, "jina")
            # the encoded target (not the raw query string) is what the proxy receives
            self.assertTrue(g.call_args.args[0].startswith("https://r.jina.ai/https%3A%2F%2F"))
            # second call skips the challenged direct path entirely (sticky hold)
            gj.side_effect = AssertionError("direct path retried inside the hold window")
            page2 = col.fetch_page(1)
        self.assertEqual([t["id"] for t in page2["topics"]], [7])
        self.assertEqual(gj.call_count, 1)

    def test_hold_expires_and_direct_is_retried(self):
        col = self._collector()
        challenged = http_util.HttpError(403, "u", "error", "")
        with mock.patch.object(http_util, "get_json", side_effect=challenged):
            with mock.patch.object(
                http_util, "get", return_value=(200, json.dumps(self.BLOB).encode(), {})
            ):
                col.fetch_page(0)
        self.assertGreater(col._list_jina_until, time.time())
        col._list_jina_until = 0.0  # hold elapsed
        with mock.patch.object(http_util, "get_json", return_value=self.BLOB) as gj:
            col.fetch_page(0)
        self.assertEqual(col.last_list_source, "direct")
        gj.assert_called_once()

    def test_non_challenge_error_is_not_proxied(self):
        col = self._collector()
        with mock.patch.object(
            http_util, "get_json", side_effect=http_util.HttpError(404, "u", "error", "nope")
        ), mock.patch.object(http_util, "get") as g:
            with self.assertRaises(http_util.HttpError):
                col.fetch_page(0)
        g.assert_not_called()

    def test_fallback_can_be_disabled(self):
        col = self._collector(list_jina_fallback=False)
        with mock.patch.object(
            http_util, "get_json", side_effect=http_util.HttpError(403, "u", "error", "Just a moment...")
        ), mock.patch.object(http_util, "get") as g:
            with self.assertRaises(http_util.HttpError):
                col.fetch_page(0)
        g.assert_not_called()

    def test_list_reports_its_source(self):
        col = self._collector()
        with mock.patch.object(http_util, "get_json", return_value=self.BLOB):
            listing = col.fetch_list(pages=1)
        self.assertEqual(listing["list_source"], "direct")
        self.assertEqual([t["id"] for t in listing["topics"]], [7])


# ----------------------------------------------------------------------- filter
class TestCooldown(unittest.TestCase):
    def setUp(self):
        http_util.reset_cooldown()

    def tearDown(self):
        http_util.reset_cooldown()

    def test_rate_limit_escalates_then_success_resets(self):
        first = http_util.note_rate_limited()
        self.assertGreaterEqual(http_util.cooldown_remaining(), first - 1)
        self.assertEqual(http_util.cooldown_level(), 1)
        second = http_util.note_rate_limited()
        self.assertGreater(second, first)
        self.assertEqual(http_util.cooldown_level(), 2)
        http_util.note_success()
        self.assertEqual(http_util.cooldown_remaining(), 0.0)
        self.assertEqual(http_util.cooldown_level(), 0)

    def test_gate_waits_for_short_cooldown(self):
        slept = []
        http_util._state["until"] = __import__("time").time() + 5
        with mock.patch.object(http_util.time, "sleep", lambda s: slept.append(s)):
            http_util._gate("https://linux.do/x")
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 4)

    def test_gate_refuses_long_cooldown(self):
        http_util._state["until"] = __import__("time").time() + 600
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util._gate("https://linux.do/x")
        self.assertIn("cooling down", str(ctx.exception))

    def test_transport_env_override(self):
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "python"}):
            self.assertEqual(http_util.transport(), "python")
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "auto"}, clear=False):
            self.assertIn(http_util.transport(), {"curl", "python"})


class TestFilterParsing(unittest.TestCase):
    def test_extract_results_plain_object(self):
        rows = filter_mod.extract_results('{"results":[{"i":1,"valuable":true,"score":80}]}')
        self.assertEqual(rows[0]["i"], 1)

    def test_extract_results_with_code_fence_and_prose(self):
        text = 'Sure!\n```json\n{"results":[{"i":2,"valuable":false,"score":10}]}\n```\nDone.'
        rows = filter_mod.extract_results(text)
        self.assertEqual(rows[0]["i"], 2)

    def test_extract_results_bare_array(self):
        rows = filter_mod.extract_results('[{"i":1,"valuable":true,"score":61}]')
        self.assertEqual(len(rows), 1)

    def test_extract_results_raises_on_prose(self):
        with self.assertRaises(ValueError):
            filter_mod.extract_results("I cannot help with that.")

    def test_normalize_score_decides_when_valuable_missing(self):
        high = filter_mod.normalize_verdict({"i": 1, "score": 88}, "m", "v1")
        low = filter_mod.normalize_verdict({"i": 1, "score": 20}, "m", "v1")
        self.assertTrue(high["valuable"])
        self.assertFalse(low["valuable"])

    def test_normalize_coerces_strings_and_truncates(self):
        v = filter_mod.normalize_verdict(
            {"i": "3", "valuable": "true", "score": "75.0", "reason": "x" * 500, "summary": "y" * 500}, "m", "v1"
        )
        self.assertEqual(v["i"], 3)
        self.assertTrue(v["valuable"])
        self.assertEqual(v["score"], 75)
        self.assertLessEqual(len(v["reason"]), 120)
        self.assertLessEqual(len(v["summary"]), 220)

    def test_build_user_prompt_numbers_entries(self):
        prompt = filter_mod.build_user_prompt([{"id": 1, "title": "T1"}, {"id": 2, "title": "T2"}], 50)
        self.assertIn("[1] 标题: T1", prompt)
        self.assertIn("[2] 标题: T2", prompt)
        self.assertIn("未取到正文", prompt)

    def test_judge_batch_maps_indices_and_reports_missing(self):
        class FakeFilter(filter_mod.Filter):
            def call_model(self, key, messages):
                return json.dumps({"results": [{"i": 2, "valuable": True, "score": 90, "reason": "r", "summary": "s"}]})

        f = FakeFilter(CFG, mock.Mock(), lambda *_: None)
        batch = [{"id": 10, "title": "a"}, {"id": 11, "title": "b"}]
        verdicts, unjudged = f.judge_batch("k", batch)
        self.assertEqual(verdicts[0]["topic_id"], 11)
        self.assertEqual([t["id"] for t in unjudged], [10])

    def test_filter_without_key_is_reported_not_raised(self):
        store = mock.Mock()
        store.pending.return_value = [{"id": 1, "title": "x"}]
        f = filter_mod.Filter(CFG, store, lambda *_: None)
        with mock.patch.object(filter_mod, "resolve_api_key", return_value=(None, None)):
            summary = f.run()
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["key_present"])
        self.assertIn("no API key", summary["error"])


class TestLearn(TempStore):
    def test_keep_rescues_a_rejected_topic(self):
        import learn as learn_mod

        self.store.upsert_topics([{"id": 1, "title": "rescued post", "created_at": "2026-09-20T00:00:00Z"}])
        self.store.set_verdict(1, {"valuable": False, "score": 20, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"})
        entry = learn_mod.record(self.store, 1, "keep", "这个方向我要看")
        self.assertEqual(entry["vote"], "keep")
        self.assertEqual(self.store.get(1)["state"], "picked")
        self.assertTrue(self.store.get(1).get("rescued"))
        self.assertEqual(self.store.queue(), [], "a vote never creates a bookmark")
        self.assertEqual(learn_mod.counts(self.store), {"keep": 1, "skip": 0, "total": 1})

    def test_skip_drops_a_pick_and_keeps_its_bookmark(self):
        """Authorized reader contract: 收藏 and 排除 are independent signals."""
        import learn as learn_mod

        self.store.upsert_topics([{"id": 2, "title": "promo", "created_at": "2026-09-20T00:00:00Z"}])
        self.store.set_verdict(2, {"valuable": True, "score": 70, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"})
        self.store.queue_add(2)
        learn_mod.record(self.store, 2, "skip", "又是中转站")
        self.assertEqual(self.store.get(2)["state"], "rejected")
        self.assertEqual(self.store.queue(), [2], "skip must not remove the bookmark")
        self.assertEqual(learn_mod.counts(self.store)["skip"], 1)

    def test_second_vote_replaces_the_first(self):
        import learn as learn_mod

        self.store.upsert_topics([{"id": 3, "title": "t", "created_at": "2026-09-20T00:00:00Z"}])
        learn_mod.record(self.store, 3, "keep")
        learn_mod.record(self.store, 3, "skip")
        self.assertEqual(learn_mod.counts(self.store), {"keep": 0, "skip": 1, "total": 1})

    def test_invalid_vote_is_rejected(self):
        import learn as learn_mod

        self.store.upsert_topics([{"id": 4, "title": "t"}])
        with self.assertRaises(ValueError):
            learn_mod.record(self.store, 4, "maybe")
        with self.assertRaises(KeyError):
            learn_mod.record(self.store, 99, "keep")

    def test_examples_are_injected_into_the_user_prompt(self):
        import learn as learn_mod
        import filter as filter_mod

        self.store.upsert_topics(
            [
                {"id": 10, "title": "我想看的深度帖"},
                {"id": 11, "title": "中转站广告"},
            ]
        )
        learn_mod.record(self.store, 10, "keep", "架构分析我要")
        learn_mod.record(self.store, 11, "skip", "推广")
        text = learn_mod.render_examples(learn_mod.examples_for_prompt(self.store))
        self.assertIn("我想看的深度帖", text)
        self.assertIn("中转站广告", text)
        self.assertIn("架构分析我要", text)
        prompt = filter_mod.build_user_prompt_with_taste([{"id": 12, "title": "新帖"}], text, 50)
        self.assertTrue(prompt.startswith("读者口味"))
        self.assertIn("[1] 标题: 新帖", prompt)

    def test_empty_examples_do_not_change_the_prompt(self):
        import filter as filter_mod

        prompt = filter_mod.build_user_prompt_with_taste([{"id": 1, "title": "x"}], "", 50)
        self.assertTrue(prompt.startswith("请筛选以下帖子"))

    def test_votes_survive_a_state_overwrite_via_the_journal(self):
        import learn as learn_mod
        from store import Store

        self.store.upsert_topics([{"id": 20, "title": "durable vote"}])
        learn_mod.record(self.store, 20, "keep", "别丢")
        journal = self.store.feedback_log
        self.assertTrue(journal.exists(), "votes must be journalled next to state.json")
        # simulate a stale writer clobbering state.json with a snapshot made before the vote
        blob = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        blob["feedback"] = []
        (self.dir / "state.json").write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        rows = learn_mod.all_votes(reloaded)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 20)
        self.assertEqual(rows[0]["vote"], "keep")

    def test_clear_vote_wins_in_the_journal_merge(self):
        import learn as learn_mod
        from store import Store

        self.store.upsert_topics([{"id": 21, "title": "changed my mind"}])
        learn_mod.record(self.store, 21, "keep")
        learn_mod.remove(self.store, 21)
        reloaded = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(learn_mod.all_votes(reloaded), [])


# ------------------------------------------------------------------------ store
class TestCycleLock(unittest.TestCase):
    def test_second_holder_is_refused(self):
        import singleton

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = singleton.CycleLock(path)
            second = singleton.CycleLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class TestStore(TempStore):
    def test_upsert_dedupes_and_keeps_verdict(self):
        self.store.upsert_topics([{"id": 1, "title": "t", "created_at": "2026-09-20T00:00:00Z"}])
        self.store.set_verdict(1, {"valuable": True, "score": 90, "reason": "r", "summary": "s", "model": "m", "prompt_version": "v1"})
        self.store.upsert_topics([{"id": 1, "title": "t2", "created_at": "2026-09-20T00:00:00Z", "views": 42}])
        topic = self.store.get(1)
        self.assertEqual(topic["state"], "picked")
        self.assertEqual(topic["title"], "t2")
        self.assertEqual(topic["views"], 42)
        self.assertEqual(self.store.counts()["all"], 1)

    def test_state_and_queue_persist_across_reload(self):
        self.store.upsert_topics([{"id": 7, "title": "q", "created_at": "2026-09-20T00:00:00Z"}])
        self.store.queue_add(7)
        self.store.save()
        again = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(again.queue(), [7])
        self.assertEqual(again.get(7)["title"], "q")

    def test_queue_set_ignores_unknown_ids_and_dupes(self):
        self.store.upsert_topics([{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
        self.assertEqual(self.store.queue_set([1, 1, 99, "2"]), [1, 2])

    def test_api_payload_sorted_newest_first(self):
        self.store.upsert_topics(
            [
                {"id": 1, "title": "old", "created_at": "2026-09-19T00:00:00Z"},
                {"id": 2, "title": "new", "created_at": "2026-09-20T00:00:00Z"},
            ]
        )
        payload = self.store.api_payload(cfg=CFG, uptime_s=1)
        self.assertEqual([t["id"] for t in payload["topics"]], [2, 1])
        self.assertEqual(payload["counts"], {"all": 2, "picked": 0, "rejected": 0, "pending": 2, "queue": 0})

    def test_attention_after_repeated_failures(self):
        for _ in range(3):
            self.store.record_failure("boom")
        att = self.store.attention(CFG)
        self.assertTrue(att["needed"])
        self.assertEqual(att["kind"], "fetch_failing")
        health = self.store.health_payload(cfg=CFG, uptime_s=10)
        self.assertEqual(health["status"], "failing")

    def test_attention_when_key_missing_with_pending(self):
        self.store.upsert_topics([{"id": 1, "title": "a"}])
        self.store.section_update("filter", key_present=False)
        import store as store_mod

        with mock.patch.object(store_mod, "live_key_present", return_value=False):
            att = self.store.attention(CFG)
        self.assertTrue(att["needed"])
        self.assertEqual(att["kind"], "no_key")
        # a freshly exported key must not raise a false alarm before the next cycle
        with mock.patch.object(store_mod, "live_key_present", return_value=True):
            self.assertFalse(self.store.attention(CFG)["needed"])

    def test_health_ok_after_success_resets_error(self):
        self.store.record_failure("boom")
        self.store.record_success()
        health = self.store.health()
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["last_error"])
        self.assertEqual(health["status"], "ok")
        self.assertFalse(self.store.attention(CFG)["needed"])

    def test_failure_journal_is_written_and_readable(self):
        self.store.add_failure("fetch_list", "HTTP 429 for https://linux.do", attempt=2, context={"page": 0})
        again = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        recent = again.recent_failures(5)
        self.assertEqual(recent[0]["stage"], "fetch_list")
        self.assertEqual(recent[0]["context"]["page"], 0)

    def test_failure_journal_redacts_key(self):
        with mock.patch.object(config, "resolve_api_key", return_value=("sk-supersecret-123456", "env:test")):
            self.store.add_failure("filter_call", "bad auth for sk-supersecret-123456")
        self.assertNotIn("sk-supersecret-123456", self.store.recent_failures(1)[0]["error"])

    def test_section_replace_clears_stale_fields(self):
        self.store.section_update("fetch", rss_error="HTTP 429 from the previous cycle", detail_ok=7)
        fresh = self.store.section_replace("fetch", ok=True, topics_seen=30)
        self.assertNotIn("rss_error", fresh)  # defaulted away, not carried over
        self.assertEqual(fresh["detail_ok"], 0)
        self.assertEqual(fresh["topics_seen"], 30)

    def test_metrics_text_exposes_key_gauges(self):
        self.store.upsert_topics([{"id": 1, "title": "a"}])
        # pending topic + no key => attention 1 (that is the no_key signal)
        text = self.store.metrics_text(cfg=CFG)
        self.assertIn("linuxdo_ai_topics_total 1", text)
        self.assertIn("linuxdo_ai_attention_needed 1", text)
        # once a key is present and the filter has run, attention clears
        self.store.section_update("filter", key_present=True)
        self.assertIn("linuxdo_ai_attention_needed 0", self.store.metrics_text(cfg=CFG))

    def test_corrupt_state_file_does_not_crash(self):
        (self.dir / "state.json").write_text("{not json", encoding="utf-8")
        store = Store(self.dir / "state.json", self.dir / "failures.jsonl")
        self.assertEqual(store.counts()["all"], 0)
        self.assertEqual(store.health()["consecutive_failures"], 0)

    def test_default_health_shape(self):
        self.assertIn("filter", default_health())
        self.assertIsNone(parse_iso("2026-09-20T09:41:00Z") is not None and None)


# ----------------------------------------------------------------------- config
class TestKeyResolution(unittest.TestCase):
    def test_env_var_wins_and_is_case_insensitive(self):
        with mock.patch.dict("os.environ", {"COMMANDCODE_APIKEY": "k-from-env"}, clear=False):
            key, src = config.resolve_api_key()
        self.assertEqual(key, "k-from-env")
        self.assertTrue(src.startswith("env:"))

    def test_dotenv_used_when_env_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("commandcode_apikey=k-from-dotenv\n", encoding="utf-8")
            with mock.patch.object(config, "ROOT", root), mock.patch.dict(
                "os.environ", {"COMMANDCODE_API_KEY": ""}, clear=False
            ):
                src, val = config._read_dotenv_key()
            self.assertEqual(val, "k-from-dotenv")
            self.assertEqual(src, "dotenv")

    def test_no_key_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(config, "ROOT", Path("/nonexistent")):
            self.assertEqual(config.resolve_api_key(), (None, None))

    def test_config_defaults_merge(self):
        cfg = config.load_config(Path("/nonexistent/config.json"))
        self.assertEqual(cfg["filter"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(cfg["fetch"]["attempts"], 5)

    def test_env_overrides_filter_endpoint(self):
        env = {"LINUXDO_AI_FILTER_BASE_URL": "https://example.test/v1", "LINUXDO_AI_FILTER_MODEL": "some/model"}
        with mock.patch.dict("os.environ", env, clear=False):
            cfg = config.load_config(Path("/nonexistent/config.json"))
        self.assertEqual(cfg["filter"]["base_url"], "https://example.test/v1")
        self.assertEqual(cfg["filter"]["model"], "some/model")


if __name__ == "__main__":
    unittest.main()
