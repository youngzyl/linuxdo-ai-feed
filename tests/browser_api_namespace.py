#!/usr/bin/env python3
"""Isolated Chromium contract: read key cutover never changes network destination."""
import json
import shutil
import tempfile
import time
from pathlib import Path

import browser_check as bc

ROOT = Path(__file__).resolve().parents[1]
OLD = "https://tcstw.youngzyl.me:8443/linuxdo-api"
EVIL = "https://evil.example/api"


def require(ok, label):
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    if not ok:
        raise AssertionError(label)


def main():
    with tempfile.TemporaryDirectory(prefix="api-namespace-", dir="/dev/shm") as temp:
        root = Path(temp)
        site = root / "site"
        site.mkdir()
        for name in ("index.html", "app.js", "styles.css"):
            shutil.copy2(ROOT / "public" / name, site / name)
        bc.StubHandler.state = {
            "state": json.loads((ROOT / "public/fixtures/state.sample.json").read_text()),
            "health": {"status": "ok", "attention": {"needed": False, "reason": "", "kind": None}, "auth": {"writes": "owner", "token_configured": True}},
            "queue": [], "feedback": [], "fail_vote": False, "refreshes": 0,
            "state_status": 200, "state_delay": 0.0,
        }
        bc.StubHandler.requests = []
        server, port = bc.serve(site)
        base = f"http://127.0.0.1:{port}"
        profile = root / "profile"
        proc = None
        cdp = None
        try:
            devtools_port = bc._free_port()
            proc = bc.launch_chromium(devtools_port, profile, "about:blank")
            cdp = bc.CDP(bc.page_ws(devtools_port))
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            def evaluate(expr):
                return cdp.evaluate(expr)
            def config(value):
                (site / "runtime-config.js").write_text("window.LINUXDO_AI_RUNTIME = " + json.dumps(value) + ";\n")
            def open_page(query=""):
                cdp.call("Page.navigate", url=base + "/index.html" + query)
                time.sleep(1.6)
            def reload():
                cdp.call("Page.reload")
                time.sleep(1.6)
            key = "linuxdo-ai.read:" + OLD
            config({"apiBase": base, "readStateNamespace": OLD})
            open_page()
            topic = evaluate("Number(document.querySelector('.item').dataset.id)")
            require(bool(topic), "stub state loaded")
            evaluate("localStorage.clear(); localStorage.setItem(%s, JSON.stringify([%d])); localStorage.setItem('readStateNamespace', %s);" % (json.dumps(key), topic, json.dumps(EVIL)))
            bc.StubHandler.requests.clear()
            open_page("?apiBase=" + EVIL + "&readStateNamespace=" + EVIL)
            require(bool(evaluate("document.querySelector('.item[data-id=\"%d\"]').classList.contains('is-read')" % topic)), "old namespace read state retained")
            api_reads = [r for r in bc.StubHandler.requests if r["path"].startswith(("/api/", "/health"))]
            require(all(r["method"] == "GET" and r["path"] in ("/api/state", "/api/queue", "/health") for r in api_reads) and bool(api_reads), f"all API requests use configured new API, never old namespace or query: {[(r['method'], r['path']) for r in api_reads]}")
            evaluate("document.querySelector('.item[data-id=\"%d\"]').click()" % topic)
            time.sleep(.4)
            require(evaluate("document.querySelector('#d-read').getAttribute('aria-pressed')") == "true", "read control reflects old stored value")
            evaluate("document.querySelector('#d-read').click()")
            require(evaluate("JSON.parse(localStorage.getItem(%s)).includes(%d)" % (json.dumps(key), topic)) is False, "manual unread writes old namespace")
            reload()
            require(evaluate("document.querySelector('.item[data-id=\"%d\"]').classList.contains('is-read')" % topic) is False, "manual unread retained after reload")
            require(evaluate("localStorage.getItem(%s)" % json.dumps("linuxdo-ai.read:" + base)) is None, "no namespace merge or new-key write")
            config({"apiBase": base, "readStateNamespace": "javascript:alert(1)"})
            evaluate("localStorage.setItem(%s, JSON.stringify([%d]))" % (json.dumps("linuxdo-ai.read:" + base), topic))
            bc.StubHandler.requests.clear()
            open_page("?readStateNamespace=" + EVIL)
            require(evaluate("document.querySelector('.item[data-id=\"%d\"]').classList.contains('is-read')" % topic) is True, "invalid namespace falls back to API base")
            require(bool(bc.StubHandler.requests) and all(r["method"] == "GET" for r in bc.StubHandler.requests), "invalid namespace does not redirect requests")
            config({"apiBase": ""})
            evaluate("localStorage.setItem(%s, JSON.stringify([%d]))" % (json.dumps("linuxdo-ai.read:" + base), topic))
            open_page("?apiBase=" + EVIL)
            require(evaluate("document.querySelector('.item[data-id=\"%d\"]').classList.contains('is-read')" % topic) is True, "default deployment isolates by page origin")
            require(evaluate("window.LINUXDO_AI_DEBUG.apiBase") == "", "default API remains same-origin")
            print("RESULT 11/11 isolated browser checks passed")
        finally:
            if cdp:
                cdp.close()
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            server.shutdown()


if __name__ == "__main__":
    main()
