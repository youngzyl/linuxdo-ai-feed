"""HTTP server: static frontend + JSON API + monitoring endpoints.

Endpoints
  GET  /                     static (public/)
  GET  /api/state            full payload for the page (CONTRACT.md §1)
  POST /api/refresh          kick a cycle in the background
  GET  /api/queue            queue ids
  POST /api/queue            replace queue  ({"queue":[id,...]} or {"add":id} / {"remove":id})
  GET  /health               status + attention flag  (watchdog reads this)
  GET  /metrics              Prometheus text
  GET  /api/failures?n=20    RCA feed (newest first)
  GET  /fixtures/<file>      dev fixtures (frontend ?fixture=1)
"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from config import FIXTURE_DIR, PUBLIC_DIR
from store import now_iso
import learn as learn_mod

MAX_BODY = 64 * 1024


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

        def _send(self, status: int, body: bytes, content_type: str, *, headers: dict | None = None):
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json_response(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def error_json(self, status: int, message: str) -> None:
            self.json_response(status, {"error": message, "status": status})

        def read_json_body(self):
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                return {}
            if length <= 0 or length > MAX_BODY:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        # ----------------------------------------------------------------- verbs
        def do_GET(self):
            self._route("GET")

        def do_HEAD(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method: str):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            try:
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
                        payload = self.read_json_body()
                        if isinstance(payload.get("queue"), list):
                            ids = app.store.queue_set(payload["queue"])
                        elif payload.get("add") is not None:
                            ids = app.store.queue_add(int(payload["add"]))
                        elif payload.get("remove") is not None:
                            ids = app.store.queue_remove(int(payload["remove"]))
                        else:
                            return self.error_json(400, "expected queue[] / add / remove")
                        app.store.save()
                        return self.json_response(200, {"queue": ids})
                    return self.json_response(200, {"queue": app.store.queue()})
                if path == "/api/feedback":
                    if method == "POST":
                        payload = self.read_json_body()
                        try:
                            tid = int(payload.get("id"))
                        except (TypeError, ValueError):
                            return self.error_json(400, "id required")
                        vote = (payload.get("vote") or "").strip().lower()
                        note = payload.get("note") or ""
                        if vote == "clear":
                            learn_mod.remove(app.store, tid)
                            return self.json_response(200, {"ok": True, "cleared": tid, "counts": learn_mod.counts(app.store)})
                        try:
                            entry = learn_mod.record(app.store, tid, vote, note)
                        except ValueError as exc:
                            return self.error_json(400, str(exc))
                        except KeyError as exc:
                            return self.error_json(404, str(exc))
                        return self.json_response(200, {"ok": True, "entry": entry, "counts": learn_mod.counts(app.store)})
                    return self.json_response(200, {"feedback": learn_mod.all_votes(app.store), "counts": learn_mod.counts(app.store)})
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
            if not any(str(target).startswith(str(root)) for root in roots):
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
    return httpd
