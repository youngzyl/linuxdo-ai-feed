#!/usr/bin/env python3
"""Deterministic health probe for the linuxdo-ai-feed watchdog.

Designed to be used as a cron *monitor*: the output must be stable while the
collector is healthy, so the agent stays asleep, and must change the moment
something needs a human. No timestamps, no counters that drift.

Two sources, so it works both next to the service and on the host:
  1. HTTP  GET http://127.0.0.1:8791/health      (when the service is reachable)
  2. file  <project>/data/state.json + logs/     (when it is not)

Output lines:
  source=http|file
  service=up|unknown
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

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("LINUXDO_AI_HOST", "127.0.0.1")
PORT = os.environ.get("LINUXDO_AI_PORT", "8791")
STALE_AFTER = float(os.environ.get("LINUXDO_AI_STALE_S", "2700"))


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


def from_http() -> dict | None:
    url = f"http://{HOST}:{PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


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


def main() -> int:
    payload = from_http()
    source = "http"
    state_path = ROOT / "data" / "state.json"
    state_age = (time.time() - state_path.stat().st_mtime) if state_path.exists() else None
    if payload is None:
        # no HTTP route to the service from here (e.g. this probe runs on the host and
        # the collector runs in a container): judge from the state file instead
        source = "file"
        payload = from_files()
        service = "unknown"
    else:
        service = "up"

    attention = bool((payload.get("attention") or {}).get("needed"))
    last_success = parse_iso(payload.get("last_success_at"))
    if last_success is not None:
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
