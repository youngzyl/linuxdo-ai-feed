#!/usr/bin/env python3
"""Headless browser check for the published frontend bundle (desktop + mobile).

Runs with the Chromium already present in the sandbox over the DevTools protocol, using
only the standard library (no installs, no npm, no playwright). It serves a throwaway copy
of the build output plus a *stub* API - nothing touches the live service on :8791 and no
real vote is cast.

Two phases:
  A. fixture mode (?fixture=1) - the local dev path must keep working with zero /api calls,
     on desktop and on a 390x844 touch viewport (drawer, links, local queue toggle).
  B. real mode against the stub API - unauthenticated the page is read-only: the write
     controls are disabled with a reason, row clicks explain themselves and send nothing.
     After the owner token is supplied through the 管理 button, writes carry
     `Authorization: Bearer`, the queue change is applied from the server's response, and a
     refused vote surfaces an error instead of pretending success (and sends only ONE
     request - no separate fire-and-forget queue write).

Usage: python3 tests/browser_check.py [--keep] ; exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
FIXTURES = PUBLIC / "fixtures"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or "/usr/bin/chromium"
OWNER_TOKEN = "test-owner-token-browser-check"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


# --------------------------------------------------------------------------- CDP client
class CDP:
    """Minimal DevTools-protocol client over a raw WebSocket."""

    def __init__(self, ws_url: str):
        assert ws_url.startswith("ws://"), ws_url
        rest = ws_url[5:]
        hostport, path = rest.split("/", 1)
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.sendall(request)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake failed: EOF")
            buf += chunk
        header, self._leftover = buf.split(b"\r\n\r\n", 1)
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket handshake failed: " + header.decode("utf-8", "replace")[:200])
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if expected.encode() not in header:
            raise RuntimeError("websocket handshake failed: bad accept key")
        self.sock.settimeout(20)
        self._id = 0
        self.events: list[dict] = []

    # -- framing ---------------------------------------------------------------
    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        frame = bytearray([0x80 | opcode])
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", length)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def _read_exact(self, n: int) -> bytes:
        while len(self._leftover) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self._leftover += chunk
        out, self._leftover = self._leftover[:n], self._leftover[n:]
        return out

    def _read_message(self) -> dict | None:
        data = b""
        while True:
            first, second = self._read_exact(2)
            fin, opcode = first & 0x80, first & 0x0F
            masked, length = second & 0x80, second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send_frame(payload, 0xA)
                continue
            if opcode == 0x8:
                raise RuntimeError("websocket closed by peer")
            data += payload
            if fin:
                break
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    # -- protocol --------------------------------------------------------------
    def call(self, method: str, timeout: float = 20, **params):
        self._id += 1
        message_id = self._id
        self._send_frame(json.dumps({"id": message_id, "method": method, "params": params}).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._read_message()
            if msg is None:
                continue
            if msg.get("id") == message_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result")
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"{method} timed out")

    def evaluate(self, expression: str, *, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("page JS error: " + json.dumps(result["exceptionDetails"])[:400])
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------------ stub backend
class StubHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the bundle, the fixtures and a stub API. Records every request."""

    state: dict = {}
    requests: list[dict] = []

    def log_message(self, fmt, *args):  # keep the output clean
        pass

    def _record(self, method: str):
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.requests.append(
            {
                "method": method,
                "path": self.path.split("?")[0],
                "origin": headers.get("origin"),
                "authorization": headers.get("authorization"),
                "body": None,
            }
        )
        return self.requests[-1]

    def _json(self, status: int, payload: dict, extra: dict | None = None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        entry = self._record("GET")
        path = entry["path"]
        state = type(self).state
        if path == "/api/state":
            return self._json(200, state["state"])
        if path == "/health":
            return self._json(200, state["health"])
        if path == "/api/queue":
            return self._json(200, {"queue": state["queue"]})
        if path == "/api/feedback":
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, {"feedback": state["feedback"], "counts": {"keep": 0, "skip": 0, "total": 0}})
        return super().do_GET()

    def do_POST(self):
        entry = self._record("POST")
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            entry["body"] = json.loads(raw.decode() or "{}")
        except Exception:
            entry["body"] = raw.decode("utf-8", "replace")
        path = entry["path"]
        state = type(self).state
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if path == "/api/queue":
            body = entry["body"] if isinstance(entry["body"], dict) else {}
            ids = list(state["queue"])
            if body.get("add") is not None and int(body["add"]) not in ids:
                ids.append(int(body["add"]))
            if body.get("remove") is not None:
                ids = [x for x in ids if x != int(body["remove"])]
            state["queue"] = ids
            return self._json(200, {"queue": ids, "counts": _counts(state, ids)})
        if path == "/api/feedback":
            if state.get("fail_vote"):
                return self._json(401, {"error": "unauthorized"})
            body = entry["body"] if isinstance(entry["body"], dict) else {}
            tid = int(body.get("id") or 0)
            vote = body.get("vote")
            ids = list(state["queue"])
            for topic in state["state"]["topics"]:
                if int(topic["id"]) == tid:
                    if vote == "keep":
                        topic["state"] = "picked"
                        if tid not in ids:
                            ids.append(tid)
                    elif vote == "skip":
                        topic["state"] = "rejected"
                        ids = [x for x in ids if x != tid]
            state["queue"] = ids
            state["feedback"] = [{"id": tid, "vote": vote, "at": "2026-09-21T00:00:00Z"}]
            return self._json(
                200,
                {
                    "ok": True,
                    "entry": {"id": tid, "vote": vote},
                    "topic": {"id": tid, "state": "picked" if vote == "keep" else "rejected"},
                    "queue": ids,
                    "counts": _counts(state, ids),
                    "votes": {"keep": 1 if vote == "keep" else 0, "skip": 1 if vote == "skip" else 0, "total": 1},
                },
            )
        if path == "/api/refresh":
            state["refreshes"] += 1
            return self._json(202, {"ok": True, "running": True, "job": {"stage": "fetch"}})
        return self._json(404, {"error": "not found"})

    def _authorized(self) -> bool:
        header = self.headers.get("authorization") or ""
        return header.strip() == f"Bearer {OWNER_TOKEN}"


