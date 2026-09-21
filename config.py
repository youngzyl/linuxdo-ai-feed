"""Configuration and secret resolution for linuxdo-ai-feed.

Secret resolution order for the CommandCode API key (the only secret this app needs):
  1. environment variable `commandcode_apikey`  (any case, e.g. COMMANDCODE_APIKEY)
  2. environment variable `COMMANDCODE_API_KEY`
  3. `.env` file in the project root (`commandcode_apikey=...`)
  4. dev-only pool fallback: env `LINUXDO_AI_ALLOW_POOL_KEY=1` -> ~/.hermes/auth.json
The key value is never logged, never returned by an endpoint, never written to state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
PUBLIC_DIR = ROOT / "public"
FIXTURE_DIR = ROOT / "fixtures"

# The owner token is a *server-side* secret: it authorises writes and owner-only reads.
# It is read from the environment only - never from config.json (which is committed),
# never from a request payload, and it is never logged or returned.
OWNER_TOKEN_ENV = "LINUXDO_AI_OWNER_TOKEN"
OWNER_TOKEN_FILE_ENV = "LINUXDO_AI_OWNER_TOKEN_FILE"
# Exact CORS allowlist, comma separated. No wildcard, no reflection.
ALLOWED_ORIGINS_ENV = "LINUXDO_AI_ALLOWED_ORIGINS"

DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 8791,
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444.json",
    "site_url": "https://linux.do",
    "tag_page_url": "https://linux.do/tag/444-tag/444",
    "pages": 2,
    "interval_minutes": 15,
    "attention_threshold": 3,
    "filter_consecutive_error_threshold": 3,
    "request_timeout_s": 30,
    "fetch": {
        "attempts": 5,
        "backoff_s": [5, 20, 60, 180, 420],
        "jitter": 0.2,
        "detail_delay_s": 2.2,
        "page_gap_s": 2.5,
        "rss_delay_s": 3.0,
        "challenge_wait_s": 8,
        "detail_attempts": 2,
        "detail_max_consecutive_failures": 5,
        "skip_direct_after_challenges": 2,
        "jina_delay_s": 4.0,
        "jina_prefix": "https://r.jina.ai/",
        "use_rss": True,
        "rss_url": "",
        "save_every": 5,
        "body_cap": 4000,
        "detail_max_per_cycle": 15,
        "max_pages": 6,
    },
    "filter": {
        "base_url": "https://api.commandcode.ai/provider/v1",
        "model": "deepseek/deepseek-v4.1-flash",
        "prompt_version": "v1",
        "batch_size": 8,
        "body_chars": 1200,
        "attempts": 3,
        "backoff_s": [4, 12, 30],
        "timeout_s": 180,
        "temperature": 0,
        "zdr": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.json"
    if path.exists():
        try:
            cfg = _deep_merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # a broken config must not brick the service
            raise SystemExit(f"config.json is not valid JSON: {exc}")
    else:
        cfg = json.loads(json.dumps(DEFAULTS))
    return _apply_env_overrides(cfg)


def _apply_env_overrides(cfg: dict) -> dict:
    """Small set of env overrides, mostly for pointing the filter somewhere else
    temporarily (tests, a different gateway) without editing config.json."""
    _, base_url = env_ci("LINUXDO_AI_FILTER_BASE_URL")
    if base_url:
        cfg["filter"]["base_url"] = base_url
    _, model = env_ci("LINUXDO_AI_FILTER_MODEL")
    if model:
        cfg["filter"]["model"] = model
    _, interval = env_ci("LINUXDO_AI_INTERVAL_MINUTES")
    if interval:
        try:
            cfg["interval_minutes"] = float(interval)
        except ValueError:
            pass
    _, pages = env_ci("LINUXDO_AI_PAGES")
    if pages:
        try:
            cfg["pages"] = int(pages)
        except ValueError:
            pass
    return cfg


def env_ci(name: str) -> tuple[str | None, str | None]:
    """Case-insensitive environment lookup; returns (actual_name, value)."""
    for k, v in os.environ.items():
        if k.lower() == name.lower() and v and v.strip():
            return k, v.strip()
    return None, None


def _read_dotenv_key() -> tuple[str | None, str | None]:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return None, None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().lower() in {"commandcode_apikey", "commandcode_api_key"}:
                v = v.strip().strip('"').strip("'")
                if v:
                    return "dotenv", v
    except Exception:
        return None, None
    return None, None


def _read_pool_key() -> tuple[str | None, str | None]:
    if str(os.environ.get("LINUXDO_AI_ALLOW_POOL_KEY", "")).strip() not in {"1", "true", "yes"}:
        return None, None
    auth = Path.home() / ".hermes" / "auth.json"
    if not auth.exists():
        return None, None
    try:
        blob = json.loads(auth.read_text(encoding="utf-8"))
        entries = blob.get("credential_pool", {}).get("commandcode") or []
        for entry in entries:
            key = (entry or {}).get("api_key")
            if key and key.strip():
                return "dev-pool:~/.hermes/auth.json", key.strip()
    except Exception:
        return None, None
    return None, None


def resolve_api_key() -> tuple[str | None, str | None]:
    """Return (key, source_label). Never print the key."""
    name, val = env_ci("commandcode_apikey")
    if val:
        return val, f"env:{name}"
    name, val = env_ci("COMMANDCODE_API_KEY")
    if val:
        return val, f"env:{name}"
    src, val = _read_dotenv_key()
    if val:
        return val, src
    src, val = _read_pool_key()
    if val:
        return val, src
    name, val = env_ci("LINUXDO_AI_FILTER_API_KEY")
    if val:
        return val, f"env:{name}"
    return None, None


def resolve_cookie() -> str | None:
    """Optional linux.do session cookie (env `linuxdo_cookie` or `.env`) for CF/detail paths."""
    _, val = env_ci("linuxdo_cookie")
    if val:
        return val
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().lower().startswith("linuxdo_cookie"):
                return line.split("=", 1)[-1].strip().strip('"').strip("'") or None
    return None


def redact(text: str | None) -> str | None:
    """Strip anything that looks like a bearer key from a log line."""
    if not text:
        return text
    out = text
    key, _ = resolve_api_key()
    if key and len(key) > 8:
        out = out.replace(key, "<redacted>")
    token, _ = owner_token()
    if token and len(token) > 3:
        out = out.replace(token, "<redacted>")
    return out


# ------------------------------------------------------------------- owner boundary
def owner_token() -> tuple[str | None, str | None]:
    """The owner token and a *secret-free* source label, or (None, None).

    Resolution order: `LINUXDO_AI_OWNER_TOKEN`, then the file named by
    `LINUXDO_AI_OWNER_TOKEN_FILE`. A missing/unreadable/empty file resolves to no token,
    which makes every owner-only endpoint fail closed (503).
    """
    _, value = env_ci(OWNER_TOKEN_ENV)
    if value:
        return value, f"env:{OWNER_TOKEN_ENV}"
    _, path = env_ci(OWNER_TOKEN_FILE_ENV)
    if path:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except Exception:
            return None, f"unreadable:{path}"
        if text:
            return text, f"file:{path}"
        return None, f"empty:{path}"
    return None, None


def normalize_origin(value: str | None) -> str | None:
    """`scheme://host:port` with defaults filled in, or None when unusable.

    `*`, a bare host, a path-only string or a non-http(s) scheme all return None: the CORS
    list is an exact allowlist and wildcards are never honoured.
    """
    text = (value or "").strip()
    if not text or text == "*":
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{port}"


def allowed_origins(cfg: dict | None = None) -> set[str]:
    """Exact allowlist of browser origins permitted to call this API.

    `LINUXDO_AI_ALLOWED_ORIGINS` (CSV) wins when set; otherwise the service's own
    origin(s) - same-origin frontends send `Origin` on POST too, so they must be listed.
    """
    _, raw = env_ci(ALLOWED_ORIGINS_ENV)
    out: set[str] = set()
    if raw is not None and raw.strip():
        for chunk in raw.split(","):
            origin = normalize_origin(chunk)
            if origin:
                out.add(origin)
        return out
    host = str((cfg or {}).get("host") or "127.0.0.1")
    try:
        port = int((cfg or {}).get("port") or 8791)
    except (TypeError, ValueError):
        port = 8791
    for candidate in (f"http://{host}:{port}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        origin = normalize_origin(candidate)
        if origin:
            out.add(origin)
    return out
