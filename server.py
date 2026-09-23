"""HTTP server: static frontend + JSON API + monitoring endpoints.

Endpoints
  GET  /                     static (public/)
  GET  /api/state            full payload for the page (CONTRACT.md §1)
  POST /api/refresh          kick a cycle in the background
  GET  /api/queue            收藏 (bookmark) ids - the third column
  POST /api/queue            replace bookmarks ({"queue":[id,...]} or {"add":id} / {"remove":id})
  GET  /health               status + attention flag  (watchdog reads this)
  GET  /metrics              Prometheus text
  GET  /api/failures?n=20    RCA feed (newest first)
  GET  /fixtures/<file>      dev fixtures (frontend ?fixture=1)

The `queue` wire name is kept for compatibility (the state file and the deployed client
use it); the reader UI treats it as the bookmark list. An explicit vote (POST
/api/feedback) only moves the manual override - it never edits this list.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from config import FIXTURE_DIR, PUBLIC_DIR, allowed_origins, normalize_origin, owner_token
from store import now_iso
import learn as learn_mod

MAX_BODY = 64 * 1024

# Methods that can change state. Anything outside this set on a known route is a 405.
ALLOWED_METHODS = ("GET", "POST", "OPTIONS")
# Endpoints that need the owner token. Reads of /api/state, /health, /metrics, /api/queue,
# the static files and the fixtures stay anonymous.
OWNER_ROUTES = {
    ("POST", "/api/queue"),
    ("POST", "/api/feedback"),
    ("POST", "/api/refresh"),
    ("GET", "/api/feedback"),   # raw owner notes
    ("GET", "/api/failures"),   # raw upstream errors
}
PREFLIGHT_HEADERS = ("content-type", "authorization")
PREFLIGHT_METHODS = ("GET", "POST")


def bearer_token(header_value: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, or None."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def token_matches(candidate: str | None, expected: str | None) -> bool:
    """Constant-time comparison; False whenever either side is missing."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _within_roots(target: Path, roots: list[Path]) -> bool:
    """Is `target` one of `roots` or below one of them?

    Compares resolved ancestry (`target == root or root in target.parents`) instead of
    string prefixes: a sibling directory named `public-other` shares the character prefix
    of `public` but is not inside it, so `startswith` let it through. Both arguments are
    expected to be resolved paths.
    """
    for root in roots:
        if target == root or root in target.parents:
            return True
    return False


def _topic_brief(topic: dict | None) -> dict | None:
    """The few topic fields a mutation response needs to keep the UI in sync."""
    if not topic:
        return None
    return {
        "id": int(topic.get("id") or 0),
        "state": topic.get("state"),
        "rescued": bool(topic.get("rescued")),
        "skipped": bool(topic.get("skipped")),
    }


