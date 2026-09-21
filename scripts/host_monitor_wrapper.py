#!/usr/bin/env python3
"""Host-side watchdog wrapper.

Hermes cron monitor/script paths must live in ~/.hermes/scripts/, but this project
lives in the sandbox workspace. Copy this file there once (see README, "Watchdog cron"):

  cp <workspace>/linuxdo-ai/scripts/host_monitor_wrapper.py ~/.hermes/scripts/linuxdo_ai_monitor.py

and register a cron job with monitor=linuxdo_ai_monitor.py. It runs the project's own
deterministic probe (scripts/monitor.py), which reads the collector's state files when the
service is not reachable over HTTP from the host.

Remote deployment: when `<project>/deploy/active-target.json` exists and contains a usable
`{"health_url": "https://..."}`, that URL is exported as `LINUXDO_AI_HEALTH_URL` so the probe
watches the deployed service instead of the local files. This file is written by the owner at
cutover - this wrapper never creates it. Without it the wrapper behaves exactly as before.

Failure policy: a present-but-unusable target file fails LOUD (attention=1) instead of
silently reverting to the local-file mode, which would hide a remote outage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ENV = "LINUXDO_AI_DIR"
DEFAULT_PROJECT = "/home/young/.hermes/sandboxes/docker/default/workspace/linuxdo-ai"
TARGET_NAME = "deploy/active-target.json"
HEALTH_URL_ENV = "LINUXDO_AI_HEALTH_URL"


def project_dir() -> Path:
    """The project directory, resolved per call (a cron may export LINUXDO_AI_DIR)."""
    return Path(os.environ.get(PROJECT_ENV) or DEFAULT_PROJECT)


def fail(error: str) -> None:
    print("source=config")
    print("service=down")
    print("attention=1")
    print("consecutive_failures=0")
    print("stale=1")
    print(f"last_error={error}")


def target_url(project: Path) -> tuple[str | None, str | None]:
    """(url, error): the configured remote health URL, or a reason it is unusable.

    (None, None) means "no target file": the caller keeps the previous local behaviour.
    """
    path = project / TARGET_NAME
    if not path.exists():
        return None, None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "bad_active_target: unreadable JSON"
    url = (blob or {}).get("health_url") if isinstance(blob, dict) else None
    if not isinstance(url, str) or not url.strip():
        return None, "bad_active_target: health_url missing"
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, "bad_active_target: unparseable health_url"
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        return None, "bad_active_target: health_url must be an absolute https URL"
    return url, None


def main() -> int:
    project = project_dir()
    target = project / "scripts" / "monitor.py"
    if not target.exists():
        print("source=file")
        print("service=unknown")
        print("attention=1")
        print("consecutive_failures=unknown")
        print("stale=1")
        print("last_error=project_directory_missing")
        return 0

    url, error = target_url(project)
    if error is not None:
        fail(error)
        return 0

    env = dict(os.environ)
    if url:
        env[HEALTH_URL_ENV] = url
    else:
        # no cutover file: drop any inherited override so the wrapper's behaviour is the
        # historical local one, not something an interactive shell happened to export
        env.pop(HEALTH_URL_ENV, None)
    return subprocess.call([sys.executable, str(target)], env=env)


if __name__ == "__main__":
    sys.exit(main())
