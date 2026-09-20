#!/usr/bin/env python3
"""Host-side watchdog wrapper.

Hermes cron monitor/script paths must live in ~/.hermes/scripts/, but this project
lives in the sandbox workspace. Copy this file there once (see README, "Watchdog cron"):

  cp <workspace>/linuxdo-ai/scripts/host_monitor_wrapper.py ~/.hermes/scripts/linuxdo_ai_monitor.py

and register a cron job with monitor=linuxdo_ai_monitor.py. It simply runs the project's
own deterministic probe (scripts/monitor.py), which reads the collector's state files when
the service is not reachable over HTTP from the host.
"""
from __future__ import annotations

import os
import subprocess
import sys

PROJECT = os.environ.get(
    "LINUXDO_AI_DIR", "/home/young/.hermes/sandboxes/docker/default/workspace/linuxdo-ai"
)
TARGET = os.path.join(PROJECT, "scripts", "monitor.py")

if not os.path.exists(TARGET):
    print("service=unknown")
    print("attention=1")
    print("consecutive_failures=unknown")
    print("stale=1")
    print("last_error=project_directory_missing")
    sys.exit(0)

sys.exit(subprocess.call([sys.executable, TARGET]))
