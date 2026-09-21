"""R2 regression tests: rate-limit cooldown must be scoped per URL origin.

Reported defect: `http_util` keeps one global `_state` cooldown (http_util.py:89) that every
request reads through `_gate`, and `note_success` clears it on *any* successful response
(http_util.py:264-265). Traffic to unrelated upstreams (linux.do, r.jina.ai and the model
provider) therefore blocks and un-blocks each other: a provider 429 can abort the collector
phase, and a successful provider call can wipe out linux.do's rate-limit backoff.

Fix shape: cooldown state is keyed by normalized origin (scheme + host + effective port).
`DEFAULT_ORIGIN` is the linux.do scope, so existing no-argument callers
(`cooldown_remaining()`, `note_success()`, `collector.py:238`) keep their meaning and the
public linux.do request path is untouched. `Retry-After` accepts seconds and HTTP-date, both
measured against an injectable clock.

Run: python3 -m unittest tests.test_http_policy -v
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_util  # noqa: E402

PROVIDER = "https://api.commandcode.ai/provider/v1/chat/completions"
LINUXDO = "https://linux.do/tag/444-tag/444.json"
JINA = "https://r.jina.ai/https%3A%2F%2Flinux.do%2Ftag"


class CooldownCase(unittest.TestCase):
    def setUp(self):
        http_util.reset_cooldown()

    def tearDown(self):
        http_util.reset_cooldown()


class TestOriginNormalization(CooldownCase):
    def test_scheme_host_and_effective_port(self):
        self.assertEqual(http_util.origin_of(LINUXDO), "https://linux.do:443")
        self.assertEqual(http_util.origin_of("https://linux.do:443/x"), http_util.origin_of(LINUXDO))
        self.assertEqual(http_util.origin_of("http://linux.do/x"), "http://linux.do:80")
        self.assertEqual(http_util.origin_of("https://LINUX.DO/x"), http_util.origin_of(LINUXDO))
        # scheme and non-default port are part of the scope
        self.assertNotEqual(http_util.origin_of("http://linux.do/x"), http_util.origin_of(LINUXDO))
        self.assertNotEqual(http_util.origin_of("https://linux.do:8443/x"), http_util.origin_of(LINUXDO))
        self.assertNotEqual(http_util.origin_of(LINUXDO), http_util.origin_of(PROVIDER))
        self.assertNotEqual(http_util.origin_of(LINUXDO), http_util.origin_of(JINA))

    def test_unparseable_url_falls_back_to_the_linuxdo_scope(self):
        """Legacy callers pass bare paths ('u' in older tests); they must keep working."""
        self.assertEqual(http_util.origin_of("u"), http_util.DEFAULT_ORIGIN)
        self.assertEqual(http_util.DEFAULT_ORIGIN, http_util.origin_of("https://linux.do"))
        self.assertEqual(http_util.origin_of(""), http_util.DEFAULT_ORIGIN)


class TestScopingBehaviour(CooldownCase):
    def test_rate_limit_on_one_origin_does_not_block_another(self):
        wait = http_util.note_rate_limited(url=PROVIDER)
        self.assertGreater(wait, 0)
        self.assertGreater(http_util.cooldown_remaining(PROVIDER), 0)
        self.assertEqual(http_util.cooldown_remaining(LINUXDO), 0.0)
        slept: list[float] = []
        with mock.patch.object(http_util.time, "sleep", lambda s: slept.append(s)):
            http_util._gate(LINUXDO)  # must neither sleep nor raise
        self.assertEqual(slept, [])
        with mock.patch.object(http_util.time, "sleep", lambda s: slept.append(s)):
            http_util._gate(PROVIDER)  # provider is gated (short cooldown => waits)
        self.assertEqual(len(slept), 1)

    def test_success_on_one_origin_cannot_clear_another(self):
        http_util.note_rate_limited(url=PROVIDER)
        http_util.note_rate_limited(url=LINUXDO)
        http_util.note_success(url=PROVIDER)
        self.assertEqual(http_util.cooldown_remaining(PROVIDER), 0.0)
        self.assertEqual(http_util.cooldown_level(PROVIDER), 0)
        self.assertGreater(
            http_util.cooldown_remaining(LINUXDO),
            0,
            "a provider success must not clear linux.do's cooldown",
        )
        self.assertEqual(http_util.cooldown_level(LINUXDO), 1)

    def test_ladder_is_per_origin(self):
        http_util.note_rate_limited(url=PROVIDER)
        second = http_util.note_rate_limited(url=PROVIDER)
        self.assertEqual(http_util.cooldown_level(PROVIDER), 2)
        self.assertEqual(http_util.cooldown_level(LINUXDO), 0)
        fresh = http_util.note_rate_limited(url=LINUXDO)
        self.assertLess(fresh, second, "a fresh origin restarts the ladder")
        self.assertEqual(http_util.cooldown_level(LINUXDO), 1)

    def test_long_cooldown_refuses_only_its_own_origin(self):
        http_util.note_rate_limited(retry_after=900.0, url=PROVIDER)
        self.assertGreater(http_util.cooldown_remaining(PROVIDER), http_util._COOLDOWN_MAX_SLEEP)
        with self.assertRaises(http_util.HttpError) as ctx:
            http_util._gate(PROVIDER)
        self.assertIn("cooling down", str(ctx.exception))
        with mock.patch.object(http_util.time, "sleep", lambda s: None):
            http_util._gate(LINUXDO)  # unaffected origin

    def test_legacy_linuxdo_callers_keep_their_scope(self):
        """No-argument calls still mean linux.do (what collector.py:238 relies on)."""
        http_util.note_rate_limited()  # legacy signature
        slept: list[float] = []
        self.assertGreater(http_util.cooldown_remaining(), 0)
        # the same bucket as an explicit linux.do url (the reads are microseconds apart)
        self.assertAlmostEqual(http_util.cooldown_remaining(), http_util.cooldown_remaining(LINUXDO), delta=1.0)
        self.assertEqual(http_util.origin_of(None), http_util.origin_of(LINUXDO))
        # the legacy module-level state object is the linux.do scope
        self.assertGreater(http_util._state["until"], 0.0)
        with mock.patch.object(http_util.time, "sleep", lambda s: slept.append(s)):
            http_util._gate("https://linux.do/x")
        self.assertEqual(len(slept), 1)
        # reset_cooldown() with no url clears every scope
        http_util.reset_cooldown()
        self.assertEqual(http_util.cooldown_remaining(LINUXDO), 0.0)
        self.assertEqual(http_util._state["until"], 0.0)


class TestRetryAfter(CooldownCase):
    def test_seconds_are_read_directly(self):
        err = http_util.HttpError(429, "u", "error", "", {"retry-after": "120"})
        self.assertEqual(err.retry_after, 120.0)

    def test_http_date_is_measured_with_the_injected_clock(self):
        err = http_util.HttpError(429, "u", "error", "", {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        now = datetime(2026, 10, 21, 7, 27, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(http_util, "_clock", lambda: now):
            self.assertAlmostEqual(err.retry_after, 60.0, delta=0.5)
        # the same clock drives the cooldown arithmetics (no real sleep involved)
        with mock.patch.object(http_util, "_clock", lambda: now):
            wait = http_util.note_rate_limited(err.retry_after, url=PROVIDER)
            self.assertAlmostEqual(wait, 60.0, delta=0.5)
            self.assertAlmostEqual(http_util.cooldown_remaining(PROVIDER), 60.0, delta=0.5)

    def test_http_date_in_the_past_is_zero_not_negative(self):
        err = http_util.HttpError(429, "u", "error", "", {"retry-after": "Wed, 21 Oct 2026 07:00:00 GMT"})
        now = datetime(2026, 10, 21, 7, 27, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(http_util, "_clock", lambda: now):
            self.assertEqual(err.retry_after, 0.0)

    def test_unparseable_retry_after_is_none(self):
        for value in ("soon", "", "  ", "0x10"):
            err = http_util.HttpError(429, "u", "error", "", {"retry-after": value})
            self.assertIsNone(err.retry_after, f"{value!r} should not be a wait")

    def test_missing_header_is_none(self):
        self.assertIsNone(http_util.HttpError(429, "u", "error", "", {}).retry_after)

    def test_retry_after_is_still_capped(self):
        err = http_util.HttpError(429, "u", "error", "", {"retry-after": "86400"})
        self.assertLessEqual(http_util.note_rate_limited(err.retry_after, url=PROVIDER), 900.0)


class TestRequestIntegration(CooldownCase):
    """The public request() path must scope both the failure and the success."""

    def test_failed_provider_call_cools_only_the_provider(self):
        def fake_python_request(url, **kwargs):
            if "commandcode" in url:
                raise http_util.HttpError(429, url, "rate limited", "slow down", {"retry-after": "45"})
            return 200, b"ok", {}

        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "python"}), mock.patch.object(
            http_util, "_python_request", side_effect=fake_python_request
        ):
            with self.assertRaises(http_util.HttpError):
                http_util.request(PROVIDER)
            self.assertGreater(http_util.cooldown_remaining(PROVIDER), 0)
            self.assertEqual(http_util.cooldown_remaining(LINUXDO), 0.0)
            # a successful linux.do request must not clear the provider's cooldown
            status, body, _ = http_util.request(LINUXDO)
            self.assertEqual((status, body), (200, b"ok"))
            self.assertGreater(
                http_util.cooldown_remaining(PROVIDER),
                0,
                "success on linux.do cleared the provider cooldown",
            )

    def test_gated_origin_refuses_a_request_rather_than_sleeping_inside_a_cycle(self):
        http_util.note_rate_limited(retry_after=900.0, url=PROVIDER)
        with mock.patch.dict("os.environ", {"LINUXDO_AI_TRANSPORT": "python"}):
            with self.assertRaises(http_util.HttpError) as ctx:
                http_util.request(PROVIDER)
            self.assertIn("cooling down", str(ctx.exception))


class TestClockIsInjectable(CooldownCase):
    def test_clock_hook_is_used_for_remaining(self):
        frozen = time.time() + 10_000  # ahead of the real clock, so the frozen cooldown survives the patch
        with mock.patch.object(http_util, "_clock", lambda: frozen):
            http_util.note_rate_limited(url=PROVIDER)
            self.assertAlmostEqual(http_util.cooldown_remaining(PROVIDER), http_util._COOLDOWN_SCHEDULE[0], delta=0.01)
        # outside the patch the real clock is back: the deadline is absolute, so the
        # remaining time is measured against real now, not against no cooldown at all
        remaining = http_util.cooldown_remaining(PROVIDER)
        self.assertGreater(remaining, 10_000)
        self.assertAlmostEqual(remaining, 10_000 + http_util._COOLDOWN_SCHEDULE[0], delta=5)
        self.assertAlmostEqual(http_util._clock(), time.time(), delta=5)


if __name__ == "__main__":
    unittest.main()