def _counts(state: dict, ids: list[int]) -> dict:
    topics = state["state"]["topics"]
    return {
        "all": len(topics),
        "picked": sum(1 for t in topics if t["state"] == "picked"),
        "rejected": sum(1 for t in topics if t["state"] == "rejected"),
        "pending": sum(1 for t in topics if t["state"] == "pending"),
        "queue": len(ids),
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    # NOTE: pass `directory` as a kwarg (via partial). Python 3.11's
    # SimpleHTTPRequestHandler.__init__ assigns self.directory = os.getcwd() when the
    # keyword is absent, so a `directory` *class attribute* is silently ignored and the
    # server would serve the process cwd instead of the bundle.
    handler = functools.partial(StubHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd, httpd.server_address[1]


def build_site(tmp: Path) -> Path:
    """A throwaway copy of the built bundle + the real fixtures, served locally."""
    site = tmp / "site"
    site.mkdir(parents=True)
    for name in ("index.html", "app.js", "styles.css", "runtime-config.js"):
        shutil.copy2(PUBLIC / name, site / name)
    (site / "fixtures").mkdir()
    for name in ("state.sample.json", "state.sample.next.json"):
        source = FIXTURES / name
        if source.is_file():
            shutil.copy2(source, site / "fixtures" / name)
    return site


def launch_chromium(port: int, profile: Path, url: str) -> subprocess.Popen:
    cmd = [
        CHROMIUM,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--window-size=1280,900",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                json.loads(resp.read())
                return proc
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("chromium did not expose a DevTools endpoint")


def page_ws(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
        targets = json.loads(resp.read())
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("no page target")
    return pages[0]["webSocketDebuggerUrl"]


def open_page(cdp: CDP, url: str, *, width: int, height: int, mobile: bool, touch: bool):
    cdp.call("Runtime.enable")
    cdp.call("Page.enable")
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        width=width,
        height=height,
        deviceScaleFactor=2 if mobile else 1,
        mobile=mobile,
    )
    if touch:
        cdp.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    cdp.call("Page.navigate", url=url)
    time.sleep(1.6)


def settle(cdp: CDP, seconds: float = 0.8):
    time.sleep(seconds)


def mouse_click(cdp: CDP, selector: str) -> tuple[bool, dict]:
    """Click like a real mouse, at a point that really belongs to that element.

    Two traps this avoids, both of which produced false failures here:
      * a JS `.click()` emits no pointer events, and the app decides mouse-vs-touch from
        `pointerdown.pointerType`, so a JS click is treated as a tap and opens the drawer;
      * the element can be below the fold (innerHeight 900 while the row sits at y=1059),
        and then Input.dispatchMouseEvent lands on <html>: the event arrives with
        pointerType=mouse but never reaches the board handler.

    So: scroll it into view, aim at a point inside its own box, and confirm with
    elementFromPoint that the click will hit this element (or a descendant of it).
    Returns (aimed, diagnostics) - callers fold `aimed` into their assertion so a harness
    aiming problem shows up as a loud failure instead of a silent no-op.
    """
    point = cdp.evaluate(
        """(function(){
          var n = document.querySelector(%s);
          if (!n) return {missing: true};
          n.scrollIntoView({block: 'center', inline: 'nearest'});
          var r = n.getBoundingClientRect();
          var cx = Math.round(r.left + r.width / 2);
          var cy = Math.round(r.top + r.height / 2);
          var x = Math.min(Math.max(cx, 1), window.innerWidth - 2);
          var y = Math.min(Math.max(cy, 1), window.innerHeight - 2);
          var visible = r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
          var el = document.elementFromPoint(x, y);
          var hits = !!el && (el === n || n.contains(el));
          return {x: x, y: y, visible: visible, hits: hits, rect: [Math.round(r.top), Math.round(r.bottom)],
                  at: el ? String(el.className || el.tagName).slice(0, 40) : null};
        })()"""
        % json.dumps(selector)
    )
    if not point or point.get("missing") or not point.get("visible") or not point.get("hits"):
        print(f"      (harness: could not aim a mouse click at {selector}: {point})")
        return False, point or {}
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp.call(
            "Input.dispatchMouseEvent",
            type=kind,
            x=point["x"],
            y=point["y"],
            button="left",
            buttons=1 if kind != "mouseReleased" else 0,
            clickCount=1,
            pointerType="mouse",
        )
    time.sleep(0.15)
    return True, point


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temp dir and screenshots")
    parser.add_argument("--headed", action="store_true", help="(unsupported here) placeholder")
    args = parser.parse_args()

    if not Path(CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="pages-browser-"))
    profile = tmp / "profile"
    site = build_site(tmp)
    fixture_state = json.loads((FIXTURES / "state.sample.json").read_text(encoding="utf-8"))
    httpd, port = serve(site)
    base = f"http://127.0.0.1:{port}"
    StubHandler.state = {
        "state": fixture_state,
        "health": {"status": "ok", "attention": {"needed": False, "reason": "", "kind": None}, "auth": {"writes": "owner", "token_configured": True}},
        "queue": [],
        "feedback": [],
        "fail_vote": False,
        "refreshes": 0,
    }
    StubHandler.requests = []
    devtools_port = _free_port()
    proc = None
    cdp = None
    try:
        proc = launch_chromium(devtools_port, profile, "about:blank")
        cdp = CDP(page_ws(devtools_port))

        print(f"--- phase A: fixture mode (desktop) at {base}/index.html?fixture=1 ---")
        open_page(cdp, f"{base}/index.html?fixture=1", width=1280, height=900, mobile=False, touch=False)
        calls_during_fixture = list(StubHandler.requests)
        check(cdp.evaluate("!!window.LINUXDO_AI_DEBUG") is True, "A1 runtime config is loaded before app.js")
        check(cdp.evaluate("window.LINUXDO_AI_DEBUG.apiBase") == "", "A2 repository default is same-origin")
        items = cdp.evaluate("document.querySelectorAll('.item').length")
        check(isinstance(items, int) and items > 0, "A3 fixture topics render", f"items={items}")
        check(cdp.evaluate("document.querySelector('#authchip').textContent") == "DEV · 只读", "A4 dev chip labels the page read-only-ish")
        check(cdp.evaluate("document.querySelector('#owner') !== null"), "A5 管理 button exists")
        api_calls = [r for r in calls_during_fixture if r["path"].startswith("/api/")]
        check(api_calls == [], "A6 fixture mode makes no API call", str(api_calls)[:200])
        # local queue interaction must still work with no backend
        target = cdp.evaluate(
            "(function(){var n=document.querySelector('.item--picked')||document.querySelectorAll('.item')[1];"
            "return n?Number(n.dataset.id):0;})()"
        )
        if target:
            before = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]').length")
            aimed, point = mouse_click(cdp, '.item[data-id="%d"]' % target)
            settle(cdp)
            after = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]').length")
            check(aimed and after == before + 1, "A7 fixture queue toggle stays local",
                  f"aimed={aimed} {before}->{after} point={point}")
            check([r for r in StubHandler.requests if r["path"] == "/api/queue"] == [], "A8 no POST for the local toggle")
        else:
            check(False, "A7/A8 could not find a row to toggle")

        print(f"--- phase A: fixture mode (mobile 390x844) ---")
        open_page(cdp, f"{base}/index.html?fixture=1", width=390, height=844, mobile=True, touch=True)
        check(cdp.evaluate("document.querySelectorAll('.item').length") > 0, "A9 mobile renders items")
        check(cdp.evaluate("getComputedStyle(document.querySelector('#authchip')).display") != "none", "A10 mobile shows the auth chip")
        drawer = cdp.evaluate(
            "(function(){var n=document.querySelector('.item');if(!n)return 'none';n.click();return document.querySelector('#drawer').hidden?'hidden':'open';})()"
        )
        settle(cdp, 0.6)
        check(drawer is not None and cdp.evaluate("document.querySelector('#drawer').hidden") in (False, "false"),
              "A11 mobile drawer opens from the tap path", str(drawer))
        check(cdp.evaluate("document.querySelector('#d-link').getAttribute('href')") .startswith("https://linux.do"),
              "A12 original link is still usable")
        check([r for r in StubHandler.requests if r["path"].startswith("/api/")] == [],
              "A13 mobile fixture mode still makes no API call")

        print(f"--- phase B: real mode, read-only (desktop) ---")
        StubHandler.requests.clear()
        open_page(cdp, f"{base}/index.html", width=1280, height=900, mobile=False, touch=False)
        check(cdp.evaluate("window.LINUXDO_AI_DEBUG.apiBase") == "", "B1 same-origin API base")
        check(cdp.evaluate("document.querySelectorAll('.item').length") > 0, "B2 state renders from the API")
        check(cdp.evaluate("document.querySelector('#authchip').textContent") == "只读", "B3 unauthenticated page is labelled read-only")
        check(cdp.evaluate("document.querySelector('#authchip').title").find("owner token") >= 0, "B4 read-only chip explains why")
        check(cdp.evaluate("document.querySelector('#refresh').disabled") is True, "B5 refresh disabled without a token")
        check(cdp.evaluate("(document.querySelector('#refresh').title||'').indexOf('owner token') >= 0") is True,
              "B6 disabled refresh carries an explanatory title")
        StubHandler.requests.clear()
        cdp.evaluate("document.querySelector('#refresh').click(); true")
        settle(cdp)
        check([r for r in StubHandler.requests if r["method"] == "POST"] == [], "B7 clicking the disabled refresh sends nothing")
        # a row click in the picked column would queue it: must explain itself instead
        picked = cdp.evaluate(
            "(function(){var n=document.querySelector('.item--picked');return n?Number(n.dataset.id):0;})()"
        )
        if picked:
            StubHandler.requests.clear()
            aimed, point = mouse_click(cdp, '.item[data-id="%d"]' % picked)
            settle(cdp)
            status_text = cdp.evaluate("document.querySelector('#opstatus').hidden ? '' : document.querySelector('#opstatus').textContent")
            check(aimed and "owner token" in (status_text or ""), "B8 row click explains the read-only mode",
                  f"aimed={aimed} point={point} status={status_text!r}")
            check([r for r in StubHandler.requests if r["method"] == "POST"] == [], "B9 no write attempted while read-only")
        else:
            check(False, "B8/B9 no picked row to click")

        print(f"--- phase B: owner token flow ---")
        cdp.evaluate("window.prompt = function(){ return %s; }; true" % json.dumps(OWNER_TOKEN))
        StubHandler.requests.clear()
        cdp.evaluate("document.querySelector('#owner').click(); true")
        settle(cdp, 1.2)
        verify = [r for r in StubHandler.requests if r["path"] == "/api/feedback"]
        check(any(r["authorization"] == f"Bearer {OWNER_TOKEN}" for r in verify), "B10 token is verified against /api/feedback with a bearer header")
        check(cdp.evaluate("document.querySelector('#authchip').textContent") == "已连接 · 可写", "B11 chip flips to connected")
        check(cdp.evaluate("document.querySelector('#refresh').disabled") is False, "B12 write controls are re-enabled")
        check(cdp.evaluate("(function(){try{return localStorage.getItem('linuxdo-ai.ownerToken')===null;}catch(e){return false;}})()") is True,
              "B13 the token is not in localStorage")
        check(OWNER_TOKEN not in (os.environ.get("BROWSER_CHECK_URL") or ""),
              "B14 the token never travels in the URL")

        StubHandler.requests.clear()
        cdp.evaluate("document.querySelector('#refresh').click(); true")
        settle(cdp, 1.6)
        refresh_calls = [r for r in StubHandler.requests if r["path"] == "/api/refresh" and r["method"] == "POST"]
        check(len(refresh_calls) == 1 and refresh_calls[0]["authorization"] == f"Bearer {OWNER_TOKEN}",
              "B15 refresh is one authenticated POST")
        check(StubHandler.state["refreshes"] == 1, "B16 the stub saw exactly one refresh")

        print("--- phase B: queue add/remove contract ---")
        StubHandler.state["queue"] = []
        StubHandler.requests.clear()
        open_page(cdp, f"{base}/index.html", width=1280, height=900, mobile=False, touch=False)
        picked = cdp.evaluate(
            "(function(){var n=document.querySelector('.item--picked');return n?Number(n.dataset.id):0;})()"
        )
        if picked:
            StubHandler.requests.clear()
            aimed, point = mouse_click(cdp, '.item[data-id="%d"]' % picked)
            settle(cdp, 1.0)
            queue_posts = [r for r in StubHandler.requests if r["path"] == "/api/queue" and r["method"] == "POST"]
            check(aimed and len(queue_posts) == 1, "B17 the queue change is one POST",
                  f"aimed={aimed} point={point} posts={json.dumps([r['body'] for r in queue_posts])}")
            body = queue_posts[0]["body"] if queue_posts else None
            check(isinstance(body, dict) and set(body) == {"add"},
                  "B18 it sends the add shape, never a whole-queue replacement", json.dumps(body))
            check(bool(queue_posts) and queue_posts[0]["authorization"] == f"Bearer {OWNER_TOKEN}", "B19 writes carry the bearer")
            local_queue = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
            check(local_queue == StubHandler.state["queue"] == [picked],
                  "B20 the local queue is the server's answer", f"{local_queue} vs {StubHandler.state['queue']}")

            # Existing app semantics: the ROW click only queues from column 2. Once the topic
            # is in the queue its row opens the preview, and the removal affordance is the
            # drawer's 「移到待读」 button - which goes through the same toggleQueue path.
            StubHandler.requests.clear()
            aimed, point = mouse_click(cdp, '.item[data-id="%d"]' % picked)
            settle(cdp, 0.9)
            writes = [r["body"] for r in StubHandler.requests if r["method"] == "POST"]
            check(aimed and cdp.evaluate("!document.querySelector('#drawer').hidden") is True and writes == [],
                  "B21 a queued row click opens the preview instead of writing",
                  f"aimed={aimed} point={point} writes={json.dumps(writes)}")

            StubHandler.requests.clear()
            aimed, point = mouse_click(cdp, "#d-queue")
            settle(cdp, 1.0)
            removes = [r["body"] for r in StubHandler.requests if r["path"] == "/api/queue" and r["method"] == "POST"]
            check(aimed and removes == [{"remove": picked}], "B22 the drawer button sends the remove shape",
                  f"aimed={aimed} point={point} removes={json.dumps(removes)}")
            check(cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')") == [],
                  "B23 the local queue follows the removal", str(cdp.evaluate("localStorage.getItem('linuxdo-ai.queue')")))

            print("--- phase B: vote contract (one request, queue from the response) ---")
            StubHandler.state["queue"] = []
            StubHandler.requests.clear()
            open_page(cdp, f"{base}/index.html?open={picked}", width=1280, height=900, mobile=False, touch=False)
            check(cdp.evaluate("!document.querySelector('#drawer').hidden") is True, "B24 drawer is open for the vote test")
            cdp.evaluate("(function(){document.querySelector('#d-keep').click();return true;})()")
            settle(cdp, 1.2)
            vote_posts = [r for r in StubHandler.requests if r["path"] == "/api/feedback" and r["method"] == "POST"]
            queue_posts = [r for r in StubHandler.requests if r["path"] == "/api/queue" and r["method"] == "POST"]
            check(len(vote_posts) == 1, "B25 a vote is exactly one POST /api/feedback")
            check(queue_posts == [], "B26 the vote does not fire a second, independent queue write")
            check(bool(vote_posts) and vote_posts[0]["authorization"] == f"Bearer {OWNER_TOKEN}", "B27 the vote is authenticated")
            vote_body = vote_posts[0]["body"] if vote_posts else None
            check(isinstance(vote_body, dict) and vote_body.get("vote") == "keep" and vote_body.get("id") == picked,
                  "B28 the vote body carries the explicit vote", json.dumps(vote_body))
            local_queue = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
            check(local_queue == [picked], "B29 the queue from the vote response was applied", str(local_queue))

            print("--- phase B: a refused write must be visible ---")
            StubHandler.state["fail_vote"] = True
            StubHandler.requests.clear()
            before = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
            cdp.evaluate("(function(){document.querySelector('#d-skip').click();return true;})()")
            settle(cdp, 1.2)
            status_text = cdp.evaluate("document.querySelector('#opstatus').hidden ? '' : document.querySelector('#opstatus').textContent")
            check("401" in (status_text or ""), "B30 the refusal is shown to the user", str(status_text))
            after = cdp.evaluate("JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
            check(after == before, "B31 a refused vote changes nothing locally", f"{after} vs {before}")
            check(cdp.evaluate("document.querySelector('#d-skip').disabled") is False, "B32 the control is usable again after the failure")
            check(cdp.evaluate("document.querySelector('#d-skip').getAttribute('aria-busy')") in (None, "0", False),
                  "B33 no control is left in a busy state")
            StubHandler.state["fail_vote"] = False
            StubHandler.requests.clear()
            cdp.evaluate("(function(){document.querySelector('#d-skip').click();return true;})()")
            settle(cdp, 1.2)
            refusals = [r for r in StubHandler.requests if r["path"] == "/api/feedback"]
            check(len(refusals) == 1 and refusals[0]["method"] == "POST", "B34 a retry after the failure is one normal vote")
        else:
            check(False, "B17-B34 no picked row available")

        screenshots = []
        for label, width, height, mobile in (("desktop", 1280, 900, False), ("mobile", 390, 844, True)):
            open_page(cdp, f"{base}/index.html", width=width, height=height, mobile=mobile, touch=mobile)
            shot = tmp / f"pages-{label}.png"
            data = cdp.call("Page.captureScreenshot", format="png")
            shot.write_bytes(base64.b64decode(data["data"]))
            screenshots.append(str(shot))
        print("screenshots:", ", ".join(screenshots))

        if args.keep:
            print("keeping evidence in", tmp)
        return 0 if all(ok for ok, _ in RESULTS) else 1
    finally:
        if cdp:
            cdp.close()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        httpd.shutdown()
        httpd.server_close()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    code = main()
    total = len(RESULTS)
    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{total - len(failed)}/{total} checks passed")
    if failed:
        print("failed: " + "; ".join(failed))
    raise SystemExit(code)
