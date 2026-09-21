#!/usr/bin/env python3
"""Deterministic health probe for the linuxdo-ai-feed watchdog.

Designed to be used as a cron *monitor*: the output must be stable while the
collector is healthy, so the agent stays asleep, and must change the moment
something needs a human. No timestamps, no counters that drift.

Sources, in order:
  1. `LINUXDO_AI_HEALTH_URL` - an explicit remote health endpoint, e.g.
     https://tcstw.youngzyl.me:8443/linuxdo-api/health. If it is set, it is the ONLY
     source: when the remote probe fails the probe reports source=http/service=down/
     attention=1/stale=1 and never falls back to the local files, because a stale-but-
     healthy local snapshot would mask a real outage (that is exactly the failure mode of
     a cutover to a remote deployment). TLS is always verified and the timeout is bounded.
  2. no explicit URL: GET http://<LINUXDO_AI_HOST>:<PORT>/health (the local service).
  3. no explicit URL and no local HTTP route (typical for a probe that runs on the host
     while the collector runs in a container): judge from <project>/data/state.json.

A configured URL that is not a usable absolute https URL is *rejected* - output
source=config/attention=1 - rather than silently treated as "no override".

Output lines:
  source=http|file|config
  service=up|down|unknown
  attention=0|1
  consecutive_failures=N
  stale=0|1
  last_error=<sanitised signature>
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("LINUXDO_AI_HOST", "127.0.0.1")
PORT = os.environ.get("LINUXDO_AI_PORT", "8791")
STALE_AFTER = float(os.environ.get("LINUXDO_AI_STALE_S", "2700"))
HEALTH_URL_ENV = "LINUXDO_AI_HEALTH_URL"
# Bounded: a monitor probe must not hang a cron tick.
HEALTH_TIMEOUT_S = float(os.environ.get("LINUXDO_AI_HEALTH_TIMEOUT_S", "6"))


def sanitize(text) -> str:
    if not text:
        return "-"
    out = re.sub(r"[^A-Za-z0-9_. :-]", " ", str(text))
    out = re.sub(r"\s+", " ", out).strip()[:70]
    return out or "-"


def parse_iso(value) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def explicit_health_url() -> str | None:
    """The configured remote health endpoint, or None when no override is configured.

    An empty/whitespace value means "not configured" on purpose: it is how a deployment
    disables the override without unsetting the variable.
    """
    raw = os.environ.get(HEALTH_URL_ENV)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def health_url_error(url: str) -> str | None:
    """Why this configured URL is unusable, or None when it is fine.

    Requires an absolute https URL with a host and no embedded credentials. Plain http is
    refused even for a loopback address: the override exists for the remote deployment, and
    an unverified-hops probe could be forged to hide an outage.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"unparseable ({exc})"
    if parts.scheme.lower() != "https":
        return "scheme must be https"
    if not parts.hostname:
        return "no host"
    if parts.username or parts.password:
        return "must not embed credentials"
    return None


def fetch_health(url: str) -> dict | None:
    """GET a health payload over verified TLS, bounded by HEALTH_TIMEOUT_S.

    urllib's default context verifies certificates and hostnames; nothing here overrides it.
    """
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def local_health_url() -> str:
    return f"http://{HOST}:{PORT}/health"


def failure_payload(reason: str, error: str) -> dict:
    """A failure payload whose printed signature survives `sanitize`.

    monitor.py prints `attention.reason` in preference to `last_error` (historical
    behaviour), so the stable machine signature is embedded in the reason text as well -
    the watchdog's signature and the cron monitor's before/after diff both see it.
    """
    return {
        "attention": {"needed": True, "reason": f"{error}: {reason}"},
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_error": error,
    }


def from_files() -> dict:
    state_path = ROOT / "data" / "state.json"
    if not state_path.exists():
        return {"attention": {"needed": True, "reason": "no state file - service never started"},
                "consecutive_failures": 0, "last_success_at": None, "last_error": "no_state_file"}
    try:
        blob = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"attention": {"needed": True, "reason": f"state file unreadable: {exc}"},
                "consecutive_failures": 0, "last_success_at": None, "last_error": "state_unreadable"}
    health = blob.get("health") or {}
    topics = blob.get("topics") or {}
    pending = sum(1 for t in topics.values() if t.get("state") == "pending")
    key_present = bool((health.get("filter") or {}).get("key_present"))
    failures = int(health.get("consecutive_failures") or 0)
    filter_errors = int(health.get("filter_consecutive_errors") or 0)
    needed = failures >= 3 or filter_errors >= 3 or (pending > 0 and not key_present)
    reason = health.get("last_error") or (health.get("filter") or {}).get("last_error") or ""
    if pending > 0 and not key_present:
        reason = reason or "no filter key"
    return {
        "attention": {"needed": needed, "reason": reason},
        "consecutive_failures": failures,
        "last_success_at": health.get("last_success_at"),
        "last_error": health.get("last_error") or (health.get("filter") or {}).get("last_error"),
    }


def select_payload() -> tuple[dict, str, str, bool]:
    """(payload, source, service, forced_stale)."""
    explicit = explicit_health_url()
    if explicit is not None:
        problem = health_url_error(explicit)
        if problem is not None:
            # fail loudly instead of pretending no override was configured
            return (
                failure_payload(f"configured health URL rejected: {problem}", "bad_health_url"),
                "config",
                "down",
                True,
            )
        payload = fetch_health(explicit)
        if payload is None:
            host = urlsplit(explicit).hostname or "unknown"
            return (
                failure_payload(
                    f"remote health probe failed: {host}",
                    f"health_unreachable:{host}",
                ),
                "http",
                "down",
                True,
            )
        return payload, "http", "up", False

    payload = fetch_health(local_health_url())
    if payload is not None:
        return payload, "http", "up", False
    # no HTTP route to the service from here (e.g. this probe runs on the host and the
    # collector runs in a container): judge from the state file instead
    return from_files(), "file", "unknown", False


def main() -> int:
    state_path = ROOT / "data" / "state.json"
    state_age = (time.time() - state_path.stat().st_mtime) if state_path.exists() else None
    payload, source, service, forced_stale = select_payload()

    attention = bool((payload.get("attention") or {}).get("needed"))
    last_success = parse_iso(payload.get("last_success_at"))
    if forced_stale:
        stale = 1
    elif last_success is not None:
        stale = int((time.time() - last_success) > STALE_AFTER)
    else:
        # No cycle has finished yet: only stale once we are past the boot grace period,
        # otherwise every fresh start would page the watchdog.
        uptime = payload.get("uptime_s")
        if uptime is not None:
            stale = int(float(uptime) > STALE_AFTER)
        else:
            stale = 0 if (state_age is not None and state_age < STALE_AFTER) else 1
    # a collector that stopped writing is as much a problem as a failing one
    attention = attention or bool(stale)

    print(f"source={source}")
    print(f"service={service}")
    print(f"attention={1 if attention else 0}")
    print(f"consecutive_failures={int(payload.get('consecutive_failures') or 0)}")
    print(f"stale={stale}")
    print(f"last_error={sanitize((payload.get('attention') or {}).get('reason') or payload.get('last_error'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