class App:
    """Holds the shared objects the handler needs."""

    def __init__(self, cfg: dict, store, pipeline, logger):
        self.cfg = cfg
        self.store = store
        self.pipeline = pipeline
        self.log = logger
        self.started_at = time.time()
        self.refresh_lock = threading.Lock()

    def uptime(self) -> float:
        return time.time() - self.started_at

    def trigger_refresh(self) -> dict:
        if self.pipeline.running:
            return {"ok": True, "running": True, "job": {"stage": "running", "started_at": None}}
        if not self.refresh_lock.acquire(blocking=False):
            return {"ok": True, "running": True, "job": {"stage": "queued", "started_at": None}}

        def work():
            try:
                self.pipeline.run_cycle()
            except Exception as exc:  # already journaled inside the pipeline
                self.log(f"manual refresh crashed: {type(exc).__name__}: {exc}")
            finally:
                self.refresh_lock.release()

        threading.Thread(target=work, name="manual-refresh", daemon=True).start()
        return {"ok": True, "running": True, "job": {"stage": "fetch", "started_at": now_iso()}}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "linuxdo-ai-feed/1.0"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):  # route access logs into our logger
            app.log(f'http {self.address_string()} {fmt % args}')

        def _send(self, status: int, body: bytes, content_type: str, *, headers: dict | None = None) -> bool:
            """Write a response. Returns True so callers can `return self._send(...)` as a
            'handled' signal - a refusal helper that returned None would let the route
            continue and perform the very mutation it just refused."""
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            # CORS response headers for the exact request origin (if it is allowed), plus
            # Vary: Origin so a shared cache never serves one origin's grant to another.
            # No Access-Control-Allow-Credentials: the API never uses cookies.
            merged = dict(getattr(self, "_cors", None) or {})
            merged.setdefault("vary", "Origin")
            merged.update(headers or {})
            for key, value in merged.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True

        def json_response(self, status: int, payload, *, headers: dict | None = None) -> bool:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return self._send(status, body, "application/json; charset=utf-8", headers=headers)

        def error_json(self, status: int, message: str, *, headers: dict | None = None) -> bool:
            return self.json_response(status, {"error": message, "status": status}, headers=headers)

        def _cors_headers(self, origin: str | None) -> dict:
            """Headers for an allowed origin, or {} - never a wildcard or a reflection."""
            if not origin:
                return {}
            if normalize_origin(origin) not in allowed_origins(app.cfg):
                return {}
            return {"access-control-allow-origin": origin}

        def _origin_allowed(self, origin: str | None) -> bool:
            return bool(origin) and normalize_origin(origin) in allowed_origins(app.cfg)

        def _read_json(self) -> tuple[dict | None, tuple[int, str] | None]:
            """(payload, error). 400 for a bad/!object body, 413 when it is too large."""
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                return None, (400, "invalid content-length")
            if length < 0:
                return None, (400, "invalid content-length")
            if length > MAX_BODY:
                self.close_connection = True  # the unread body would desync HTTP/1.1
                return None, (413, f"body too large (max {MAX_BODY} bytes)")
            if length == 0:
                return {}, None
            raw = self.rfile.read(length)
            if not raw.strip():
                return {}, None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                return None, (400, "invalid JSON body")
            if not isinstance(payload, dict):
                return None, (400, "JSON body must be an object")
            return payload, None

        def _body_declared(self) -> bool:
            """True when the request announces a body that a refused route will not read.

            The bytes stay in the socket, where a persistent connection would parse the next
            request out of them, so the caller closes the connection instead.
            """
            if (self.headers.get("transfer-encoding") or "").strip():
                return True
            length = (self.headers.get("content-length") or "").strip()
            return bool(length) and length != "0"

        # ----------------------------------------------------------------- verbs
        def do_GET(self):
            self._route("GET")

        def do_HEAD(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_OPTIONS(self):
            self._route("OPTIONS")

        def do_PUT(self):
            self._route("PUT")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        def _method_not_allowed(self, allow: str = "GET, POST, OPTIONS"):
            return self.error_json(405, "method not allowed", headers={"allow": allow})

        def _preflight(self, origin: str | None):
            """CORS preflight: exact origin, GET/POST only, Content-Type/Authorization only."""
            if not origin:
                return self.error_json(403, "origin required")
            if not self._origin_allowed(origin):
                return self.error_json(403, "origin not allowed")
            wanted = (self.headers.get("access-control-request-method") or "GET").strip().upper()
            if wanted not in PREFLIGHT_METHODS:
                self._cors = {}
                return self.error_json(403, f"method {wanted} not allowed")
            asked = [h.strip().lower() for h in (self.headers.get("access-control-request-headers") or "").split(",") if h.strip()]
            bad = [h for h in asked if h not in PREFLIGHT_HEADERS]
            if bad:
                self._cors = {}  # a refused preflight gets no grant, not even ACAO
                return self.error_json(403, "headers not allowed: " + ", ".join(bad))
            self._cors = {"access-control-allow-origin": origin}
            return self._send(
                204,
                b"",
                "text/plain; charset=utf-8",
                headers={
                    "access-control-allow-methods": ", ".join(PREFLIGHT_METHODS),
                    "access-control-allow-headers": "Content-Type, Authorization",
                    "access-control-max-age": "600",
                },
            )

        def _owner_gate(self, path: str, origin: str | None):
            """None to proceed, otherwise the refusal response.

            Order: no configured token => 503 (fail closed) for every owner endpoint;
            a present-but-not-allowed Origin => 403 even with a valid bearer; a missing or
            wrong bearer => 401. A request without an Origin header is allowed only when it
            is authenticated, which the bearer check enforces.
            """
            token, _source = owner_token()
            if not token:
                return self.error_json(503, "owner token not configured: writes are disabled")
            if origin is not None and not self._origin_allowed(origin):
                return self.error_json(403, "origin not allowed")
            if not token_matches(bearer_token(self.headers.get("authorization")), token):
                return self.error_json(401, "unauthorized")
            return None

        def _route(self, method: str):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            origin = self.headers.get("origin")
            self._cors = self._cors_headers(origin)
            try:
                if method == "OPTIONS":
                    return self._preflight(origin)
                if method not in ALLOWED_METHODS:
                    return self._method_not_allowed(
                        "POST, OPTIONS" if path in {"/api/refresh", "/api/queue", "/api/feedback"} else "GET, POST, OPTIONS"
                    )
                if path == "/api/refresh" and method != "POST":
                    return self._method_not_allowed("POST")
                # Method policy for the raw owner reads (lead-authored): /api/failures answers
                # only the effective GET (do_HEAD maps to GET) and OPTIONS. Every other
                # dispatched method is refused HERE - before the owner gate and before any
                # payload access. Dispatch used to key on the path alone, so a POST skipped
                # the gate below and fell into the read branch, handing the raw failure feed
                # to an unauthenticated caller.
                if path == "/api/failures" and method not in ("GET", "OPTIONS"):
                    if self._body_declared():
                        self.close_connection = True  # the unread body would desync HTTP/1.1
                    return self._method_not_allowed("GET, HEAD, OPTIONS")
                if (method, path) in OWNER_ROUTES:
                    denial = self._owner_gate(path, origin)
                    if denial is not None:
                        return denial
                if path in ("/", "/index.html"):
                    return self.serve_file(PUBLIC_DIR / "index.html")
                if path == "/api/state":
                    return self.json_response(200, app.store.api_payload(cfg=app.cfg, uptime_s=app.uptime()))
                if path == "/health":
                    return self.json_response(200, app.store.health_payload(cfg=app.cfg, uptime_s=app.uptime()))
                if path == "/metrics":
                    body = app.store.metrics_text(cfg=app.cfg).encode("utf-8")
                    return self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
                if path == "/api/failures":
                    n = int((query.get("n") or ["20"])[0])
                    return self.json_response(200, {"failures": app.store.recent_failures(n)})
                if path == "/api/queue":
                    if method == "POST":
                        payload, body_error = self._read_json()
                        if body_error is not None:
                            return self.error_json(*body_error)
                        if isinstance(payload.get("queue"), list):
                            ids = app.store.queue_set(payload["queue"])
                        elif payload.get("add") is not None:
                            ids = app.store.queue_add(int(payload["add"]))
                        elif payload.get("remove") is not None:
                            ids = app.store.queue_remove(int(payload["remove"]))
                        else:
                            return self.error_json(400, "expected queue[] / add / remove")
                        app.store.save()
                        # the server's own copy is the answer: the client never sends a
                        # whole-queue replacement and never merges a stale local list
                        return self.json_response(200, {"queue": ids, "counts": app.store.counts()})
                    return self.json_response(200, {"queue": app.store.queue(), "counts": app.store.counts()})
                if path == "/api/feedback":
                    if method == "POST":
                        payload, body_error = self._read_json()
                        if body_error is not None:
                            return self.error_json(*body_error)
                        try:
                            tid = int(payload.get("id"))
                        except (TypeError, ValueError):
                            return self.error_json(400, "id required")
                        vote = (payload.get("vote") or "").strip().lower()
                        note = payload.get("note") or ""
                        if vote == "clear":
                            learn_mod.remove(app.store, tid)
                            topic = app.store.get(tid)
                            return self.json_response(
                                200,
                                {
                                    "ok": True,
                                    "cleared": tid,
                                    "topic": _topic_brief(topic),
                                    "queue": app.store.queue(),
                                    "counts": app.store.counts(),
                                    "votes": learn_mod.counts(app.store),
                                },
                            )
                        try:
                            entry = learn_mod.record(app.store, tid, vote, note)
                        except ValueError as exc:
                            return self.error_json(400, str(exc))
                        except KeyError as exc:
                            return self.error_json(404, str(exc))
                        # The vote is the override only: 收藏 is a separate signal, so the
                        # queue reported here is just the current bookmark list, unchanged
                        # by this request.
                        return self.json_response(
                            200,
                            {
                                "ok": True,
                                "entry": entry,
                                "topic": _topic_brief(app.store.get(tid)),
                                "queue": app.store.queue(),
                                "counts": app.store.counts(),
                                "votes": learn_mod.counts(app.store),
                            },
                        )
                    return self.json_response(
                        200,
                        {
                            "feedback": learn_mod.all_votes(app.store),
                            "counts": learn_mod.counts(app.store),
                        },
                    )
                if path == "/api/refresh" and method == "POST":
                    result = app.trigger_refresh()
                    payload = app.store.api_payload(cfg=app.cfg, uptime_s=app.uptime())
                    result["counts"] = payload["counts"]
                    return self.json_response(202 if result["running"] else 200, result)
                if path.startswith("/fixtures/"):
                    return self.serve_file(FIXTURE_DIR / path[len("/fixtures/") :], allow_any=True)
                if method == "GET" and not path.startswith("/api/"):
                    return self.serve_file(PUBLIC_DIR / path.lstrip("/"), allow_any=True)
                return self.error_json(404, "not found")
            except BrokenPipeError:
                return
            except Exception as exc:  # never let one bad request kill the thread
                app.log(f"http error for {path}: {type(exc).__name__}: {exc}")
                try:
                    return self.error_json(500, f"{type(exc).__name__}: {exc}")
                except Exception:
                    return

        def serve_file(self, path: Path, *, allow_any: bool = False):
            try:
                target = path.resolve()
            except Exception:
                return self.error_json(404, "bad path")
            roots = [PUBLIC_DIR.resolve()] + ([FIXTURE_DIR.resolve()] if allow_any else [])
            if not _within_roots(target, roots):
                return self.error_json(403, "forbidden")
            if not target.is_file():
                return self.error_json(404, f"not found: {path.name}")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
                ctype += "; charset=utf-8"
            body = target.read_bytes()
            self._send(200, body, ctype)

    return Handler


def build_server(cfg: dict, app: App) -> ThreadingHTTPServer:
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), handler)
    httpd.daemon_threads = True
    _token, source = owner_token()
    origins = ", ".join(sorted(allowed_origins(cfg))) or "-"
    # source label only: the token value is never logged
    app.log(f"owner writes {'enabled' if _token else 'DISABLED (503 until a token is configured)'} [{source or 'no token configured'}]")
    app.log(f"cors allowlist: {origins}")
    return httpd
