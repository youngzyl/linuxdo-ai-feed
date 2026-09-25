#!/usr/bin/env python3
"""U07 — the density toggle must keep the reader's article, lane and viewport offset.

The reported UX bug: in compact mode the reader scrolls to a topic, explicitly opens and
bookmarks it, closes the preview, goes back up to the masthead and clicks 紧凑/间隙 — and the
board throws them back to the top of the page because the toggle re-renders the whole board
(``render()`` replaces the DOM) while the document scroll stays where it was.

What is asserted is OBSERVABLE geometry, never a test-only probe: the same ``data-id`` in the
same lane (``cell--cN``, which is the cell in gap mode and the cell inside the stack in compact
mode) must sit at the remembered viewport offset again after the reflow settles. "The page
scrolled somewhere" is not accepted as a pass.

The dataset is deterministic and lives only in the stdlib stub of ``browser_check.py``: a long
list with asymmetric lane membership (全部 / 精选 / 收藏), a topic that is both picked and
bookmarked, i.e. duplicated across two lanes, and (for the sparse-lane checks) a 收藏 lane whose
rows stand far apart. Nothing here touches the live service or linux.do: no production read, no
vote, no bookmark, no provider call.

Consumption of the density control is observed on the control itself — ``aria-pressed``,
cross-checked against the persisted preference — because ``render()`` only ever puts the
``is-compact`` class on the desktop board; that class cannot witness anything on mobile. Real
input everywhere: ``Input.dispatchMouseEvent``, ``dispatchKeyEvent``, ``dispatchTouchEvent``.

Sections
  A. the exact reported flow, both toggle directions (compact -> gap -> compact)
  B. lanes: picked origin, bookmarks origin, the duplicated id, keyboard activation
  C. browsing semantics: plain scroll, a stale explicit article followed by deeper scrolling,
     a ~2px sliver at the viewport edge, a whole-viewport gap in a sparse lane, repeated rapid
     toggles, and a genuine scroll that arrives right after the toggle
  D. safety: empty data, the anchor pair disappearing (from its own lane, and with that lane
     having nothing on screen), the bottom clamp, mobile no-op, no new browser storage, and
     "the toggle is inert": no read change, no POST, no body GET
  E. a density reflow must not synthesize a passive hover (the row coming to a stationary
     cursor), while a genuine move onto a row resumes the normal hover

Usage: TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1 python3 tests/browser_density_position.py
Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import browser_check as bc  # noqa: E402  (CDP client + stdlib stub API)

OWNER_TOKEN = bc.OWNER_TOKEN
TOL = 24            # documented pixel tolerance for "the same offset"
WANT_TOP = 220      # where the reader parks the article before opening it
SAMPLE_TOP = 3      # at the very viewport top, so the passive sample deterministically lands here
HOVER_MS = 250      # CFG.hoverMs in public/app.js
SELF_SCROLL_MS = 500  # ANCHOR_SELF_MS: the window in which our own compensation scroll is ignored

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""))
    return bool(ok)


# --------------------------------------------------------------------------- dataset


def build_dataset(n: int = 48, head_picked: int = 0) -> dict:
    """Deterministic long list: asymmetric lanes + one topic in two lanes at once.

    ``head_picked`` forces the first N topics into 精选, which pushes the first 全部 (lane 1) row
    below row N — the sparse-lane checks need a lane whose first on-screen row is not row 1.
    """
    topics = []
    for i in range(n):
        picked = (i % 4 == 2) or i < head_picked
        topic = {
            "id": 9200000 + i,
            "title": f"U07 密度回归条目 {i:02d}",
            "url": f"https://linux.do/t/topic/{9200000 + i}",
            "author": f"reader{i % 7}",
            "created_at": "2026-09-25T0%d:00:00.000Z" % (i % 9),
            "bumped_at": "2026-09-25T0%d:00:00.000Z" % (i % 9),
            "reply_count": i % 13,
            "views": 100 + i,
            "like_count": i % 5,
            "category": "Develop",
            "tags": ["回归", "密度"],
            "excerpt": f"确定性摘要 {i}：用于密度切换的位置保持回归，长度固定，便于几何断言。",
            "detail_fetched": False,
            "state": "picked" if picked else ("rejected" if i % 7 == 3 else "pending"),
            "filter": None,
        }
        topics.append(topic)
    # one topic is BOTH picked and bookmarked -> the same id sits in lane 2 and lane 3
    dup = 10
    queue = [9200000 + i for i in range(n) if i % 6 == 5 or i == dup]
    return {"generated_at": "2026-09-26T00:00:00Z", "source": "u07-stub", "filter": None,
            "topics": topics}, queue


DUP_INDEX = 10


def state_payload(topics: dict, queue: list) -> dict:
    return {
        "state": topics,
        "health": {"status": "ok", "attention": {"needed": False, "reason": "", "kind": None},
                   "auth": {"writes": "owner", "token_configured": True}},
        "queue": list(queue),
        "feedback": [],
        "fail_vote": False,
        "refreshes": 0,
        "state_status": 200,
        "state_delay": 0.0,
    }


def lane_ids(state: dict, col: int) -> list[int]:
    """The ids a lane really renders, straight from the payload (1 全部 / 2 精选 / 3 收藏)."""
    topics = state["state"]["topics"]
    queue = set(state["queue"])
    if col == 1:
        return [t["id"] for t in topics if t["state"] != "picked"]
    if col == 2:
        return [t["id"] for t in topics if t["state"] == "picked"]
    return [t["id"] for t in topics if t["id"] in queue]


# --------------------------------------------------------------------------- JS helpers


def js(cdp, expression):
    return cdp.evaluate(expression)


def wait_js(cdp, expression, timeout=5.0, interval=0.1):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return value


HIDE_POINTER_EVENT = """(function(){
  /* A legacy engine: no PointerEvent input typing at all. The app must then keep the reflow's
     passive hover blocked and resume it only on the next explicit activation. */
  try { delete window.PointerEvent; } catch (e) {}
  if ('PointerEvent' in window) {
    try { Object.defineProperty(window, 'PointerEvent',
        { value: undefined, writable: true, configurable: true }); } catch (e) {}
  }
  return typeof window.PointerEvent;
})()"""


def open_page(cdp, url, *, width=1280, height=900, mobile=False, touch=False, settle_s=1.8):
    cdp.call("Runtime.enable")
    cdp.call("Page.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=2 if mobile else 1, mobile=mobile)
    cdp.call("Emulation.setTouchEmulationEnabled", enabled=bool(touch), maxTouchPoints=5)
    cdp.call("Page.navigate", url=url)
    time.sleep(settle_s)


GEOM = """(function(){
  var id = %d;
  var nodes = document.querySelectorAll('.item[data-id="' + id + '"]');
  var out = [];
  for (var i = 0; i < nodes.length; i++) {
    var cell = nodes[i].closest('.cell--c1, .cell--c2, .cell--c3');
    var m = cell ? /cell--c(\\d)/.exec(cell.className) : null;
    var r = nodes[i].getBoundingClientRect();
    out.push({col: m ? Number(m[1]) : 0, top: Math.round(r.top * 100) / 100,
              bottom: Math.round(r.bottom * 100) / 100});
  }
  return out;
})()"""


def geom(cdp, tid: int) -> list[dict]:
    return js(cdp, GEOM % tid) or []


def lane_top(cdp, tid: int, col: int):
    """The viewport top of (id, col) — the pair, never just the id."""
    for row in geom(cdp, tid):
        if row["col"] == col:
            return row
    return None


def placed_geometry(cdp, tid: int, want_top: float) -> dict:
    """Scroll the document so the first instance of `tid` sits at `want_top`."""
    return js(
        cdp,
        """(function(){
             var n = document.querySelector('.item[data-id="%d"]');
             if (!n) return {missing: true};
             var r = n.getBoundingClientRect();
             window.scrollTo(0, Math.max(0, r.top + window.scrollY - %f));
             var after = n.getBoundingClientRect();
             var cell = n.closest('.cell--c1, .cell--c2, .cell--c3');
             var m = cell ? /cell--c(\\d)/.exec(cell.className) : null;
             return {top: Math.round(after.top * 100) / 100, col: m ? Number(m[1]) : 0,
                     scrollY: Math.round(window.scrollY)};
           })()""" % (tid, want_top),
    )


# The hit test every real-input helper shares: aim at a point inside the element's own box and
# confirm with elementFromPoint that the event will reach it. A helper that cannot hit its target
# reports it instead of clicking something else. The element comes in as a JS expression, so the
# same body serves a selector and one specific (id, lane) instance.
HIT_BODY = """(function(){
  var n = %s;
  if (!n) return {missing: true};
  var r = n.getBoundingClientRect();
  var x = Math.round(r.left + r.width / 2);
  var y = Math.round(r.top + r.height / 2);
  var visible = r.bottom > 1 && r.top < window.innerHeight - 1 && r.width > 0;
  var el = document.elementFromPoint(x, y);
  var hits = !!el && (el === n || n.contains(el));
  return {x: x, y: y, visible: visible, hits: hits,
          at: el ? String(el.className || el.tagName).slice(0, 40) : null,
          rect: [Math.round(r.top), Math.round(r.bottom)]};
})()"""


def hit_js_selector(selector: str) -> str:
    return HIT_BODY % ("document.querySelector(" + json.dumps(selector) + ")")


def hit_js_node(node_expr: str) -> str:
    return HIT_BODY % node_expr


def _click_at(cdp, point: dict, pause: float = 0.2) -> tuple[bool, dict]:
    if not point or point.get("missing") or not point.get("visible") or not point.get("hits"):
        return False, point or {}
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", type=kind, x=point["x"], y=point["y"], button="left",
                 buttons=1 if kind != "mouseReleased" else 0, clickCount=1, pointerType="mouse")
    time.sleep(pause)
    return True, point


def real_click(cdp, selector: str, pause: float = 0.2) -> tuple[bool, dict]:
    """A real mouse click on an element that is already on screen.

    Deliberately does NOT scroll first: `browser_check.mouse_click` scrolls the target into
    view, which would silently move the very geometry this suite measures. The caller places
    the page first; if the target is not hittable the failure is loud, not silent.
    """
    return _click_at(cdp, js(cdp, hit_js_selector(selector)), pause=pause)


PAIR_NODE = """(function(){
  var nodes = document.querySelectorAll('.item[data-id="' + %d + '"]');
  for (var i = 0; i < nodes.length; i++) {
    var cell = nodes[i].closest('.cell--c1, .cell--c2, .cell--c3');
    var m = cell ? /cell--c(\\d)/.exec(cell.className) : null;
    if (m && Number(m[1]) === %d) return nodes[i];
  }
  return null;
})()"""


def real_click_pair(cdp, tid: int, col: int, pause: float = 0.2) -> tuple[bool, dict]:
    """Real click on one specific (id, lane) instance — never "whichever copy comes first"."""
    return _click_at(cdp, js(cdp, hit_js_node(PAIR_NODE % (tid, col))), pause=pause)


def _tap_at(cdp, point: dict) -> tuple[bool, dict]:
    if not point or point.get("missing") or not point.get("visible") or not point.get("hits"):
        return False, point or {}
    cdp.call("Input.dispatchTouchEvent", type="touchStart",
             touchPoints=[{"x": point["x"], "y": point["y"]}])
    time.sleep(0.05)
    cdp.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
    time.sleep(0.25)
    return True, point


def real_tap(cdp, selector: str) -> tuple[bool, dict]:
    """A real touch tap (touchStart/touchEnd) on a point that really belongs to the element."""
    return _tap_at(cdp, js(cdp, hit_js_selector(selector)))


def place_pair(cdp, tid: int, col: int, want_top: float) -> dict:
    """Scroll so the (id, lane) instance sits at `want_top`."""
    return js(
        cdp,
        """(function(){
             var n = %s;
             if (!n) return {missing: true};
             var r = n.getBoundingClientRect();
             window.scrollTo(0, Math.max(0, r.top + window.scrollY - %f));
             var after = n.getBoundingClientRect();
             return {top: Math.round(after.top * 100) / 100, col: %d,
                     scrollY: Math.round(window.scrollY)};
           })()""" % (PAIR_NODE % (tid, col), want_top, col),
    )


def key_activate(cdp, tid: int) -> bool:
    """Real keyboard activation: a real Tab (marks the input as keyboard), focus, real Enter."""
    press_key(cdp, "Tab")
    ok = js(cdp, """(function(){var n=document.querySelector('.item[data-id="%d"]');
                    if(!n) return false; n.focus(); return document.activeElement===n;})()""" % tid)
    if not ok:
        return False
    press_key(cdp, "Enter", code="Enter", vk=13, text="\r")
    time.sleep(0.3)
    return True


def press_key(cdp, key: str, *, code: str = "", vk: int = 0, text: str = "") -> None:
    """A real key press (used both for activation and for a genuine keyboard scroll)."""
    code = code or key
    vk = vk or {"Tab": 9, "Enter": 13, "Escape": 27, "PageDown": 34, "PageUp": 33, "Home": 36,
                "End": 35, "ArrowDown": 40}.get(key, 0)
    for kind in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", type=kind, key=key, code=code,
                 windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, text=text if kind == "keyDown" else "")


def item_point(cdp, selector: str) -> dict:
    return js(cdp, hit_js_selector(selector)) or {}


def move_to(cdp, x: int, y: int) -> None:
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, pointerType="mouse")


def park_mouse(cdp) -> None:
    """Rest the cursor off the board and off the drawer (4,4 is in the masthead)."""
    move_to(cdp, 4, 4)
    time.sleep(0.35)


def drawer_open(cdp) -> bool:
    return bool(js(cdp, "document.querySelector('#drawer').hidden === false && "
                        "document.querySelector('#d-title').textContent.length > 0"))


def drawer_state(cdp) -> dict:
    return js(cdp, """(function(){
        var d = document.querySelector('#drawer');
        return {hidden: d.hidden, open: d.hidden === false, cls: String(d.className || ''),
                title: (document.querySelector('#d-title').textContent || '').slice(0, 24)};
    })()""") or {}


def close_preview(cdp, *, via: str = "click") -> bool:
    """Close the preview with real input, and never silently: no HTMLElement.click() fallback.

    ``via="click"`` aims at 关闭 and reports the aim trace when the native hit test cannot reach
    it; ``via="escape"`` is the other genuinely real close path the contract allows (a real Esc
    key press). Either way the caller gets a truthful False if the drawer did not close.
    """
    if via == "escape":
        press_key(cdp, "Escape")
    else:
        ok, point = real_click(cdp, "#d-close")
        if not ok:
            print(f"      (harness: #d-close not reachable: {point})")
            return False
    return bool(wait_js(cdp, "document.querySelector('#drawer').hidden === true", timeout=4.0))


def density_state(cdp) -> dict:
    """The control's own state, plus the board class as a diagnostic only.

    `pressed` (aria-pressed) and `pref` (the persisted preference the handler writes) are what a
    toggle can be witnessed on. `compactClass` is reported but must never be used to infer it:
    render() sets `is-compact` only on the desktop board, so on mobile it is constant.
    """
    return js(cdp, """(function(){
        var b = document.querySelector('#density');
        var pref = null;
        try { pref = localStorage.getItem('linuxdo-ai.density'); } catch (e) { pref = 'ERR'; }
        return {pressed: b.getAttribute('aria-pressed'),
                pref: pref,
                compactClass: document.querySelector('#board').classList.contains('is-compact'),
                scrollY: Math.round(window.scrollY),
                scrollMax: Math.round(document.documentElement.scrollHeight - window.innerHeight)};
    })()""") or {}


def density_mode(cdp):
    """The density mode the page is really in, from the control: 'compact' | 'gap' | None."""
    st = density_state(cdp)
    pressed, pref = st.get("pressed"), st.get("pref")
    mode = "compact" if pressed == "true" else ("gap" if pressed == "false" else None)
    if mode and pref in ("compact", "gap") and pref != mode:
        print(f"      (harness: aria-pressed={pressed} disagrees with the stored pref {pref})")
        return None
    return mode


LAST_TOGGLE: dict = {}


def toggle_density(cdp, *, expect=None, touch=False, settle=0.55, after_click=None) -> bool:
    """Return to the masthead and click/tap the density control with real input.

    Consumption is witnessed on the control itself (aria-pressed, cross-checked with the stored
    preference) — the board's `is-compact` class is not a witness on mobile, where render()
    never sets it. A click/tap that demonstrably did not reach the control fails loudly with the
    aim trace; there is no "re-aim" retry, because a retry driven by the wrong signal is exactly
    what hides a real miss.

    `settle` is how long to wait before reading the mode back; `after_click` runs immediately
    after the click (real input that must land inside the same window, e.g. a scroll right after
    the toggle).
    """
    js(cdp, "window.scrollTo(0, 0)")
    time.sleep(0.35)
    before = density_mode(cdp)
    if touch:
        ok, point = real_tap(cdp, "#density")
    else:
        ok, point = real_click(cdp, "#density", pause=0.0)
    if not ok:
        print(f"      (harness: could not reach #density: {point})")
        return False
    LAST_TOGGLE["click_at"] = time.time()
    if after_click is not None:
        after_click()
    time.sleep(settle)
    after = density_mode(cdp)
    if after is None or after == before:
        print(f"      (harness: #density not consumed at {point}: mode {before} -> {after})")
        return False
    if expect is not None and after != expect:
        print(f"      (harness: #density settled in {after}, expected {expect})")
        return False
    return True


def read_marks(cdp) -> list:
    return js(cdp, """(function(){
        var out = {};
        try { for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            if (k.indexOf('linuxdo-ai.read') === 0) out[k] = localStorage.getItem(k);
        } } catch (e) { out['ERR'] = String(e); }
        return out;
    })()""") or {}


def ls_keys(cdp) -> list:
    return js(cdp, """(function(){var out=[];try{for(var i=0;i<localStorage.length;i++)out.push(localStorage.key(i));}
                      catch(e){out.push('ERR');}return out.sort();})()""") or []


def errors(cdp) -> list:
    return js(cdp, "window.__u07errs || []") or []


def arm_errors(cdp) -> None:
    js(cdp, """(function(){window.__u07errs=[];
        window.addEventListener('error', function(e){window.__u07errs.push(String(e.message));});
        return true;})()""")


def reqs_since(mark: int) -> list[dict]:
    return bc.StubHandler.requests[mark:]


def req_mark() -> int:
    return len(bc.StubHandler.requests)


def api_calls(reqs: list[dict]) -> list[str]:
    return [f"{r['method']} {r['path']}" for r in reqs if str(r.get("path", "")).startswith("/api/")]


def detail_get(reqs: list[dict]) -> list[str]:
    return [r["path"] for r in reqs if str(r.get("path", "")).startswith("/api/topic/")]


def posts(reqs: list[dict]) -> list[str]:
    return [f"{r['method']} {r['path']}" for r in reqs if r["method"] == "POST"]


def off_origin(cdp) -> list[str]:
    return js(cdp, """(function(){var bad=[];
        (performance.getEntriesByType('resource')||[]).forEach(function(e){
          try { var u = new URL(e.name, location.href);
                if (u.origin !== location.origin) bad.push(e.name); } catch (err) { bad.push(e.name); }
        }); return bad;})()""") or []


VISIBLE_NEED = """var h = window.innerHeight;
    var need = Math.min(%d, Math.max(1, r.height));
    var inter = Math.min(r.bottom, h) - Math.max(r.top, 0);"""


def topmost_lane_item(cdp, col: int, min_visible: int = 24):
    """The article the reader is looking at, by the published U07 rule.

    The first item of that lane whose INTERSECTION with the viewport is at least `min_visible`
    px (capped for a row shorter than that). A ~2px sliver at the bottom edge and a row that is
    entirely below the viewport are both "not on screen" — the previous form
    (bottom > 24 && top < height - 1) accepted ~2px of a row.
    """
    return js(cdp, """(function(){
        var nodes = document.querySelectorAll('.cell--c' + %d + ' > .item');
        for (var i = 0; i < nodes.length; i++) {
          var r = nodes[i].getBoundingClientRect();
          %s
          if (r.height > 0 && inter >= need) {
            return {id: Number(nodes[i].dataset.id), top: Math.round(r.top * 100) / 100,
                    col: %d, inter: Math.round(inter)};
          }
        }
        return null;
      })()""" % (col, VISIBLE_NEED % min_visible, col))


VISIBLE_ROW_POINT = """(function(){
  var nodes = document.querySelectorAll('.cell--c' + %d + ' > .item');
  var h = window.innerHeight;
  var seen = 0, skip = %d;
  for (var i = 0; i < nodes.length; i++) {
    var r = nodes[i].getBoundingClientRect();
    if (r.height <= 0) continue;
    var x = Math.round(r.left + r.width / 2);
    var y = Math.round(r.top + r.height / 2);
    if (x > window.innerWidth - 430) continue;        /* left of the desktop drawer panel */
    if (y < 4 || y > h - 4) continue;                 /* its own centre is really on screen */
    var el = document.elementFromPoint(x, y);
    if (!el || !(el === nodes[i] || nodes[i].contains(el))) continue;
    if (seen++ < skip) continue;
    return {x: x, y: y, id: Number(nodes[i].dataset.id), hits: true,
            rect: [Math.round(r.top), Math.round(r.bottom)]};
  }
  return {missing: true};
})()"""


def visible_row_point(cdp, col: int = 1, skip: int = 0) -> dict:
    """A lane row whose own centre is on screen and hittable right now (left of the drawer)."""
    return js(cdp, VISIBLE_ROW_POINT % (col, skip)) or {"missing": True}


def doc_top(cdp, tid: int):
    """The document offset of the first instance of `tid` (independent of the scroll)."""
    return js(cdp, """(function(){var n=document.querySelector('.item[data-id="%d"]');
        return n?Math.round((n.getBoundingClientRect().top+window.scrollY)*100)/100:null;})()""" % tid)


INPUT_ARM = """(function(){
  window.__u07inputs = [];
  function rec(kind){ return function(e){
    var t = e.target;
    var item = t && t.closest ? t.closest('.item') : null;
    var btn = t && t.closest ? t.closest('#density') : null;
    window.__u07inputs.push({kind: kind,
      target: String((t && (t.id || t.className || t.tagName)) || '').slice(0, 40),
      item: item ? Number(item.dataset.id) : null,
      density: !!btn, ptype: e.pointerType || null, trusted: !!e.isTrusted,
      x: (e.clientX === undefined ? null : Math.round(e.clientX)),
      y: (e.clientY === undefined ? null : Math.round(e.clientY))});
  };}
  ['pointerdown','mousedown','click','touchstart'].forEach(function(kind){
    document.addEventListener(kind, rec(kind), true);
  });
  return true;
})()"""

TRACE_ARM = """(function(){
  window.__u07trace = [];
  function rec(kind){ return function(e){
    var t = e.target;
    var node = t && t.closest ? t.closest('.item') : null;
    window.__u07trace.push({kind: kind,
      target: String((t && (t.id || t.className || t.tagName)) || '').slice(0, 32),
      item: node ? Number(node.dataset.id) : null,
      x: (e.clientX === undefined ? null : Math.round(e.clientX)),
      y: (e.clientY === undefined ? null : Math.round(e.clientY)),
      mx: (e.movementX === undefined ? null : e.movementX),
      my: (e.movementY === undefined ? null : e.movementY),
      ptype: e.pointerType || null});
  };}
  ['mouseover','mousemove','mouseout','pointermove','wheel','keydown'].forEach(function(kind){
    document.addEventListener(kind, rec(kind), true);
  });
  return true;
})()"""

DRAWER_ARM = """(function(){
  window.__u07drawer = [];
  var d = document.querySelector('#drawer');
  var open = d.hidden === false;
  new MutationObserver(function(){
    var now = d.hidden === false;
    if (now !== open) {
      open = now;
      window.__u07drawer.push({open: now, at: Math.round(performance.now()),
        title: (document.querySelector('#d-title').textContent || '').slice(0, 24)});
    }
  }).observe(d, {attributes: true, attributeFilter: ['hidden', 'class']});
  return true;
})()"""


def arm_inputs(cdp) -> None:
    js(cdp, INPUT_ARM)


def inputs(cdp) -> list[dict]:
    return js(cdp, "window.__u07inputs || []") or []


def trace_arm(cdp) -> None:
    js(cdp, TRACE_ARM)


def trace_end(cdp) -> list[dict]:
    return js(cdp, "window.__u07trace || []") or []


def drawer_arm(cdp) -> None:
    js(cdp, DRAWER_ARM)


def drawer_log(cdp) -> list[dict]:
    return js(cdp, "window.__u07drawer || []") or []


def reset_stub(n: int = 48, queue_picks=None, head_picked: int = 0) -> dict:
    """A fresh page-level state for each phase (stub only)."""
    dataset, queue = build_dataset(n, head_picked)
    if queue_picks is not None:
        queue = list(queue_picks)
    bc.StubHandler.state = state_payload(dataset, queue)
    bc.StubHandler.requests = []
    return bc.StubHandler.state


def open_fresh(cdp, url: str, *, density: str = "compact", clear: bool = True, settle_s=2.0):
    """Fresh page with a known density preference (browser-local, as the reader has it)."""
    if clear:
        open_page(cdp, url, settle_s=1.4)
        js(cdp, "try{localStorage.clear();}catch(e){} true")
    js(cdp, "try{localStorage.setItem('linuxdo-ai.density', %s);}catch(e){} true" % json.dumps(density))
    open_page(cdp, url, settle_s=settle_s)
    arm_errors(cdp)


def ensure_owner(cdp) -> bool:
    if js(cdp, "document.querySelector('#authchip').textContent") == "已连接 · 可写":
        return True
    js(cdp, "window.prompt = function(){ return %s; };" % json.dumps(OWNER_TOKEN))
    js(cdp, "document.querySelector('#owner').click()")
    return bool(wait_js(cdp, "document.querySelector('#authchip').textContent === '已连接 · 可写'",
                        timeout=6.0))


# --------------------------------------------------------------------------- phases


def phase_a(cdp, url: str) -> None:
    """The exact reported flow, both directions."""
    print("--- phase A: the reported flow (compact -> gap -> compact) ---")
    state = reset_stub()
    bookmarked = set(state["queue"])
    in_lane1 = [t["id"] for t in state["state"]["topics"]
                if t["state"] != "picked" and t["id"] not in bookmarked][8]
    picked_dup = 9200000 + DUP_INDEX

    open_fresh(cdp, url, density="compact")
    if not ensure_owner(cdp):
        check(False, "A1 precondition: the stub is writable with the owner token")
        return
    check(True, "A0 the stub is the only backend in play (owner token accepted)")

    placed = placed_geometry(cdp, in_lane1, WANT_TOP)
    inst = geom(cdp, in_lane1)
    check(len(inst) == 1 and inst[0]["col"] == 1 and abs(placed.get("top", -999) - WANT_TOP) <= 2,
          "A1 the target row is unique and parked in lane 1 at the requested offset",
          f"geom={inst} placed={placed}")

    # fixture sanity, asserted (and before the bookmark action it depends on): the topic that is
    # both picked and bookmarked really carries one instance in lane 2 and one in lane 3
    dup_inst = geom(cdp, picked_dup)
    check(sorted(r["col"] for r in dup_inst) == [2, 3] and len(dup_inst) == 2,
          "A2 fixture sanity: the picked+bookmarked topic has exactly one instance in lane 2 and one in lane 3",
          f"dup={[(r['col'], r['top']) for r in dup_inst]}")

    ok, point = real_click(cdp, f'.item[data-id="{in_lane1}"]')
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    check(ok and bool(opened), "A3 an explicit open of the parked row pins the preview",
          f"click={point} opened={bool(opened)}")

    ok, _ = real_click(cdp, "#d-queue")
    bookmarked_now = wait_js(
        cdp,
        "document.querySelectorAll('.item[data-id=\"%d\"]').length === 2" % in_lane1,
        timeout=5.0)
    after_bookmark = geom(cdp, in_lane1)
    lanes = sorted(r["col"] for r in after_bookmark)
    check(ok and bool(bookmarked_now) and lanes == [1, 3],
          "A4 the explicit bookmark went to the isolated stub and the topic now sits in two lanes",
          f"click_ok={ok} lanes={lanes}")

    closed = close_preview(cdp)
    check(closed, "A5 the preview closes (the real 关闭 click path stays intact)")

    expect = lane_top(cdp, in_lane1, 1)
    stored_top = WANT_TOP if not expect else expect["top"]
    before = density_state(cdp)
    mark = req_mark()
    reads_before = read_marks(cdp)
    keys_before = ls_keys(cdp)

    toggled = toggle_density(cdp, expect="gap")
    after = density_state(cdp)
    restored = lane_top(cdp, in_lane1, 1)
    delta = None if not restored else round(restored["top"] - stored_top, 2)
    detail = (f"stored_top={stored_top} after={None if not restored else restored['top']} delta={delta} "
              f"state_before={before} state_after={after} placed={placed}")
    check(toggled and restored is not None and abs(delta) <= TOL,
          "A6 A: compact -> gap restores the SAME id in the SAME lane at the stored offset",
          detail)

    during = reqs_since(mark)
    check(not posts(during) and not detail_get(during) and read_marks(cdp) == reads_before,
          "A7 the toggle itself is inert: no POST, no detail GET, no read-set change",
          f"posts={posts(during)} detail={detail_get(during)} reads={read_marks(cdp)}")
    check(not off_origin(cdp), "A8 the toggle loads nothing off-origin", f"{off_origin(cdp)}")

    # back again: gap -> compact, the same pair and offset must survive
    stored_top_b = None if not restored else restored["top"]
    mark_b = req_mark()
    reads_b = read_marks(cdp)
    toggled_b = toggle_density(cdp, expect="compact")
    back = lane_top(cdp, in_lane1, 1)
    delta_b = None if not back else round(back["top"] - stored_top_b, 2)
    during_b = reqs_since(mark_b)
    check(toggled_b and back is not None and abs(delta_b) <= TOL,
          "A9 B: gap -> compact restores the same id+lane+offset again",
          f"stored_top={stored_top_b} after={None if not back else back['top']} delta={delta_b} "
          f"state={density_state(cdp)}")
    check(not posts(during_b) and not detail_get(during_b) and read_marks(cdp) == reads_b,
          "A10 the reverse toggle is inert too", f"posts={posts(during_b)} detail={detail_get(during_b)}")
    check(ls_keys(cdp) == keys_before, "A11 no new browser storage appears across the toggles",
          f"before={keys_before} after={ls_keys(cdp)}")


def phase_b(cdp, url: str) -> None:
    """Lane fidelity: picked origin, bookmarks origin, the duplicated id, keyboard."""
    print("--- phase B: lanes and keyboard activation ---")
    state = reset_stub()
    picked = [t["id"] for t in state["state"]["topics"] if t["state"] == "picked"][3]
    bookmark_only = [i for i in state["queue"] if str(i) not in
                     [str(t["id"]) for t in state["state"]["topics"] if t["state"] == "picked"]][1]
    dup = 9200000 + DUP_INDEX

    # B: from the 精选 lane
    open_fresh(cdp, url, density="gap")
    ensure_owner(cdp)
    placed = place_pair(cdp, picked, 2, WANT_TOP)
    ok, _ = real_click_pair(cdp, picked, 2)
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    stored = lane_top(cdp, picked, 2)
    toggled = toggle_density(cdp, expect="compact")
    got = lane_top(cdp, picked, 2)
    check(ok and closed and toggled and (stored or {}).get("col") == 2 and (got or {}).get("col") == 2 and
          got is not None and abs(got["top"] - (stored or {}).get("top", -999)) <= TOL,
          "B1 a 精选-origin article comes back in lane 2 at its offset",
          f"placed={placed} closed={closed} stored={stored} got={got}")

    # B: from the 收藏 lane. The pair this check is about is the lane-3 one: place_pair parks
    #    THAT instance at the offset and real_click_pair activates THAT instance, so the origin
    #    is settled before the toggle. Closing the preview restores the focus with preventScroll
    #    and must not advance the pair either.
    open_fresh(cdp, url, density="compact")
    lanes_rest = sorted(r["col"] for r in geom(cdp, bookmark_only))
    placed = place_pair(cdp, bookmark_only, 3, WANT_TOP)
    if 3 not in lanes_rest or placed.get("missing"):
        check(False, "B2 precondition: the bookmarks-lane target exists in lane 3",
              f"lanes={lanes_rest} placed={placed}")
    else:
        ok, point = real_click_pair(cdp, bookmark_only, 3)
        opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
        pre_close = lane_top(cdp, bookmark_only, 3)
        closed = close_preview(cdp)
        stored = lane_top(cdp, bookmark_only, 3)
        other_lane = lane_top(cdp, bookmark_only, 1)
        toggled = toggle_density(cdp, expect="gap")
        got = lane_top(cdp, bookmark_only, 3)
        other_after = lane_top(cdp, bookmark_only, 1)
        keep = abs((stored or {}).get("top", -999) - placed.get("top", -4242)) <= TOL
        check(ok and bool(opened) and closed and toggled and
              placed.get("col") == 3 and (stored or {}).get("col") == 3 and (got or {}).get("col") == 3 and
              keep and abs(got["top"] - (stored or {}).get("top", -999)) <= TOL and
              other_lane is not None and abs(other_lane["top"] - stored["top"]) > TOL,
              "B2 a 收藏-origin article comes back in the SAME lane-3 pair at its offset "
              "(and closing the preview did not advance it)",
              f"click={point} opened={bool(opened)} pre_close={pre_close} closed={closed} "
              f"toggled={toggled} state={density_state(cdp)} placed={placed} stored={stored} "
              f"lane1_at_rest={other_lane} got={got} lane1_after={other_after}")

    # B: the duplicated topic — anchored on its 精选 copy in gap mode, restored into the
    #    精选 stack in compact mode. In compact the two stacks sit at different offsets, so
    #    restoring the wrong lane would be visible; in gap mode they share a row and would not.
    open_fresh(cdp, url, density="gap")
    placed = place_pair(cdp, dup, 2, WANT_TOP)
    lanes_at_rest = sorted(r["col"] for r in geom(cdp, dup))
    ok, _ = real_click_pair(cdp, dup, 2)
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    close_preview(cdp)
    stored = lane_top(cdp, dup, 2)
    toggled = toggle_density(cdp, expect="compact")
    got2 = lane_top(cdp, dup, 2)
    got3 = lane_top(cdp, dup, 3)
    other_off = None if not (got2 and got3) else round(got3["top"] - got2["top"], 2)
    check(lanes_at_rest == [2, 3] and toggled and got2 is not None and
          abs(got2["top"] - (stored or {}).get("top", -999)) <= TOL and abs(other_off or 0) > TOL,
          "B3 the duplicated topic returns in its ORIGIN lane, not the copy in the other lane",
          f"lanes={lanes_at_rest} stored={stored} lane2={got2} lane3={got3} other_delta={other_off}")

    # B: keyboard activation records the same anchor
    open_fresh(cdp, url, density="gap")
    kb_target = [t["id"] for t in state["state"]["topics"] if t["state"] != "picked"][20]
    placed = placed_geometry(cdp, kb_target, WANT_TOP)
    activated = key_activate(cdp, kb_target)
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    stored = lane_top(cdp, kb_target, 1)
    toggled = toggle_density(cdp, expect="compact")
    got = lane_top(cdp, kb_target, 1)
    check(activated and bool(opened) and closed and toggled and got is not None and
          abs(got["top"] - (stored or {}).get("top", -999)) <= TOL,
          "B4 a keyboard-activated article is restored like a clicked one",
          f"placed={placed} activated={activated} opened={bool(opened)} closed={closed} "
          f"stored={stored} got={got}")


def phase_c(cdp, url: str) -> None:
    """Browsing semantics: plain scroll, a stale explicit article, slivers, gaps, rapid toggles."""
    print("--- phase C: browsing semantics ---")
    state = reset_stub()
    ids = [t["id"] for t in state["state"]["topics"] if t["state"] != "picked"]

    # plain browsing: no explicit open at all, just scroll into the list
    open_fresh(cdp, url, density="compact")
    placed = placed_geometry(cdp, ids[14], SAMPLE_TOP)
    time.sleep(0.5)                       # the bounded sampler settles
    expect = topmost_lane_item(cdp, 1)
    inst = lane_top(cdp, ids[14], 1)
    stored = None if not inst else inst["top"]
    toggled = toggle_density(cdp, expect="gap")
    got = lane_top(cdp, ids[14], 1)
    check(toggled and abs(placed.get("top", -999) - SAMPLE_TOP) <= 2 and
          expect is not None and expect["id"] == ids[14] and got is not None and
          abs(got["top"] - (stored or -999)) <= TOL,
          "C1 plain scrolling (no preview) already establishes an anchor",
          f"placed={placed} expect={expect} stored={stored} got={got}")

    # a stale explicit article, then a deliberate settled scroll deeper in the list
    open_fresh(cdp, url, density="compact")
    old = ids[6]
    placed = placed_geometry(cdp, old, WANT_TOP)
    real_click(cdp, f'.item[data-id="{old}"]')
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    close_preview(cdp)
    deeper = ids[30]
    placed2 = placed_geometry(cdp, deeper, SAMPLE_TOP)
    time.sleep(0.6)
    expect_anchor = topmost_lane_item(cdp, 1)
    old_geom = lane_top(cdp, old, 1)
    toggled = toggle_density(cdp, expect="gap")
    got_anchor = lane_top(cdp, expect_anchor["id"], 1) if expect_anchor else None
    got_old = lane_top(cdp, old, 1)
    check(toggled and expect_anchor is not None and expect_anchor["id"] != old and
          expect_anchor["top"] > -TOL and
          got_anchor is not None and abs(got_anchor["top"] - expect_anchor["top"]) <= TOL and
          got_old is not None and got_old["top"] < -TOL,
          "C2 deeper settled browsing advances the anchor; the toggle does not yank back to the old article",
          f"old_at_rest={old_geom} expect_anchor={expect_anchor} after={got_anchor} "
          f"old_after={got_old} placed2={placed2}")

    # a ~2px sliver at the viewport edge is not "the article being read": the settled deliberate
    # scroll past the parked article advances the browsing anchor to what is really on screen
    open_fresh(cdp, url, density="compact")
    parked = ids[10]
    placed = placed_geometry(cdp, parked, WANT_TOP)
    real_click(cdp, f'.item[data-id="{parked}"]')
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    height = js(cdp, "window.innerHeight")
    sliver = placed_geometry(cdp, parked, height - 2)
    time.sleep(0.6)                       # the bounded sampler settles
    on_screen = topmost_lane_item(cdp, 1)
    sliver_now = lane_top(cdp, parked, 1)
    toggled = toggle_density(cdp, expect="gap")
    got_on_screen = lane_top(cdp, on_screen["id"], 1) if on_screen else None
    got_parked = lane_top(cdp, parked, 1)
    check(closed and toggled and on_screen is not None and on_screen["id"] != parked and
          got_on_screen is not None and abs(got_on_screen["top"] - on_screen["top"]) <= TOL and
          got_parked is not None and abs(got_parked["top"] - WANT_TOP) > TOL,
          "C3 a ~2px sliver at the edge is not 'visible enough': scrolling past the parked article advances the anchor",
          f"placed={placed} sliver={sliver} sliver_now={sliver_now} on_screen={on_screen} "
          f"got_on_screen={got_on_screen} got_parked={got_parked}")

    # a sparse lane: 收藏 holds two rows ~40 apart, so the viewport can sit in a stretch where NO
    # 收藏 row intersects it. The passive sample must not silently anchor the off-screen topic
    # from that gap — the article really on screen (全部) stays the reading position.
    reset_stub(48, queue_picks=[9200001, 9200045])
    open_fresh(cdp, url, density="gap")
    ensure_owner(cdp)
    placed = place_pair(cdp, 9200001, 3, WANT_TOP)
    ok, _ = real_click_pair(cdp, 9200001, 3)
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    press_key(cdp, "PageDown")            # a real scroll deeper, into the lane-3 gap
    time.sleep(0.4)
    press_key(cdp, "PageDown")
    time.sleep(0.9)                       # the bounded sampler settles
    gap_scroll = js(cdp, "Math.round(window.scrollY)")
    lane3_on_screen = topmost_lane_item(cdp, 3)
    offscreen3 = lane_top(cdp, 9200045, 3)
    on_screen = topmost_lane_item(cdp, 1)
    toggled = toggle_density(cdp, expect="compact")
    got_on_screen = lane_top(cdp, on_screen["id"], 1) if on_screen else None
    check(ok and bool(opened) and closed and toggled and lane3_on_screen is None and
          offscreen3 is not None and offscreen3["top"] > 900 and
          on_screen is not None and on_screen["id"] != 9200001 and got_on_screen is not None and
          abs(got_on_screen["top"] - on_screen["top"]) <= TOL,
          "C4 a whole-viewport gap in a sparse lane: the sample keeps the article on screen, not the off-screen 收藏 row",
          f"placed={placed} gap_scroll={gap_scroll} lane3_on_screen={lane3_on_screen} "
          f"offscreen3={offscreen3} on_screen={on_screen} got_on_screen={got_on_screen}")

    # repeated rapid toggles
    reset_stub()
    open_fresh(cdp, url, density="compact")
    placed = placed_geometry(cdp, ids[12], WANT_TOP)
    real_click(cdp, f'.item[data-id="{ids[12]}"]')
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    close_preview(cdp)
    stored = lane_top(cdp, ids[12], 1)
    seq_ok = True
    for _ in range(2):
        js(cdp, "window.scrollTo(0, 0)")
        time.sleep(0.1)
        ok, _ = real_click(cdp, "#density")
        if not ok:
            seq_ok = False
            break
        time.sleep(0.12)          # faster than the sampler/animation can settle
    time.sleep(0.8)
    state_after = density_state(cdp)
    got_final = lane_top(cdp, ids[12], 1)
    check(seq_ok and state_after.get("pressed") == "true" and got_final is not None and
          abs(got_final["top"] - (stored or {}).get("top", -999)) <= TOL and not errors(cdp),
          "C5 two rapid toggles settle in the last requested mode with the anchor intact",
          f"state={state_after} stored={stored} got={got_final} errors={errors(cdp)}")

    # a genuine scroll that arrives right after the toggle must not be swallowed with our own
    # compensation scroll: only the compensation is ignored, not the reader's movement after it
    open_fresh(cdp, url, density="compact")
    index = ids[2]                        # a row far above where the PageDown lands
    placed = placed_geometry(cdp, index, WANT_TOP)
    real_click(cdp, f'.item[data-id="{index}"]')
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    stored = lane_top(cdp, index, 1)
    stamp = {}

    def keep_scrolling():
        stamp["at"] = time.time()
        press_key(cdp, "PageDown")

    toggled = toggle_density(cdp, expect="gap", settle=0.05, after_click=keep_scrolling)
    click_to_key_ms = int((stamp.get("at", time.time()) - LAST_TOGGLE.get("click_at", time.time())) * 1000)
    park_mouse(cdp)
    time.sleep(1.1)                       # the sampler settles on the deeper row
    deeper_now = topmost_lane_item(cdp, 1)
    deep_scroll = js(cdp, "Math.round(window.scrollY)")
    if deeper_now is not None:
        deeper_top = lane_top(cdp, deeper_now["id"], 1)
    else:
        deeper_top = None
    press_key(cdp, "Home")                # real input back to the masthead
    time.sleep(0.45)
    park_mouse(cdp)                       # the reader moves to the control (no stale hover)
    time.sleep(0.35)
    toggled_b = toggle_density(cdp, expect="compact")
    got_deep = lane_top(cdp, deeper_now["id"], 1) if deeper_now else None
    got_old = lane_top(cdp, index, 1)
    check(toggled and closed and toggled_b and deeper_now is not None and
          deeper_now["id"] != index and deep_scroll > 300 and deeper_top is not None and
          got_deep is not None and abs(got_deep["top"] - deeper_top["top"]) <= TOL and
          got_old is not None and got_old["top"] < -TOL and not errors(cdp),
          "C6 a genuine scroll right after the toggle still advances the anchor (only our own scroll is ignored)",
          f"click_to_key_ms={click_to_key_ms} stored={stored} deep_scroll={deep_scroll} "
          f"deeper={deeper_now} deeper_top={deeper_top} got_deep={got_deep} got_old={got_old}")

    # C7: a genuine scroll can outrun the 140 ms trailing sample. The reader activates an article,
    # toggles density from the keyboard (focus stays on the control), scrolls with a REAL wheel and
    # immediately activates the still-focused control again — before the trailing sample fires. The
    # plan must be resolved from the viewport the reader is actually looking at, synchronously,
    # never from the stale anchor that that same scroll has already pushed off screen.
    #
    # Everything the assertion needs from *before* the click (the stale row's visibility, the pair at
    # the top of the viewport, the scroll position and the gap since the last scroll event) is
    # captured in-page inside the control's click, before the handler's reflow — so no CDP round trip
    # slips in between the scroll and the activation and quietly lets the trailing sample expire.
    reset_stub()
    open_fresh(cdp, url, density="compact")
    ensure_owner(cdp)
    stale = ids[4]
    placed_s = placed_geometry(cdp, stale, WANT_TOP)
    ok_s, _ = real_click(cdp, f'.item[data-id="{stale}"]')
    opened_s = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed_s = close_preview(cdp)
    focused_s = js(cdp, """(function(){var b=document.getElementById('density');
        b.focus({preventScroll:true}); return document.activeElement === b;})()""")
    press_key(cdp, " ", code="Space", vk=32, text=" ")      # keyboard toggle, focus retained
    mode_first = density_mode(cdp)
    js(cdp, """(function(){
        window.__u07 = {scrollAt: -1, sinceScroll: -1, scrolls: 0, staleAtClick: null,
                        viewportAtClick: null, scrollYAtClick: -1, elementAtClick: null};
        document.addEventListener('scroll', function(){
            window.__u07.scrollAt = performance.now(); window.__u07.scrolls++; },
            {passive: true, capture: true});
        document.addEventListener('click', function(ev){
            if (!ev.target || ev.target.id !== 'density') return;
            var u = window.__u07;
            u.sinceScroll = Math.round(performance.now() - u.scrollAt);
            u.scrollYAtClick = Math.round(window.scrollY);
            u.elementAtClick = String((document.activeElement || {}).id || '');
            var stale = document.querySelector('.item[data-id="%d"]');
            if (stale) { var r = stale.getBoundingClientRect();
                         u.staleAtClick = {top: Math.round(r.top * 100) / 100,
                                           bottom: Math.round(r.bottom * 100) / 100,
                                           inter: Math.round(Math.min(r.bottom, window.innerHeight)
                                                             - Math.max(r.top, 0))}; }
            var nodes = document.querySelectorAll('.cell--c1 > .item');
            for (var i = 0; i < nodes.length; i++) {
                var q = nodes[i].getBoundingClientRect();
                if (q.height <= 0) continue;
                var inter = Math.min(q.bottom, window.innerHeight) - Math.max(q.top, 0);
                if (inter >= Math.min(24, q.height)) {
                    u.viewportAtClick = {id: Number(nodes[i].dataset.id), col: 1,
                                         top: Math.round(q.top * 100) / 100};
                    break;
                }
            }
        }, {capture: true});
        return true;})()""" % stale)
    # A real WHEEL, then the control at once — as soon as the stale article is really off screen and
    # without waiting out the trailing sample. The wheel is aimed at a point inside no scrollable
    # row/cell of its own, so the document is what scrolls; and unlike a keyboard scroll it does not
    # depend on where the (still focused, now off-screen) control sits.
    base_y = js(cdp, "Math.round(window.scrollY)")
    wheel_at = js(cdp, """(function(){
        var xs = [6, 10, 1264], ys = [240, 320, 420, 520, 620, 720];
        for (var a = 0; a < xs.length; a++) { for (var b = 0; b < ys.length; b++) {
            var el = document.elementFromPoint(xs[a], ys[b]);
            if (!el || (el.closest && el.closest('.item, .cell, .drawer, header'))) continue;
            return {x: xs[a], y: ys[b], at: String(el.className || el.tagName).slice(0, 24)};
        } }
        return {missing: true};})()""")
    cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=wheel_at.get("x", 6),
             y=wheel_at.get("y", 420), deltaX=0, deltaY=500)
    positions = []
    for _ in range(20):
        time.sleep(0.025)                 # let the compositor actually start the scroll
        y = js(cdp, "Math.round(window.scrollY)")
        positions.append(y)
        if isinstance(y, int) and y >= (base_y or 0) + 300:
            break                         # off screen — activate at once, mid-scroll
    press_key(cdp, " ", code="Space", vk=32, text=" ")       # the still-focused control, at once
    scroll_path = positions
    clocks = js(cdp, """({sinceScroll: window.__u07.sinceScroll, scrolls: window.__u07.scrolls,
        staleAtClick: window.__u07.staleAtClick, viewportAtClick: window.__u07.viewportAtClick,
        scrollYAtClick: window.__u07.scrollYAtClick, activeAtClick: window.__u07.elementAtClick,
        scrollY: Math.round(window.scrollY)})""")
    time.sleep(0.6)
    mode_second = density_mode(cdp)
    seen = (clocks or {}).get("viewportAtClick") or {}
    seen_id = seen.get("id")
    got_seen = lane_top(cdp, seen_id, 1) if seen_id is not None else None
    stale_after = lane_top(cdp, stale, 1)
    at_click = (clocks or {}).get("staleAtClick") or {}
    since = (clocks or {}).get("sinceScroll")
    check(ok_s and bool(opened_s) and closed_s and bool(focused_s) and mode_first == "gap" and
          mode_second == "compact" and isinstance(since, int) and 0 <= since < 140 and
          (clocks or {}).get("activeAtClick") == "density" and
          (clocks or {}).get("scrollYAtClick", 0) >= (base_y or 0) + 300 and
          bool(wheel_at.get("x")) and not wheel_at.get("missing") and
          (at_click.get("inter") or 0) < 24 and seen_id is not None and seen_id != stale and
          seen.get("col") == 1 and got_seen is not None and
          abs(got_seen["top"] - seen.get("top")) <= TOL and stale_after is not None and
          stale_after["bottom"] <= 24 and not errors(cdp),
          "C7 a real scroll outran the 140 ms sample: the toggle keeps the viewport the reader is "
          "looking at, not the stale off-screen article",
          f"placed={placed_s} first={mode_first} wheel_at={wheel_at} base_y={base_y} "
          f"scroll_path={scroll_path} clocks={clocks} "
          f"seen={seen} got_seen={got_seen} stale_after={stale_after} errors={errors(cdp)}")


def phase_d(cdp, url: str) -> None:
    """Safety: empty data, the pair vanishing, the bottom clamp, mobile, storage."""
    print("--- phase D: safety and edges ---")

    # empty data: the control must still work and must not force a scroll
    bc.StubHandler.state = state_payload({"generated_at": "2026-09-26T00:00:00Z", "source": "u07-empty",
                                          "filter": None, "topics": []}, [])
    bc.StubHandler.requests = []
    open_fresh(cdp, url, density="compact", settle_s=1.6)
    toggled = toggle_density(cdp, expect="gap")
    state_after = density_state(cdp)
    check(toggled and state_after.get("scrollY") == 0 and not errors(cdp),
          "D1 empty data: the toggle does not throw and does not scroll",
          f"state={state_after} errors={errors(cdp)}")

    # the anchored pair disappears FROM ITS OWN LANE (the bookmark that was opened is removed
    # while it is the reading position). The origin is proven first: the lane-3 instance is the
    # one parked and the one activated, and the two lanes really sit at different offsets.
    state = reset_stub()
    bookmark_only = [i for i in state["queue"] if str(i) not in
                     [str(t["id"]) for t in state["state"]["topics"] if t["state"] == "picked"]][1]
    open_fresh(cdp, url, density="compact")
    ensure_owner(cdp)
    placed = place_pair(cdp, bookmark_only, 3, WANT_TOP)
    ok, point = real_click_pair(cdp, bookmark_only, 3)
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    stored = lane_top(cdp, bookmark_only, 3)
    other_lane = lane_top(cdp, bookmark_only, 1)
    real_click(cdp, "#d-queue")                     # 取消收藏: THIS lane-3 pair goes away
    gone = wait_js(cdp, "document.querySelectorAll('.item[data-id=\"%d\"]').length === 1" % bookmark_only,
                   timeout=5.0)
    closed = close_preview(cdp)
    after = geom(cdp, bookmark_only)
    js(cdp, "window.scrollTo(0, 0)")
    time.sleep(0.45)                                # the sampler settles at the masthead
    same_lane = topmost_lane_item(cdp, 3)           # the 收藏 article that IS on screen
    mark = req_mark()
    reads = read_marks(cdp)
    toggled = toggle_density(cdp, expect="gap")
    got_same_lane = lane_top(cdp, same_lane["id"], 3) if same_lane else None
    did_after = geom(cdp, bookmark_only)
    during = reqs_since(mark)
    check(ok and bool(opened) and closed and bool(gone) and
          placed.get("col") == 3 and (stored or {}).get("col") == 3 and
          abs(stored["top"] - placed["top"]) <= TOL and
          other_lane is not None and abs(other_lane["top"] - stored["top"]) > TOL and
          len(after) == 1 and after[0]["col"] == 1 and toggled and
          same_lane is not None and got_same_lane is not None and
          abs(got_same_lane["top"] - same_lane["top"]) <= TOL and not errors(cdp),
          "D2 the vanished pair falls back inside its OWN lane (no jump to the other lane's copy)",
          f"click={point} placed={placed} stored={stored} lane1_at_rest={other_lane} "
          f"after_removal={after} same_lane={same_lane} got_same_lane={got_same_lane} "
          f"still_one={did_after} errors={errors(cdp)}")
    check(not posts(during) and not detail_get(during) and read_marks(cdp) == reads,
          "D3 the fallback toggle is inert as well", f"posts={posts(during)} detail={detail_get(during)}")

    # The pair disappears and its own lane has NOTHING on screen (a single bookmark, removed):
    # a visible fallback in that lane is impossible, so the toggle must force no scroll at all —
    # never a cross-lane jump into another lane's article. The detector is the scroll position
    # itself: adopting the 全部 row as the position makes the compensation scroll the document to
    # keep it at its stored offset (the RED run: 0 -> 80). The reflow may still change the layout
    # (an empty 收藏 lane renders its own block), which moves that row inside the viewport while
    # the scroll position — the thing the anchor controls — does not move.
    reset_stub(48, queue_picks=[9200000], head_picked=1)
    open_fresh(cdp, url, density="compact")
    ensure_owner(cdp)
    placed = place_pair(cdp, 9200000, 3, WANT_TOP)
    ok, _ = real_click_pair(cdp, 9200000, 3)
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    real_click(cdp, "#d-queue")
    gone = wait_js(cdp, "document.querySelectorAll('.item[data-id=\"9200000\"]').length === 1",
                   timeout=5.0)
    closed = close_preview(cdp)
    js(cdp, "window.scrollTo(0, 0)")
    time.sleep(0.45)
    lane3_items = js(cdp, "document.querySelectorAll('.cell--c3 > .item').length")
    lane3_on_screen = topmost_lane_item(cdp, 3)
    lane1_on_screen = topmost_lane_item(cdp, 1)
    l1_before = lane_top(cdp, lane1_on_screen["id"], 1) if lane1_on_screen else None
    scroll_before = js(cdp, "Math.round(window.scrollY)")
    toggled = toggle_density(cdp, expect="gap")
    scroll_after = js(cdp, "Math.round(window.scrollY)")
    l1_after = lane_top(cdp, lane1_on_screen["id"], 1) if lane1_on_screen else None
    check(ok and bool(opened) and bool(gone) and closed and toggled and lane3_items == 0 and
          lane3_on_screen is None and lane1_on_screen is not None and
          lane1_on_screen["id"] != 9200000 and scroll_after == scroll_before and
          l1_after is not None and
          (min(l1_after["bottom"], 900) - max(l1_after["top"], 0)) >= 24 and not errors(cdp),
          "D4 the pair is gone and its lane is empty: no forced cross-lane scroll",
          f"placed={placed} lane3_items={lane3_items} lane3_on_screen={lane3_on_screen} "
          f"lane1_on_screen={lane1_on_screen} l1 {l1_before}->{l1_after} "
          f"scrollY {scroll_before}->{scroll_after} errors={errors(cdp)}")

    # bottom clamp: the anchored article is the last row
    reset_stub()
    tail = [t["id"] for t in bc.StubHandler.state["state"]["topics"] if t["state"] != "picked"][-1]
    open_fresh(cdp, url, density="compact")
    js(cdp, "window.scrollTo(0, document.documentElement.scrollHeight)")
    time.sleep(0.5)
    real_click(cdp, f'.item[data-id="{tail}"]')
    wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    close_preview(cdp)
    stored = lane_top(cdp, tail, 1)
    toggled = toggle_density(cdp, expect="gap")
    got = lane_top(cdp, tail, 1)
    state_after = density_state(cdp)
    clamped = state_after.get("scrollY", 0) >= state_after.get("scrollMax", 0) - 2
    visible_after = got is not None and got["top"] < 900 and got["bottom"] > 0
    check(toggled and visible_after and
          (abs(got["top"] - (stored or {}).get("top", -9999)) <= TOL or clamped) and not errors(cdp),
          "D5 a bottom-of-document anchor is clamped by the document bound, not lost",
          f"stored={stored} got={got} state={state_after} clamped={clamped}")

    # mobile: density changes no list geometry there, so a real TAP on the control must flip the
    # mode and add no restoration jump. Consumption is read from the control itself — render()
    # never puts is-compact on the mobile board, so the class cannot witness this.
    reset_stub()
    target = [t["id"] for t in bc.StubHandler.state["state"]["topics"] if t["state"] != "picked"][10]
    open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.8)
    js(cdp, "try{localStorage.clear();localStorage.setItem('linuxdo-ai.density','compact');}catch(e){} true")
    open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.8)
    arm_errors(cdp)
    arm_inputs(cdp)
    placed2 = placed_geometry(cdp, target, WANT_TOP)
    time.sleep(0.4)
    doc_before = doc_top(cdp, target)
    before_state = density_state(cdp)
    ok2 = toggle_density(cdp, touch=True)
    after_state = density_state(cdp)
    taps = inputs(cdp)
    time.sleep(0.5)
    doc_after = doc_top(cdp, target)
    scroll_after = js(cdp, "Math.round(window.scrollY)")
    touch_proof = [t for t in taps if t.get("kind") == "pointerdown" and t.get("density")]
    tap_proof = [t for t in taps if t.get("kind") == "click" and t.get("density")]
    check(ok2 and bool(touch_proof) and bool(tap_proof) and touch_proof[0].get("ptype") == "touch" and
          before_state.get("pressed") == "true" and after_state.get("pressed") == "false" and
          doc_before is not None and doc_after is not None and abs(doc_after - doc_before) <= 2 and
          scroll_after == 0 and not errors(cdp),
          "D6 mobile: a real touch tap flips the control and the toggle changes no geometry",
          f"before={before_state} after={after_state} document_top {doc_before}->{doc_after} "
          f"scrollY_after={scroll_after} touch={touch_proof[:1]} click={tap_proof[:1]} "
          f"placed={placed2} errors={errors(cdp)}")

    # A finger is not a hover. The density tap leaves the layout-hover guard up, and a slow drag
    # across a row must not be mistaken for the reader hovering that row: touch movement may neither
    # release the guard nor arm a passive preview, and nothing may be read or fetched. The hold
    # happens with the finger still down — no touchend, no click — so what is asserted is the
    # unintended hover itself, not the gesture's outcome.
    reset_stub()
    row = [t["id"] for t in bc.StubHandler.state["state"]["topics"] if t["state"] != "picked"][10]
    open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.8)
    js(cdp, "try{localStorage.clear();localStorage.setItem('linuxdo-ai.density','compact');}catch(e){} true")
    open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.8)
    arm_errors(cdp)
    arm_inputs(cdp)
    drawer_arm(cdp)
    tapped_m = toggle_density(cdp, touch=True)          # a real tap: raises the guard
    mode_after_tap = density_mode(cdp)
    placed_m = placed_geometry(cdp, row, 400.0)         # the row the finger will cross
    time.sleep(0.3)
    point_m = item_point(cdp, f'.item[data-id="{row}"]')
    mark_m = req_mark()
    # the page-side record of exactly what the gesture delivers: kind, pointerType, coordinates,
    # page-clock timing and whether any click ever arrived
    js(cdp, """(function(){ window.__u07m = {events: [], clicks: 0};
        ['pointermove', 'mousemove', 'mouseover', 'pointerdown', 'touchstart', 'touchmove',
         'touchend', 'click'].forEach(function(kind){
            document.addEventListener(kind, function(ev){
                var it = (ev.target && ev.target.closest) ? ev.target.closest('.item') : null;
                if (kind === 'click') window.__u07m.clicks++;
                window.__u07m.events.push({kind: kind,
                    ptype: (ev.pointerType === undefined ? null : ev.pointerType),
                    x: Math.round(ev.clientX || 0), y: Math.round(ev.clientY || 0),
                    t: Math.round(performance.now()),
                    item: it ? Number(it.dataset.id) : null});
            }, {capture: true, passive: true});
        }); return true;})()""")
    x0, y0 = point_m.get("x", 0), point_m.get("y", 0)
    cdp.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x0, "y": y0}])
    time.sleep(0.05)
    for step in (1, 2, 3):                              # a few pixels, under the native pan slop
        cdp.call("Input.dispatchTouchEvent", type="touchMove",
                 touchPoints=[{"x": x0 + step, "y": y0 + 1}])
        time.sleep(0.04)
    moved_m = inputs(cdp)
    time.sleep(0.4)                                     # far past CFG.hoverMs, finger still down
    held_m = drawer_state(cdp)
    held_log_m = drawer_log(cdp)
    held_reqs_m = reqs_since(mark_m)
    # everything read here is the HOLD WINDOW: finger still down, no touchend and no click yet
    held_obs = js(cdp, "window.__u07m") or {}
    held_events = held_obs.get("events") or []
    held_clicks = held_obs.get("clicks") or 0
    cdp.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])   # finish the gesture
    time.sleep(0.35)
    after_m = drawer_state(cdp)
    post_obs = js(cdp, "window.__u07m") or {}
    post_clicks = post_obs.get("clicks") or 0
    moves_m = [e for e in held_events if e.get("kind") in ("pointermove", "mousemove")]
    move_types = sorted({str(e.get("ptype")) for e in moves_m})
    touch_moves = [e for e in moves_m if e.get("ptype") == "touch"]
    # a preview that DID open must have opened before the gesture's compatibility click, which is
    # what makes it an unintended hover rather than an activation
    open_at = (held_log_m[0].get("at") if held_log_m else None)
    click_t = min([e["t"] for e in (post_obs.get("events") or []) if e.get("kind") == "click"],
                  default=None)
    preceded = True if open_at is None else (click_t is None or open_at < click_t)
    c_tap = bool(tapped_m)
    c_mode = mode_after_tap == "gap"
    c_hit = bool(point_m.get("hits"))
    c_moves = bool(touch_moves)
    c_clean = held_m.get("open") is False and not held_log_m
    c_norq = not posts(held_reqs_m) and not detail_get(held_reqs_m)
    c_noclick = held_clicks == 0
    c_ok = not errors(cdp)
    check(c_tap and c_mode and c_hit and c_moves and c_clean and preceded and c_norq and
          c_noclick and c_ok,
          "D8 mobile: a finger dragged across a row is not a hover (no unintended preview)",
          f"tap={c_tap} mode={c_mode} hit={c_hit} touch_moves={touch_moves[:3]} "
          f"move_types={move_types} no_click_during_hold={c_noclick} held={held_m} "
          f"drawer={held_log_m} no_read={c_norq} clean={c_clean} "
          f"open_at={open_at} first_click_t={click_t} preceded_click={preceded} "
          f"clicks_after_release={post_clicks} after_release={after_m} errors={errors(cdp)}")

    # ...and a deliberate tap on that row still opens the preview (and a tap pins it). Whatever the
    # gesture's own compatibility click left open is closed first, so the tap really lands on the row.
    if drawer_state(cdp).get("open"):
        real_tap(cdp, ".drawer-scrim")                     # the real mobile dismissal path
        wait_js(cdp, "document.querySelector('#drawer').hidden === true", timeout=4.0)
        time.sleep(0.35)
    tap_pt_m = item_point(cdp, f'.item[data-id="{row}"]')
    closed_before = drawer_state(cdp).get("hidden") is True
    hit_m = js(cdp, """(function(){var el=document.elementFromPoint(%d,%d);
        var it = el && el.closest ? el.closest('.item') : null;
        return it ? Number(it.dataset.id) : null;})()""" % (tap_pt_m.get("x", 0), tap_pt_m.get("y", 0)))
    tap_ok_m, tap_at_m = real_tap(cdp, f'.item[data-id="{row}"]')
    opened_m = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    time.sleep(0.5)
    pinned_m = drawer_state(cdp)
    expected_m = "" if hit_m is None else "U07 密度回归条目 %02d" % (hit_m - 9200000)
    check(closed_before and hit_m is not None and bool(tap_ok_m) and bool(opened_m) and
          pinned_m.get("open") is True and pinned_m.get("title") == expected_m and not errors(cdp),
          "D9 mobile: a deliberate tap on the row still opens and pins the preview",
          f"closed_before={closed_before} tap={tap_at_m} hit={hit_m} opened={bool(opened_m)} "
          f"pinned={pinned_m} expected={expected_m!r}")
    close_preview(cdp)

    # The vanished pair is a question for ITS OWN lane only. Here the deep 收藏 bookmark is removed
    # while the header is far off screen, and a REAL scroll then settles mid-page — inside the
    # sampler's window, exactly where the old sampler adopted a visible 全部 article. The reader
    # then returns to the header and toggles: with the pair retained as a lane-3 question the
    # toggle forces no scroll; with a cross-lane adoption it restores that other article.
    reset_stub(48, queue_picks=[9200045], head_picked=1)
    open_fresh(cdp, url, density="gap")
    ensure_owner(cdp)
    deep = 9200045
    placed_deep = place_pair(cdp, deep, 3, WANT_TOP)
    ok_deep, _ = real_click_pair(cdp, deep, 3)
    opened_deep = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    real_click(cdp, "#d-queue")
    gone_deep = wait_js(cdp, "document.querySelectorAll('.item[data-id=\"9200045\"]').length === 1",
                        timeout=5.0)
    closed_deep = close_preview(cdp)
    header_off = js(cdp, "document.querySelector('#density').getBoundingClientRect().bottom < 0")
    press_key(cdp, "PageUp", code="PageUp", vk=33)   # a genuine scroll, settling mid-page
    time.sleep(0.8)
    mid = js(cdp, """({y: Math.round(window.scrollY),
        header: document.querySelector('#density').getBoundingClientRect().bottom > 0})""")
    mid_lane1 = topmost_lane_item(cdp, 1)
    js(cdp, "window.scrollTo(0, 0)")                 # the reader returns to the header
    time.sleep(0.5)
    lane3_items_d = js(cdp, "document.querySelectorAll('.cell--c3 > .item').length")
    lane3_on_screen_d = topmost_lane_item(cdp, 3)
    scroll_before_d = js(cdp, "Math.round(window.scrollY)")
    toggled_d = toggle_density(cdp, expect="compact")
    scroll_after_d = js(cdp, "Math.round(window.scrollY)")
    check(ok_deep and bool(opened_deep) and bool(gone_deep) and closed_deep and bool(header_off) and
          mid["header"] is False and mid_lane1 is not None and lane3_items_d == 0 and
          lane3_on_screen_d is None and toggled_d and scroll_after_d == scroll_before_d and
          not errors(cdp),
          "D7 a vanished deep 收藏 pair: a settled scroll en route never adopts another lane's article",
          f"placed={placed_deep} mid={mid} mid_lane1={mid_lane1} lane3_items={lane3_items_d} "
          f"lane3_on_screen={lane3_on_screen_d} scrollY {scroll_before_d}->{scroll_after_d} "
          f"errors={errors(cdp)}")


def phase_e(cdp, url: str) -> None:
    """A density reflow must not synthesize a passive hover under a stationary pointer."""
    print("--- phase E: a density reflow synthesizes no hover ---")
    state = reset_stub()
    ids = [t["id"] for t in state["state"]["topics"] if t["state"] != "picked"]

    open_fresh(cdp, url, density="compact")
    ensure_owner(cdp)
    # row 14: after the compensation restores it at ~220 px, the row that comes to rest under the
    # stationary cursor (~120 px higher, the 收藏 column at the control's x) is a 收藏 row — the
    # exact geometry of the reported A9 failure, where the drawer ended up covering #density.
    parked = 9200013
    placed = placed_geometry(cdp, parked, WANT_TOP)
    ok, _ = real_click(cdp, f'.item[data-id="{parked}"]')
    opened = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    closed = close_preview(cdp)
    check(ok and bool(opened) and closed,
          "E1 precondition: an explicit open parks the anchor and closes again (real 关闭 click)",
          f"placed={placed} opened={bool(opened)} closed={closed}")

    # arm the trace: pointer coordinates, hover events and the moment the drawer shows
    arm_inputs(cdp)
    trace_arm(cdp)
    drawer_arm(cdp)
    mark = req_mark()
    reads = read_marks(cdp)
    before = density_state(cdp)
    t0 = time.time()
    toggled = toggle_density(cdp, expect="gap", settle=0.2)
    click_ms = int((time.time() - t0) * 1000)
    time.sleep(1.4)                       # far past CFG.hoverMs (250 ms), pointer untouched
    trace = trace_end(cdp)
    drawer = drawer_log(cdp)
    after = density_state(cdp)
    hover = [e for e in trace if e.get("kind") in ("mouseover", "mousemove")]
    synthetic = [e for e in hover if e.get("mx") in (0, None) and e.get("my") in (0, None)]
    under = js(cdp, """(function(){var el=document.elementFromPoint(1136, 97);
        var it = el && el.closest ? el.closest('.item') : null;
        return {at: String((el && (el.className || el.tagName)) || '').slice(0, 30),
                item: it ? Number(it.dataset.id) : null, scrollY: Math.round(window.scrollY)};})()""")
    print(f"      (trace: {len(trace)} events, {len(synthetic)} with no pointer movement; "
          f"under the parked cursor: {under}; drawer={drawer}; click_ms={click_ms})")
    check(toggled and not drawer and not errors(cdp),
          "E2 with the cursor parked on the control, the reflow opens no preview",
          f"drawer={drawer} click_ms={click_ms} under={under} state={after} hover={hover[-6:]} "
          f"errors={errors(cdp)}")
    under_item = under.get("item") if under else None
    anchored = [e for e in hover if e.get("mx") == 0 and e.get("my") == 0 and
                e.get("item") is not None and e.get("item") == under_item]
    check(under_item is not None and bool(anchored),
          "E3 the trace shows a zero-movement hover on the article under the stationary cursor "
          "(the mechanism under test)",
          f"under={under} anchored={anchored[-4:]} hover={hover[-8:]}")

    # a genuine move onto a row resumes the normal passive hover, with the existing delay
    close_preview(cdp, via="escape")      # start clean (the other real close path)
    time.sleep(0.35)
    point = visible_row_point(cdp, 1)
    ev_target = point.get("id")
    expected_title = "" if ev_target is None else "U07 密度回归条目 %02d" % (ev_target - 9200000)
    move_to(cdp, point.get("x", 0), point.get("y", 0))
    time.sleep(0.12)
    early = drawer_state(cdp)
    time.sleep(0.45)
    hovered = drawer_state(cdp)
    check(bool(point.get("hits")) and ev_target is not None and hovered.get("open") is True and
          not early.get("open") and hovered.get("title") == expected_title,
          "E4 a real move onto a row resumes the passive hover (nothing before CFG.hoverMs, open after)",
          f"point={point} expected={expected_title!r} early={early} hovered={hovered}")

    # ...and it is still a transient preview: leaving closes it
    park_mouse(cdp)
    time.sleep(0.7)
    left = drawer_state(cdp)
    check(left.get("hidden") is True, "E5 the resumed hover is still a transient preview (leaving closes it)",
          f"left={left}")

    # an explicit activation is never gated by the suppression: a click right after a toggle pins
    exp_target = ids[24]
    toggled_b = toggle_density(cdp, expect="compact")
    js(cdp, "window.scrollTo(0, 0)")
    time.sleep(0.3)
    placed_exp = placed_geometry(cdp, exp_target, WANT_TOP)
    mark_b = req_mark()
    ok2, point2 = real_click(cdp, f'.item[data-id="{exp_target}"]')
    opened2 = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    park_mouse(cdp)                       # the pointer leaves: a pinned preview stays
    time.sleep(0.6)
    still = drawer_state(cdp)
    during = reqs_since(mark_b)
    check(toggled_b and ok2 and bool(opened2) and still.get("open") is True and
          still.get("title") and not posts(during) and not detail_get(during),
          "E6 an explicit click right after a toggle still pins the preview (and writes nothing)",
          f"placed={placed_exp} click={point2} opened={bool(opened2)} still={still} "
          f"posts={posts(during)} detail={detail_get(during)}")

    # R1: a density render with NO remembered pair (the vanished pair, its lane empty) reflows just
    # the same — rows move under a cursor that never moved. The reader rests the pointer on a row
    # and activates the control from the keyboard, so nothing about the pointer changes.
    reset_stub(48, queue_picks=[9200000], head_picked=1)
    open_fresh(cdp, url, density="compact")
    ensure_owner(cdp)
    placed7 = place_pair(cdp, 9200000, 3, WANT_TOP)
    ok7, _ = real_click_pair(cdp, 9200000, 3)
    opened7 = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    real_click(cdp, "#d-queue")
    gone7 = wait_js(cdp, "document.querySelectorAll('.item[data-id=\"9200000\"]').length === 1",
                    timeout=5.0)
    closed7 = close_preview(cdp)
    js(cdp, "window.scrollTo(0, 0)")
    time.sleep(0.45)
    lane3_items7 = js(cdp, "document.querySelectorAll('.cell--c3 > .item').length")
    row_point = visible_row_point(cdp, 1)
    row_id = row_point.get("id")
    focused = js(cdp, """(function(){var b=document.getElementById('density');
        b.focus({preventScroll:true}); return document.activeElement === b;})()""")
    arm_inputs(cdp)
    trace_arm(cdp)
    drawer_arm(cdp)
    t_move = time.time()
    move_to(cdp, row_point.get("x", 0), row_point.get("y", 0))
    press_key(cdp, "Enter", code="Enter", vk=13, text="\r")   # a real keyboard activation
    move_ms = int((time.time() - t_move) * 1000)
    time.sleep(1.3)                       # far past CFG.hoverMs, pointer untouched
    trace7 = trace_end(cdp)
    drawer7 = drawer_log(cdp)
    state7 = density_state(cdp)
    after7 = drawer_state(cdp)
    check(ok7 and bool(opened7) and bool(gone7) and closed7 and lane3_items7 == 0 and
          bool(focused) and bool(row_point.get("hits")) and row_id is not None and
          state7.get("pressed") == "false" and after7.get("open") is False and
          not drawer7 and not errors(cdp),
          "E7 a density render with NO remembered pair suppresses the layout hover too",
          f"placed={placed7} lane3_items={lane3_items7} row={row_point} move_to_key_ms={move_ms} "
          f"state={state7} after={after7} drawer={drawer7} trace={trace7[-4:]} "
          f"errors={errors(cdp)}")

    # ...and the reader's next real movement still resumes the normal passive hover
    next_point = visible_row_point(cdp, 1, skip=1)
    next_id = next_point.get("id")
    expected8 = "" if next_id is None else "U07 密度回归条目 %02d" % (next_id - 9200000)
    move_to(cdp, next_point.get("x", 0), next_point.get("y", 0))
    time.sleep(0.12)
    early8 = drawer_state(cdp)
    time.sleep(0.45)
    hovered8 = drawer_state(cdp)
    check(bool(next_point.get("hits")) and next_id is not None and not early8.get("open") and
          hovered8.get("open") is True and hovered8.get("title") == expected8,
          "E8 after the no-pair reflow a real move onto a row still resumes the passive hover",
          f"point={next_point} expected={expected8!r} early={early8} hovered={hovered8}")

    # Legacy capability: the engine cannot type its input, so a compatibility mouse event may be a
    # finger and no movement can be trusted to mean "a mouse is hovering". The declared fallback
    # after a reflow is click-only: passive hover stays blocked until an explicit activation. This
    # is the safe fallback, not the default modern interaction (that is E1-E8 above).
    reset_stub()
    open_fresh(cdp, url, density="compact")
    installed = cdp.call("Page.addScriptToEvaluateOnNewDocument", source=HIDE_POINTER_EVENT) or {}
    hide_id = installed.get("identifier")
    open_page(cdp, url, width=1280, height=900, mobile=False, touch=False, settle_s=1.8)
    legacy = js(cdp, "typeof window.PointerEvent")
    arm_errors(cdp)
    arm_inputs(cdp)
    drawer_arm(cdp)
    park_mouse(cdp)
    target_l = [t["id"] for t in bc.StubHandler.state["state"]["topics"] if t["state"] != "picked"][12]
    toggled_l = toggle_density(cdp)                     # a real click: the reflow raises the guard
    placed_l = placed_geometry(cdp, target_l, 400.0)
    time.sleep(0.3)
    mark_l = req_mark()
    point_l = item_point(cdp, f'.item[data-id="{target_l}"]')
    move_to(cdp, point_l.get("x", 0) + 2, point_l.get("y", 0) + 1)   # genuine movement onto the row
    time.sleep(0.08)
    move_to(cdp, point_l.get("x", 0), point_l.get("y", 0))
    time.sleep(0.6)                                     # well past CFG.hoverMs
    held_l = drawer_state(cdp)
    held_log_l = drawer_log(cdp)
    held_reqs_l = reqs_since(mark_l)
    check(legacy == "undefined" and bool(toggled_l) and bool(point_l.get("hits")) and
          held_l.get("open") is False and not held_log_l and
          not posts(held_reqs_l) and not detail_get(held_reqs_l) and not errors(cdp),
          "E9 legacy (no PointerEvent): a genuine move after a reflow does not resume the hover",
          f"typeof_PointerEvent={legacy!r} toggled={bool(toggled_l)} placed={placed_l} "
          f"point={point_l} held={held_l} drawer={held_log_l} posts={posts(held_reqs_l)} "
          f"detail={detail_get(held_reqs_l)} errors={errors(cdp)}")

    # ...and the explicit activation still works there: a real click opens and pins the preview
    if drawer_state(cdp).get("open"):
        close_preview(cdp, via="escape")
        time.sleep(0.35)
    click_ok_l, click_pt_l = real_click(cdp, f'.item[data-id="{target_l}"]')
    opened_l = wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
    time.sleep(0.5)
    pinned_l = drawer_state(cdp)
    expected_l = "U07 密度回归条目 %02d" % (target_l - 9200000)
    check(bool(click_ok_l) and bool(opened_l) and pinned_l.get("open") is True and
          pinned_l.get("title") == expected_l and not errors(cdp),
          "E10 legacy (no PointerEvent): an explicit click still opens and pins the preview",
          f"click={click_pt_l} opened={bool(opened_l)} pinned={pinned_l} expected={expected_l!r}")
    if drawer_state(cdp).get("open"):
        close_preview(cdp, via="escape")
        time.sleep(0.3)
    # the override is removed, and one fresh load leaves the document in its normal capability state
    if hide_id:
        cdp.call("Page.removeScriptToEvaluateOnNewDocument", identifier=hide_id)
    open_page(cdp, url, width=1280, height=900, mobile=False, touch=False, settle_s=1.5)
    restored = js(cdp, "typeof window.PointerEvent")
    print(f"      (legacy override removed; PointerEvent is {restored!r} again)")
    park_mouse(cdp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated phases, e.g. 'a,c' (diagnosis)")
    args = parser.parse_args()
    only = {p.strip().lower() for p in args.only.split(",") if p.strip()}

    if not Path(bc.CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    reset_stub()
    tmp = Path(tempfile.mkdtemp(prefix="density-position-"))
    profile = tmp / "profile"
    site = bc.build_site(tmp)
    httpd, port = bc.serve(site)
    url = f"http://127.0.0.1:{port}/index.html"
    devtools_port = bc._free_port()
    proc = None
    cdp = None
    try:
        proc = bc.launch_chromium(devtools_port, profile, "about:blank")
        cdp = bc.CDP(bc.page_ws(devtools_port))
        open_page(cdp, url, settle_s=1.6)
        ensure_owner(cdp)
        if not only or "a" in only:
            phase_a(cdp, url)
        if not only or "b" in only:
            phase_b(cdp, url)
        if not only or "c" in only:
            phase_c(cdp, url)
        if not only or "d" in only:
            phase_d(cdp, url)
        if not only or "e" in only:
            phase_e(cdp, url)
        if only:
            print("(focused run: --only " + args.only + ")")
        failed = [label for ok, label in RESULTS if not ok]
        print(f"--- {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed ---")
        if failed:
            print("failed: " + " | ".join(failed))
        return 0 if not failed else 1
    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        if args.keep:
            print(f"kept: {tmp}")
        else:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
