"""Tiny stdlib HTTP helper: browser-ish headers, gzip, JSON, typed errors.

No third-party dependency on purpose: the app must run anywhere Python 3.11 runs.
TLS verification is never disabled.
"""
from __future__ import annotations

import email.utils
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
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


# ------------------------------------------------------------------- clock + origins
# Every time reading in this module goes through `_clock` so a test can freeze time
# (Retry-After HTTP-dates and the cooldown ladder are relative to it). Assign a callable
# to replace it: `http_util._clock = lambda: 1_000_000.0`.
_clock = time.time

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
# The scope used when no usable URL is given. It is linux.do because the collector's
# phase-abort checks call the no-argument form (`collector.py:238`) and only ever mean
# linux.do traffic. It is written already normalized (`origin_of` form) so no-argument
# callers and `https://linux.do/...` callers share one bucket.
DEFAULT_ORIGIN = "https://linux.do:443"


def _now() -> float:
    return float(_clock())


def origin_of(url: str | None) -> str:
    """Normalized cooldown scope for a URL: `scheme://host:effective-port`.

    The port is the explicit one, or the scheme default (http 80 / https 443), so
    `https://example.test/x` and `https://example.test:443/y` share a scope while
    `https://example.test:8443/z` and `http://example.test/x` do not. A URL without a
    usable scheme+host (a bare path, an empty string) falls back to `DEFAULT_ORIGIN`.
    """
    try:
        parts = urlsplit(str(url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return DEFAULT_ORIGIN
    if not scheme or not host:
        return DEFAULT_ORIGIN
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)
    return f"{scheme}://{host}:{port}"


def _parse_http_date(text: str) -> float | None:
    """Absolute epoch seconds for an RFC 1123 / RFC 850 / asctime date, else None."""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
        """Server-advised wait in seconds, from `Retry-After` seconds or an HTTP-date.

        The HTTP-date form is converted against the injectable clock (`_clock`), so the
        value is a delta, not an absolute timestamp. Unparseable values return None.
        """
        return self.retry_after_at(_now())

    def retry_after_at(self, now: float | None = None) -> float | None:
        """`retry_after` measured against an explicit `now` (epoch seconds)."""
        raw = self.headers.get("retry-after")
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        absolute = _parse_http_date(text)
        if absolute is None:
            return None
        base = _now() if now is None else float(now)
        return max(0.0, absolute - base)

    @property
    def is_challenge(self) -> bool:
        """Cloudflare interstitial / rate limit rather than a real answer."""
        if self.status in {403, 429, 503}:
            return True
        low = self.body_head.lower()
        return "just a moment" in low or "cf-chl" in low or "challenges.cloudflare.com" in low


# ---------------------------------------------------------------------------- cooldown
# linux.do rate-limits per IP: once it starts answering 429/403, every further request
# in the same burst is refused too. The client therefore backs off (not just per retry)
# and refuses to sit on a long sleep inside a cycle - the caller aborts and tries again
# on the next one.
#
# The cooldown is kept PER ORIGIN (scheme + host + effective port). One global cell meant
# that unrelated upstreams blocked each other: a provider 429 could abort the collector
# phase, and any successful provider response cleared linux.do's backoff. Untargeted calls
# (`note_rate_limited()`, `cooldown_remaining()`) stay on `DEFAULT_ORIGIN`, so the linux.do
# callers keep their behaviour.
_COOLDOWN_SCHEDULE = [30.0, 60.0, 180.0, 300.0]
_COOLDOWN_MAX_SLEEP = 75.0
_states: dict[str, dict] = {}


def _state_for(origin: str) -> dict:
    state = _states.get(origin)
    if state is None:
        state = {"until": 0.0, "level": 0}
        _states[origin] = state
    return state


# Legacy module-level view: the linux.do scope. Kept as the same dict object so code
# (and older tests) that poke `_state["until"]` still target the linux.do cooldown.
_state = _state_for(DEFAULT_ORIGIN)


def cooldown_remaining(url: str | None = None) -> float:
    return max(0.0, _state_for(origin_of(url))["until"] - _now())


def cooldown_level(url: str | None = None) -> int:
    return _state_for(origin_of(url))["level"]


def note_rate_limited(retry_after: float | None = None, url: str | None = None) -> float:
    """Register a 429/CF-challenge for this origin and extend its cooldown."""
    state = _state_for(origin_of(url))
    level = min(state["level"], len(_COOLDOWN_SCHEDULE) - 1)
    wait = _COOLDOWN_SCHEDULE[level]
    if retry_after:
        wait = max(wait, min(retry_after, 900.0))  # honour Retry-After, but stay sane
    state["level"] = min(state["level"] + 1, len(_COOLDOWN_SCHEDULE))
    state["until"] = max(state["until"], _now() + wait)
    return wait


def note_success(url: str | None = None) -> None:
    """A good answer clears the cooldown ladder of *that* origin only."""
    state = _state_for(origin_of(url))
    state["level"] = 0
    state["until"] = 0.0


def reset_cooldown(url: str | None = None) -> None:
    """Clear one origin's cooldown, or every origin when no URL is given."""
    if url is not None:
        state = _state_for(origin_of(url))
        state["level"] = 0
        state["until"] = 0.0
        return
    for state in _states.values():
        state["level"] = 0
        state["until"] = 0.0
    _states.clear()
    _states[DEFAULT_ORIGIN] = _state  # keep the legacy object identity


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
    return transport_for(None)


# Headers whose value is a credential. They must never travel as curl command-line
# arguments: any process in the same namespace can read another process's argv.
CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization", "x-api-key")


def carries_credentials(headers: dict | None) -> bool:
    return any(str(k).strip().lower() in CREDENTIAL_HEADERS for k in (headers or {}))


def transport_for(headers: dict | None = None) -> str:
    """Transport for a request with these headers.

    Credentialed requests always use the in-process stdlib client (bypassing the env
    override): `_curl_request` would expose the bearer/cookie in `-H` arguments. Public
    scraping keeps curl, whose TLS handshake passes Cloudflare where urllib is refused.
    """
    mode = (os.environ.get("LINUXDO_AI_TRANSPORT") or "auto").strip().lower()
    if carries_credentials(headers):
        return "python"
    if mode == "python":
        return "python"
    if mode == "curl" or (mode == "auto" and shutil.which("curl")):
        return "curl"
    return "python"


class _RefuseAllRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of replaying the request somewhere else.

    urllib's stock handler rebuilds the request for the `Location` target and re-attaches the
    original headers (all but `Content-*`), so a 302 answered to a credentialed call hands
    `Authorization` / `Cookie` to whatever host the response names - and an `http://` target
    would silently downgrade the https hop too. No provider or owner endpoint this client
    talks to needs a redirect, so a credentialed request treats any 3xx as final.

    Returning None makes OpenerDirector fall through to its default error handling, which
    raises HTTPError(code): callers keep seeing the status, headers and body they already
    handle (`request()` still maps it onto HttpError, rate-limit cooldowns included).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CREDENTIALED_OPENER: urllib.request.OpenerDirector | None = None


def _credentialed_opener() -> urllib.request.OpenerDirector:
    """The opener used for requests that carry a credential.

    Built with `build_opener`, so the default `HTTPSHandler` - and therefore the default
    *verifying* SSL context - stays in the chain; only the redirect handler is replaced.
    Certificate and hostname verification are never weakened here.
    """
    global _CREDENTIALED_OPENER
    if _CREDENTIALED_OPENER is None:
        _CREDENTIALED_OPENER = urllib.request.build_opener(_RefuseAllRedirects())
    return _CREDENTIALED_OPENER


def _curl_request(
    url: str, *, method: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, bytes, dict]:
    """curl transport.

    linux.do is behind Cloudflare, which fingerprints the TLS handshake: urllib/httpx
    are answered with 403 no matter how browser-like the headers are, while curl's
    handshake passes. So curl is the default transport (stdlib fallback for hosts
    without it). TLS verification stays on.

    Refuses outright when the headers carry a credential (see `transport_for`): those go
    through the in-process client so the secret never lands in this process's argv.
    """
    if carries_credentials(headers):
        raise HttpError(None, url, "refusing to pass credentials on the curl command line", "")
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
    """Wait out a short cooldown for this origin, refuse to block on a long one."""
    if not _cooldown_enabled():
        return
    remaining = cooldown_remaining(url)
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
        if transport_for(headers) == "curl":
            result = _curl_request(url, method=method, headers=headers or {}, body=body, timeout=timeout)
        else:
            result = _python_request(url, method=method, headers=headers or {}, body=body, timeout=timeout)
    except HttpError as exc:
        if exc.is_challenge and _cooldown_enabled():
            wait = note_rate_limited(exc.retry_after, url=url)
            suffix = f" (rate limited; cooldown for {origin_of(url)} {wait:.0f}s"
            suffix += f", Retry-After {exc.retry_after:.0f}s)" if exc.retry_after else ")"
            exc.message = f"{exc.message}{suffix}"
        raise
    if _cooldown_enabled():
        note_success(url)
    return result


def _python_request(
    url: str, *, method: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    # Credentialed requests never follow a redirect (see `_RefuseAllRedirects`); public
    # requests keep the stock urlopen behaviour, redirects included.
    opener = _credentialed_opener().open if carries_credentials(headers) else urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:
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
