"""Tiny stdlib HTTP helper: browser-ish headers, gzip, JSON, typed errors.

No third-party dependency on purpose: the app must run anywhere Python 3.11 runs.
TLS verification is never disabled.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

BROWSER_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "accept-encoding": "gzip, deflate",
    "x-requested-with": "XMLHttpRequest",
    "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}

JINA_HEADERS = {
    # r.jina.ai rejects browser-looking agents (403 "Just a moment"); a plain
    # non-browser UA passes. No cookie, no auth, read-only.
    "accept": "text/plain",
}


class HttpError(Exception):
    def __init__(
        self,
        status: int | None,
        url: str,
        message: str,
        body_head: str = "",
        headers: dict | None = None,
    ):
        super().__init__(f"HTTP {status} for {url}: {message}")
        self.status = status
        self.url = url
        self.message = message
        self.body_head = (body_head or "")[:300]
        self.headers = headers or {}

    @property
    def retry_after(self) -> float | None:
        """Server-advised wait, when it sends one."""
        raw = self.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None

    @property
    def is_challenge(self) -> bool:
        """Cloudflare interstitial / rate limit rather than a real answer."""
        if self.status in {403, 429, 503}:
            return True
        low = self.body_head.lower()
        return "just a moment" in low or "cf-chl" in low or "challenges.cloudflare.com" in low


# ---------------------------------------------------------------------------- cooldown
# linux.do rate-limits per IP: once it starts answering 429/403, every further request
# in the same burst is refused too. The client therefore backs off globally (not just
# per retry) and refuses to sit on a long sleep inside a cycle - the caller aborts and
# tries again on the next one.
_COOLDOWN_SCHEDULE = [30.0, 60.0, 180.0, 300.0]
_COOLDOWN_MAX_SLEEP = 75.0
_state = {"until": 0.0, "level": 0}


def cooldown_remaining() -> float:
    return max(0.0, _state["until"] - time.time())


def cooldown_level() -> int:
    return _state["level"]


def note_rate_limited(retry_after: float | None = None) -> float:
    """Register a 429/CF-challenge and extend the global cooldown."""
    level = min(_state["level"], len(_COOLDOWN_SCHEDULE) - 1)
    wait = _COOLDOWN_SCHEDULE[level]
    if retry_after:
        wait = max(wait, min(retry_after, 900.0))  # honour Retry-After, but stay sane
    _state["level"] = min(_state["level"] + 1, len(_COOLDOWN_SCHEDULE))
    _state["until"] = max(_state["until"], time.time() + wait)
    return wait


def note_success() -> None:
    """Any good answer clears the cooldown ladder."""
    _state["level"] = 0
    _state["until"] = 0.0


def reset_cooldown() -> None:
    _state["until"] = 0.0
    _state["level"] = 0


def _decode(resp) -> bytes:
    raw = resp.read()
    enc = (resp.headers.get("content-encoding") or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
    if "deflate" in enc:
        try:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return raw


def _decode_bytes(raw: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
    if "deflate" in enc:
        try:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return raw


def transport() -> str:
    """`auto` (default) prefers curl, `python` forces urllib, `curl` forces curl."""
    mode = (os.environ.get("LINUXDO_AI_TRANSPORT") or "auto").strip().lower()
    if mode == "python":
        return "python"
    if mode == "curl" or (mode == "auto" and shutil.which("curl")):
        return "curl"
    return "python"


def _curl_request(
    url: str, *, method: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, bytes, dict]:
    """curl transport.

    linux.do is behind Cloudflare, which fingerprints the TLS handshake: urllib/httpx
    are answered with 403 no matter how browser-like the headers are, while curl's
    handshake passes. So curl is the default transport (stdlib fallback for hosts
    without it). TLS verification stays on.
    """
    curl = shutil.which("curl") or "curl"
    with tempfile.TemporaryDirectory() as tmp:
        hdr_path = Path(tmp) / "headers"
        body_path = Path(tmp) / "body"
        cmd = [
            curl,
            "-sS",
            "--max-time",
            str(int(max(5, timeout))),
            "-X",
            method,
            "-D",
            str(hdr_path),
            "-o",
            str(body_path),
        ]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        if body is not None:
            cmd += ["--data-binary", "@-"]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            raise HttpError(None, url, f"curl timed out after {timeout}s", "") from None
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise HttpError(None, url, f"curl exit {proc.returncode}: {detail}", "")
        raw = body_path.read_bytes() if body_path.exists() else b""
        head_text = hdr_path.read_text("utf-8", "replace") if hdr_path.exists() else ""
    status = 0
    resp_headers: dict = {}
    content_encoding = ""
    for line in head_text.splitlines():
        if line.startswith("HTTP/"):
            try:
                status = int(line.split()[1])
            except (IndexError, ValueError):
                status = 0
            resp_headers = {}
        elif ":" in line:
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            resp_headers[key] = value
            if key == "content-encoding":
                content_encoding = value
    decoded = _decode_bytes(raw, content_encoding)
    if status >= 400:
        raise HttpError(status, url, "error", decoded.decode("utf-8", "replace"), resp_headers)
    if status == 0:
        raise HttpError(None, url, "no HTTP status line from curl", decoded.decode("utf-8", "replace"))
    return status, decoded, resp_headers


def _cooldown_enabled() -> bool:
    return (os.environ.get("LINUXDO_AI_COOLDOWN") or "1").strip().lower() not in {"0", "false", "no"}


def _gate(url: str) -> None:
    """Wait out a short cooldown, refuse to block on a long one."""
    if not _cooldown_enabled():
        return
    remaining = cooldown_remaining()
    if remaining <= 0:
        return
    if remaining > _COOLDOWN_MAX_SLEEP:
        raise HttpError(None, url, f"cooling down for {remaining:.0f}s after rate limiting", "")
    time.sleep(remaining + 0.5)


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 30,
) -> tuple[int, bytes, dict]:
    _gate(url)
    try:
        if transport() == "curl":
            result = _curl_request(url, method=method, headers=headers or {}, body=body, timeout=timeout)
        else:
            result = _python_request(url, method=method, headers=headers or {}, body=body, timeout=timeout)
    except HttpError as exc:
        if exc.is_challenge and _cooldown_enabled():
            wait = note_rate_limited(exc.retry_after)
            suffix = f" (rate limited; global cooldown {wait:.0f}s"
            suffix += f", Retry-After {exc.retry_after:.0f}s)" if exc.retry_after else ")"
            exc.message = f"{exc.message}{suffix}"
        raise
    if _cooldown_enabled():
        note_success()
    return result


def _python_request(
    url: str, *, method: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        head = ""
        try:
            head = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise HttpError(exc.code, url, exc.reason or "error", head, dict(exc.headers or {})) from None
    except urllib.error.URLError as exc:
        raise HttpError(None, url, f"network error: {exc.reason}", "") from None


def get(url: str, *, headers: dict | None = None, timeout: float = 30) -> tuple[int, bytes, dict]:
    return request(url, headers=headers, timeout=timeout)


def get_json(url: str, *, headers: dict | None = None, timeout: float = 30) -> Any:
    status, raw, _ = get(url, headers=headers, timeout=timeout)
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(status, url, f"not JSON: {exc}", text) from None


def backoff_delay(attempt_index: int, schedule: list[float], jitter: float, rng: random.Random | None = None) -> float:
    """Delay for the attempt that just failed (attempt_index is 0-based)."""
    if not schedule:
        return 0.0
    base = schedule[min(attempt_index, len(schedule) - 1)]
    r = (rng or random).random()
    spread = base * max(0.0, jitter)
    return max(0.0, base - spread + 2 * spread * r)


def with_retries(
    fn,
    *,
    attempts: int,
    schedule: list[float],
    jitter: float = 0.2,
    on_attempt_failure=None,
    abort_check=None,
    sleep=time.sleep,
    rng: random.Random | None = None,
):
    """Run fn(); on exception retry per schedule. Returns (ok, value, last_error).

    on_attempt_failure(attempt_number, delay, exc) is called before each sleep so the
    caller can journal the failure (this is what the RCA feed reads).
    abort_check() is consulted before every retry: when it returns True (e.g. the client
    is in a long rate-limit cooldown) the retry loop stops instead of hammering.
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return True, fn(i), None
        except Exception as exc:  # noqa: BLE001 - journaled, then retried
            last = exc
            delay = backoff_delay(i, schedule, jitter, rng) if i < attempts - 1 else 0.0
            if on_attempt_failure:
                try:
                    on_attempt_failure(i + 1, delay, exc)
                except Exception:
                    pass
            if i < attempts - 1 and abort_check is not None:
                try:
                    if abort_check():
                        return False, None, last
                except Exception:
                    pass
            if delay:
                sleep(delay)
    return False, None, last
