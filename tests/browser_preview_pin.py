#!/usr/bin/env python3
"""Preview focus/pin pointer regression — real CDP input events, stub API only.

The reported UX bug: a hover preview must disappear on mouseleave, but a CLICK must pin it
until the reader clicks elsewhere, closes it or presses Escape. The shipped handlers made the
pin a side effect of whatever open came last (`S.pinned = !!opts.pinned`), so the 250 ms hover
timer that was still pending after a click, a mouseout, a re-entry or a focus restoration each
silently unpinned (or replaced) the preview the reader had asked for.

Every check below drives the page with REAL input events — `Input.dispatchMouseEvent`,
`Input.dispatchKeyEvent`, `Input.dispatchTouchEvent` — because the bug lives in the event
handlers, not in the DOM: a JS `.click()` never fires `mouseover`/`mouseout`, so it cannot
reproduce it. All of it runs against the stdlib stub in browser_check.py (fixtures only):
nothing here touches the live service on :8791.

Every assertion is OBSERVABLE behaviour - there is no test-only probe in the app. "Pinned" is
proven the way the reader sees it: the pointer leaves (or another row is hovered / focused) and
the preview is still open on the same topic, while a transient hover preview closes.

Sections
  A. desktop 1280x900, real mode (owner token present, writes enabled so a stray write would
     be visible on the wire): hover transience, click pinning, hover over another row while
     pinned, explicit replacement, every column, outside dismissal, inside retention.
  B. keyboard: Tab/Enter/Space activation, and a passive focus that must not steal the pin.
  C. touch 390x844: a tap pins, the scrim dismisses, the next tap pins.
  D. no implicit write: no activation path records a bookmark, in any column.

Usage: python3 tests/browser_preview_pin.py [--keep]
Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import browser_check as bc  # noqa: E402  (CDP client + stub backend + pointer helpers)

OWNER_TOKEN = bc.OWNER_TOKEN
HOVER_MS = 250          # CFG.hoverMs in public/app.js
CLOSE_MS = 180          # CFG.closeMs

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""))
    return bool(ok)


def run(label: str, fn) -> bool:
    """Run one check body; an exception is a failure, never a crash."""
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - the harness must keep going
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return check(ok, label, detail)


# ---------------------------------------------------------------------- page utils


def js(cdp, expression):
    return cdp.evaluate(expression)


def open_page(cdp, url, *, width=1280, height=900, mobile=False, touch=False, settle_s=1.8):
    cdp.call("Runtime.enable")
    cdp.call("Page.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=2 if mobile else 1, mobile=mobile)
    cdp.call("Emulation.setTouchEmulationEnabled", enabled=bool(touch), maxTouchPoints=5)
    cdp.call("Page.navigate", url=url)
    time.sleep(settle_s)


def fresh_page(cdp, url, **kw):
    """Navigate, wipe storage, reload: a deterministic starting point (no bookmark, no read)."""
    open_page(cdp, url, **kw)
    cdp.evaluate("try{localStorage.clear();sessionStorage.clear();}catch(e){} true")
    cdp.call("Page.reload")
    time.sleep(float(kw.get("settle_s", 1.8)))


def wait_js(cdp, expression, timeout=5.0, interval=0.1):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return value


STATE_JS = """(function(){
  var cur = Array.from(document.querySelectorAll('#board .item.is-current'))
      .map(function(n){ return Number(n.dataset.id); });
  return {
    open: document.querySelector('#drawer').hidden === false,
    openId: cur.length ? cur[0] : null,
    current: cur,
    title: document.querySelector('#d-title').textContent
  };
})()"""


def state(cdp) -> dict:
    """What the reader sees: is the preview open, and which topic is marked current."""
    return js(cdp, STATE_JS) or {}


def read_marked(cdp, topic_id: int) -> int:
    return js(
        cdp,
        """(function(id){
             return Array.from(document.querySelectorAll('.item[data-id="'+id+'"]'))
               .filter(function(n){ return n.classList.contains('is-read'); }).length;
           })(%d)""" % topic_id,
    )


def posts(cdp, path: str) -> list:
    return [r for r in bc.StubHandler.requests if r["path"] == path and r["method"] == "POST"]


def all_posts() -> list:
    return [r for r in bc.StubHandler.requests if r["method"] == "POST"]


def detail_reads() -> list:
    return [r["path"] for r in bc.StubHandler.requests if r["path"].startswith("/api/topic/")]


def item_point(cdp, selector: str) -> dict:
    """A point that really belongs to the element (scrolled into view, verified hit-test)."""
    return js(
        cdp,
        """(function(){
             var n = document.querySelector(%s);
             if (!n) return {missing: true};
             n.scrollIntoView({block: 'center', inline: 'nearest'});
             var r = n.getBoundingClientRect();
             var x = Math.min(Math.max(Math.round(r.left + r.width / 2), 1), window.innerWidth - 2);
             var y = Math.min(Math.max(Math.round(r.top + r.height / 2), 1), window.innerHeight - 2);
             var el = document.elementFromPoint(x, y);
             return {x: x, y: y, hits: !!el && (el === n || n.contains(el)),
                     at: el ? String(el.className || el.tagName).slice(0, 40) : null};
           })()""" % json.dumps(selector),
    )


def move_to(cdp, x: int, y: int):
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, pointerType="mouse")


def park_mouse(cdp):
    """Rest the cursor off the board and off the drawer (4,4 is in the masthead)."""
    move_to(cdp, 4, 4)
    time.sleep(0.35)


def hover(cdp, selector: str) -> dict:
    """Move the real cursor onto the element — the only thing that fires mouseover/mouseout."""
    point = item_point(cdp, selector)
    if not point or point.get("missing") or not point.get("hits"):
        return point or {}
    move_to(cdp, point["x"], point["y"])
    return point


def pointer_click(cdp, selector: str) -> tuple[bool, dict]:
    """A real click: mouseMoved, mousePressed, mouseReleased at a point inside the element.

    Clicking right after the move is deliberate: it is the "fast click, before the 250 ms hover
    timer" case that used to leave a pending passive open behind the click."""
    point = hover(cdp, selector)
    if not point or point.get("missing") or not point.get("hits"):
        return False, point or {}
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", type=kind, x=point["x"], y=point["y"], button="left",
                 buttons=1 if kind == "mousePressed" else 0, clickCount=1, pointerType="mouse")
    time.sleep(0.15)
    return True, point


def click_at(cdp, point: dict) -> bool:
    if not point or point.get("missing"):
        return False
    move_to(cdp, point["x"], point["y"])
    time.sleep(0.05)
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", type=kind, x=point["x"], y=point["y"], button="left",
                 buttons=1 if kind == "mousePressed" else 0, clickCount=1, pointerType="mouse")
    time.sleep(0.15)
    return True


def blank_board_point(cdp) -> dict:
    """A real point on the board that belongs to no row: an empty slot left of the drawer.

    The desktop drawer is a fixed 400 px right panel; a point under it would hit the drawer
    instead of the board, so the scan skips that band and verifies the hit-test itself."""
    return js(
        cdp,
        """(function(){
             var limit = window.innerWidth - 430;
             var cells = Array.from(document.querySelectorAll('#board .cell--empty'));
             for (var i = 0; i < cells.length; i++) {
               var r = cells[i].getBoundingClientRect();
               if (r.width < 8 || r.height < 8) continue;
               var x = Math.round(r.left + r.width / 2);
               var y = Math.round(r.top + r.height / 2);
               if (x > limit || y < 2 || y > window.innerHeight - 2) continue;
               var el = document.elementFromPoint(x, y);
               if (!el || (el.closest && el.closest('.item'))) continue;
               return {x: x, y: y, at: String(el.className || el.tagName).slice(0, 40)};
             }
             return {missing: true};
           })()"""
    )


def key(cdp, k: str, code: str, vk: int, text: str = ""):
    cdp.call("Input.dispatchKeyEvent", type="rawKeyDown", key=k, code=code,
             windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    if text:
        cdp.call("Input.dispatchKeyEvent", type="char", key=k, code=code, text=text,
                 unmodifiedText=text, windowsVirtualKeyCode=vk)
    cdp.call("Input.dispatchKeyEvent", type="keyUp", key=k, code=code,
             windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)


def press_escape(cdp):
    key(cdp, "Escape", "Escape", 27)


def press_enter(cdp):
    key(cdp, "Enter", "Enter", 13, text="\r")


def press_space(cdp):
    key(cdp, " ", "Space", 32, text=" ")


def press_tab(cdp):
    cdp.call("Input.dispatchKeyEvent", type="rawKeyDown", key="Tab", code="Tab",
             windowsVirtualKeyCode=9, nativeVirtualKeyCode=9)
    cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab",
             windowsVirtualKeyCode=9, nativeVirtualKeyCode=9)
    time.sleep(0.06)


def tab_to_row(cdp, selector: str, limit: int = 120) -> bool:
    """Keyboard-reach the row: real Tab presses until it holds focus.

    Starts from a blurred document so the walk always begins at the first focusable (a row
    earlier in the DOM would otherwise need a wrap-around the browser does not do for us)."""
    js(cdp, "(function(){var a=document.activeElement;if(a&&a.blur)a.blur();return true;})()")
    time.sleep(0.15)
    for _ in range(limit):
        node = js(
            cdp,
            """(function(){
                 var a = document.activeElement;
                 var n = a && a.closest ? a.closest('.item') : null;
                 if (!n) return false;
                 return n === document.querySelector(%s);
               })()""" % json.dumps(selector),
        )
        if node:
            return True
        press_tab(cdp)
    return False


def touch_tap(cdp, selector: str) -> tuple[bool, dict]:
    point = item_point(cdp, selector)
    if not point or point.get("missing") or not point.get("hits"):
        return False, point or {}
    return tap_at(cdp, point), point


def tap_at(cdp, point: dict) -> bool:
    cdp.call("Input.dispatchTouchEvent", type="touchStart",
             touchPoints=[{"x": point["x"], "y": point["y"]}])
    time.sleep(0.05)
    cdp.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
    time.sleep(0.6)
    return True


def scrim_point(cdp) -> dict:
    """A real point on the mobile scrim.

    The sheet's height follows its content (max-height 80vh), so the strip that is still scrim
    moves: scan the viewport for a point where the hit-test really lands on #scrim."""
    return js(
        cdp,
        """(function(){
             var s = document.querySelector('#scrim');
             if (!s || s.hidden) return {missing: true};
             var r = s.getBoundingClientRect();
             var ys = [];
             for (var y = 4; y < r.height - 4; y += 7) ys.push(y);
             var xs = [Math.round(r.width / 2), Math.round(r.width / 4), Math.round(r.width / 8)];
             for (var i = 0; i < xs.length; i++) {
               for (var j = 0; j < ys.length; j++) {
                 var el = document.elementFromPoint(xs[i], ys[j]);
                 if (el === s) return {x: xs[i], y: ys[j], hits: true, at: 'scrim'};
               }
             }
             return {missing: true, rect: [Math.round(r.width), Math.round(r.height)]};
           })()"""
    )


def reset_preview(cdp) -> dict:
    """Escape back to a closed preview: every check that asserts transient behaviour starts here."""
    press_escape(cdp)
    time.sleep(0.9)
    return state(cdp)


def dismiss_scrim(cdp) -> dict:
    point = scrim_point(cdp)
    if point.get("missing") or not point.get("hits"):
        return point or {}
    tap_at(cdp, point)
    time.sleep(0.4)
    return point


def ensure_owner(cdp) -> bool:
    if js(cdp, "document.querySelector('#authchip').textContent") == "已连接 · 可写":
        return True
    js(cdp, "window.prompt = function(){ return %s; };" % json.dumps(OWNER_TOKEN))
    js(cdp, "document.querySelector('#owner').click()")
    return bool(wait_js(cdp, "document.querySelector('#authchip').textContent === '已连接 · 可写'", timeout=6.0))


def focus_is_row(cdp) -> bool:
    return bool(js(cdp, "(function(){var a=document.activeElement;return !!(a&&a.closest&&a.closest('.item'));})()"))


# --------------------------------------------------------------------------- phases


def phase_a(cdp, url: str, fixtures: dict) -> None:
    col1 = fixtures["col1"][0]
    col1_other = fixtures["col1"][1]
    col2 = fixtures["col2"][0]

    def leaves_and_holds(cdp, topic_id: int) -> dict:
        """The observable proof of a pin: the pointer leaves and the preview is still there."""
        park_mouse(cdp)
        time.sleep((CLOSE_MS + 500) / 1000.0)
        return state(cdp)

    def a1():
        # A hover preview is transient: it opens on the hover and leaves with the pointer.
        bc.StubHandler.requests.clear()
        point = hover(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not point or not point.get("hits"):
            return False, f"could not hover the row: {point}"
        time.sleep((HOVER_MS + 400) / 1000.0)
        opened = state(cdp)
        reads = detail_reads()
        marked = read_marked(cdp, col1)
        after_leave = leaves_and_holds(cdp, col1)
        return (
            opened.get("open") and opened.get("openId") == col1
            and reads == [] and marked == 0 and not after_leave.get("open"),
            f"opened={opened} reads={reads} marked={marked} after_leave={after_leave}",
        )

    def a2():
        # A click pins: it must survive the hover timer that was still pending when it landed.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        clicked = state(cdp)
        time.sleep(0.8)                      # the pending hover timer would have fired by now
        after_timer = state(cdp)
        left = leaves_and_holds(cdp, col1)
        return (
            clicked.get("open") and clicked.get("openId") == col1
            and after_timer.get("open") and after_timer.get("openId") == col1
            and left.get("open") and left.get("openId") == col1,
            f"clicked={clicked} after_hover_timer={after_timer} after_leave={left}",
        )

    def a3():
        # The slow path (hover, wait past the delay, then click) stays stable too - and the
        # hover half really was transient (it closes when the pointer leaves).
        reset = reset_preview(cdp)
        if reset.get("open"):
            return False, f"could not get back to a closed preview: {reset}"
        click_row = f'.cell--c1 .item[data-id="{col1}"]'
        park_mouse(cdp)
        point = hover(cdp, click_row)
        if not point or not point.get("hits"):
            return False, f"could not hover the row: {point}"
        time.sleep((HOVER_MS + 350) / 1000.0)
        transient = state(cdp)
        transient_closed = leaves_and_holds(cdp, col1)
        point2 = hover(cdp, click_row)
        if not point2 or not point2.get("hits"):
            return False, f"could not re-hover the row: {point2}"
        time.sleep((HOVER_MS + 350) / 1000.0)
        if not click_at(cdp, point2):
            return False, "could not click the hovered row"
        time.sleep(0.2)
        clicked = state(cdp)
        left = leaves_and_holds(cdp, col1)
        return (
            transient.get("open") and transient.get("openId") == col1 and not transient_closed.get("open")
            and clicked.get("open") and clicked.get("openId") == col1
            and left.get("open") and left.get("openId") == col1,
            f"hover={transient} hover_after_leave={transient_closed} clicked={clicked} after_leave={left}",
        )

    def a4():
        # Leaving and re-entering the pinned row is a passive hover: it may not unpin it.
        click_row = f'.cell--c1 .item[data-id="{col1}"]'
        aimed, point = pointer_click(cdp, click_row)
        if not aimed:
            return False, f"could not click the row: {point}"
        still = leaves_and_holds(cdp, col1)
        again = hover(cdp, click_row)
        if not again or not again.get("hits"):
            return False, f"could not re-enter the row: {again}"
        time.sleep((HOVER_MS + 400) / 1000.0)
        reentered = state(cdp)
        left = leaves_and_holds(cdp, col1)
        return (
            still.get("open") and still.get("openId") == col1
            and reentered.get("open") and reentered.get("openId") == col1
            and left.get("open") and left.get("openId") == col1,
            f"after_leave={still} after_reenter={reentered} after_second_leave={left}",
        )

    def a5():
        # A passive hover over a DIFFERENT row may neither replace nor unpin the pinned preview.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        pinned = leaves_and_holds(cdp, col1)
        other = hover(cdp, f'.cell--c1 .item[data-id="{col1_other}"]')
        if not other or not other.get("hits"):
            return False, f"could not hover the other row: {other}"
        time.sleep((HOVER_MS + 500) / 1000.0)
        hovered = state(cdp)
        left = leaves_and_holds(cdp, col1)
        return (
            pinned.get("open") and pinned.get("openId") == col1
            and hovered.get("open") and hovered.get("openId") == col1
            and left.get("open") and left.get("openId") == col1,
            f"pinned={pinned} while_hovering_other={hovered} after_leave={left}",
        )

    def a6():
        # An explicit click on another row DOES replace the pinned topic — and stays pinned.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the first row: {point}"
        park_mouse(cdp)
        aimed2, point2 = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1_other}"]')
        if not aimed2:
            return False, f"could not click the second row: {point2}"
        replaced = state(cdp)
        left = leaves_and_holds(cdp, col1_other)
        return (
            replaced.get("open") and replaced.get("openId") == col1_other
            and left.get("open") and left.get("openId") == col1_other,
            f"after_second_click={replaced} after_leave={left}",
        )

    def a7():
        # Every column activates the same way: the row opens the preview, in 全部 and in 精选.
        if not js(cdp, 'document.querySelectorAll(\'.cell--c3 .item[data-id="%d"]\').length' % col2):
            # a col3 instance only exists once bookmarked, and the drawer button is the way
            bc.StubHandler.requests.clear()
            bc.StubHandler.state["queue"] = []
            open_row(cdp, col2)
            click_drawer_queue(cdp)
            wait_js(cdp, 'document.querySelectorAll(\'.cell--c3 .item[data-id="%d"]\').length === 1' % col2)
        bc.StubHandler.requests.clear()
        park_mouse(cdp)
        press_escape(cdp)                      # start from nothing on screen
        time.sleep(0.9)
        seen = []
        for label, selector in (("col1", f'.cell--c1 .item[data-id="{col1}"]'),
                                ("col2", f'.cell--c2 .item[data-id="{col2}"]'),
                                ("col3", f'.cell--c3 .item[data-id="{col2}"]')):
            aimed, point = pointer_click(cdp, selector)
            time.sleep(0.35)
            st = state(cdp)
            held = leaves_and_holds(cdp, st.get("openId") or 0)   # the pin proof for this column
            seen.append((label, aimed, st.get("open"), st.get("openId"),
                         held.get("open"), held.get("openId")))
            press_escape(cdp)                  # the open panel covers 收藏; clear it for the next
            time.sleep(0.9)
        queue_posts = posts(cdp, "/api/queue")
        ok = all(aimed and open_ and open_id is not None and held_open and held_id == open_id
                 for _, aimed, open_, open_id, held_open, held_id in seen)
        ok = ok and [open_id for _, _, _, open_id, _, _ in seen] == [col1, col2, col2]
        ok = ok and not state(cdp).get("open")
        return ok and queue_posts == [], f"seen={seen} writes={json.dumps([r['body'] for r in queue_posts])}"

    def a8():
        # A genuine click on blank board space dismisses a pinned preview.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        pinned = leaves_and_holds(cdp, col1)
        js(cdp, "window.scrollTo(0, 0); true")
        time.sleep(0.2)
        blank = blank_board_point(cdp)
        if blank.get("missing") or not click_at(cdp, blank):
            return False, f"could not click a blank board spot: {blank}"
        time.sleep(0.4)
        closed = state(cdp)
        return (
            pinned.get("open") and pinned.get("openId") == col1
            and not closed.get("open") and focus_is_row(cdp) is False,
            f"pinned={pinned} blank={blank} after_outside_click={closed} focus_stolen={focus_is_row(cdp)}",
        )

    def a9():
        # …and so does a click in the header.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        pinned = leaves_and_holds(cdp, col1)
        js(cdp, "window.scrollTo(0, 0); true")
        time.sleep(0.2)
        aimed_h, point_h = pointer_click(cdp, ".brand")
        if not aimed_h:
            return False, f"could not click the header: {point_h}"
        time.sleep(0.4)
        closed = state(cdp)
        return (
            pinned.get("open") and not closed.get("open"),
            f"pinned={pinned} header_point={point_h} after_header_click={closed}",
        )

    def a9b():
        # A header control both dismisses the preview and does its job. 刷新 / 紧凑 live in the
        # tools row the desktop drawer overlays (so a real pointer cannot reach them while the
        # preview is open - a layout fact); the app's own buttons are clicked here and the
        # contract is asserted: dismissal + the control's own effect.
        StubHandler = bc.StubHandler
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        pinned = leaves_and_holds(cdp, col1)
        StubHandler.requests.clear()
        js(cdp, "document.querySelector('#refresh').click()")
        time.sleep(0.5)
        after_refresh = state(cdp)
        refreshes = [r for r in StubHandler.requests if r["path"] == "/api/refresh" and r["method"] == "POST"]
        aimed2, point2 = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed2:
            return False, f"could not re-pin the row: {point2}"
        pinned2 = leaves_and_holds(cdp, col1)
        # 紧凑 sits in the same (drawer-covered) tools row, so it is clicked through the app's
        # own button: what is asserted is the contract - it dismisses AND its state flips.
        before_density = js(cdp, "document.querySelector('#density').getAttribute('aria-pressed')")
        js(cdp, "document.querySelector('#density').click()")
        time.sleep(0.4)
        after_density = state(cdp)
        density_state = js(cdp, "document.querySelector('#density').getAttribute('aria-pressed')")
        js(cdp, "document.querySelector('#density').click()")     # leave the mode as we found it
        time.sleep(0.3)
        return (
            pinned.get("open") and not after_refresh.get("open") and len(refreshes) == 1
            and pinned2.get("open") and not after_density.get("open")
            and before_density != density_state,
            f"pinned={pinned} after_refresh_click={after_refresh} refresh_posts={len(refreshes)} "
            f"pinned2={pinned2} after_density_click={after_density} "
            f"density={before_density!r}->{density_state!r} tab_point={point2}",
        )

    def a10():
        # Interactions INSIDE the preview retain the pin (the note field and the preview text).
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        time.sleep(0.3)
        aimed0, point0 = pointer_click(cdp, "#d-note")
        typed = js(cdp, "document.activeElement === document.querySelector('#d-note')")
        aimed2, point2 = pointer_click(cdp, "#d-read")
        time.sleep(0.3)
        after_read = state(cdp)
        aimed3, point3 = pointer_click(cdp, "#d-title")
        time.sleep(0.3)
        after_title = state(cdp)
        held = leaves_and_holds(cdp, col1)
        return (
            aimed0 and typed is True and aimed2 and aimed3
            and after_read.get("open") and after_read.get("openId") == col1
            and after_title.get("open") and after_title.get("openId") == col1
            and held.get("open") and held.get("openId") == col1,
            f"note_point={point0} note_focused={typed} after_read_click={after_read} "
            f"after_title_click={after_title} after_leave={held}",
        )

    def a11():
        # 收藏 lives on the drawer button only, and it keeps the pinned preview open.
        bc.StubHandler.state["queue"] = []
        bc.StubHandler.requests.clear()
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        before = posts(cdp, "/api/queue")
        label_before = js(cdp, "document.querySelector('#d-queue').textContent")
        aimed2, point2 = pointer_click(cdp, "#d-queue")
        time.sleep(1.2)
        adds = [r["body"] for r in posts(cdp, "/api/queue")]
        after_click = state(cdp)
        label_after = js(cdp, "document.querySelector('#d-queue').textContent")
        aimed3, point3 = pointer_click(cdp, "#d-queue")
        time.sleep(1.2)
        removes = [r["body"] for r in posts(cdp, "/api/queue") if isinstance(r["body"], dict) and "remove" in r["body"]]
        held = leaves_and_holds(cdp, col1)
        return (
            aimed2 and aimed3 and before == [] and adds == [{"add": col1}]
            and removes == [{"remove": col1}] and label_before == "收藏" and label_after == "取消收藏"
            and after_click.get("open") and after_click.get("openId") == col1
            and held.get("open") and held.get("openId") == col1,
            f"row_click_writes={json.dumps([r['body'] for r in before])} adds={json.dumps(adds)} "
            f"removes={json.dumps(removes)} label={label_before!r}->{label_after!r} state={after_click}",
        )

    def a12():
        # Escape dismisses a pinned preview, and the focus handed back to the row must not
        # immediately re-open it through the keyboard-focus preview path.
        click_row = f'.cell--c1 .item[data-id="{col1}"]'
        aimed, point = pointer_click(cdp, click_row)
        if not aimed:
            return False, f"could not click the row: {point}"
        js(cdp, "document.activeElement.blur(); true")     # the row must not hold the focus
        park_mouse(cdp)
        time.sleep(0.3)
        pinned = state(cdp)
        press_escape(cdp)
        time.sleep(0.6)
        closed = state(cdp)
        time.sleep(0.8)                      # a late focus/timer re-open would land here
        settled = state(cdp)
        # …the same with the focus FIRST inside a preview control: the focus returns to the row
        # (asserted) and that restoration must not be read as a fresh keyboard preview.
        aimed2, point2 = pointer_click(cdp, click_row)
        if not aimed2:
            return False, f"could not re-click the row: {point2}"
        park_mouse(cdp)
        pointer_click(cdp, "#d-note")
        time.sleep(0.2)
        focus_inside = js(cdp, "document.activeElement === document.querySelector('#d-note')")
        press_escape(cdp)
        time.sleep(1.2)
        after_focus_escape = state(cdp)
        restored = focus_is_row(cdp)
        # …and with a TRANSIENT preview, where the row itself never held focus.
        park_mouse(cdp)
        hover_point = hover(cdp, f'.cell--c1 .item[data-id="{col1_other}"]')
        if not hover_point or not hover_point.get("hits"):
            return False, f"could not hover the other row: {hover_point}"
        time.sleep((HOVER_MS + 400) / 1000.0)
        transient = state(cdp)
        press_escape(cdp)
        time.sleep(1.2)
        after_hover_escape = state(cdp)
        park_mouse(cdp)
        time.sleep((CLOSE_MS + 400) / 1000.0)
        idle = state(cdp)
        return (
            pinned.get("open") and pinned.get("openId") == col1
            and not closed.get("open") and not settled.get("open")
            and focus_inside is True and not after_focus_escape.get("open") and restored is True
            and transient.get("open") and transient.get("openId") == col1_other
            and not after_hover_escape.get("open") and not idle.get("open"),
            f"pinned={pinned} after_escape={closed} settled={settled} focus_inside={focus_inside} "
            f"after_focus_escape={after_focus_escape} focus_restored={restored} transient={transient} "
            f"after_hover_escape={after_hover_escape}",
        )

    def a13():
        # After Escape the page is not stuck: an explicit open still works and still pins.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1_other}"]')
        if not aimed:
            return False, f"could not click the row after Escape: {point}"
        pinned = state(cdp)
        left = leaves_and_holds(cdp, col1_other)
        return (
            pinned.get("open") and pinned.get("openId") == col1_other
            and left.get("open") and left.get("openId") == col1_other,
            f"after_escape_reopen={pinned} after_leave={left}",
        )

    def a14():
        # The close control behaves like Escape: closes, hands the focus back to the row
        # (with the focus inside the preview when it is clicked) and does not re-open.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        park_mouse(cdp)
        time.sleep(0.3)
        aimed2, point2 = pointer_click(cdp, "#d-close")
        time.sleep(0.5)
        closed = state(cdp)
        time.sleep(0.8)
        settled = state(cdp)
        restored = focus_is_row(cdp)
        return (
            aimed2 and not closed.get("open") and not settled.get("open") and restored is True,
            f"after_close={closed} settled={settled} focus_restored_to_row={restored}",
        )

    def a15():
        # A passive preview never reads or fetches; the explicit open still does both (the
        # explicit-only read/body rules are unchanged by the pin work).
        bc.StubHandler.state["queue"] = []
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token after the page reset"
        bc.StubHandler.requests.clear()
        park_mouse(cdp)
        selector = f'.cell--c1 .item[data-id="{col1}"]'
        hover_point = hover(cdp, selector)
        if not hover_point or not hover_point.get("hits"):
            return False, f"could not hover the row: {hover_point}"
        time.sleep((HOVER_MS + 450) / 1000.0)
        transient = state(cdp)
        transient_reads = detail_reads()
        marked_hover = read_marked(cdp, col1)
        aimed, point = pointer_click(cdp, selector)
        time.sleep(0.9)
        marked_click = read_marked(cdp, col1)
        reads = [p for p in detail_reads() if p.endswith("/%d" % col1)]
        return (
            transient.get("open") and transient.get("openId") == col1
            and transient_reads == [] and marked_hover == 0
            and aimed and marked_click >= 1 and len(reads) == 1,
            f"transient={transient} reads_on_hover={transient_reads} marked_on_hover={marked_hover} "
            f"marked_after_click={marked_click} reads={reads}",
        )

    run("A1  a hover preview opens transiently and closes when the pointer leaves", a1)
    run("A2  a click inside the hover delay pins the preview, and leaving keeps it", a2)
    run("A3  hover → wait past the delay → click → leave keeps the pinned preview", a3)
    run("A4  leaving and re-entering the pinned row neither unpins nor replaces it", a4)
    run("A5  a passive hover over another row cannot replace or unpin the pinned preview", a5)
    run("A6  an explicit click on another row replaces the pinned topic", a6)
    run("A7  all three columns open (and hold) the preview from a row click, no bookmark write", a7)
    run("A8  a click on blank board space dismisses the pinned preview (no focus steal)", a8)
    run("A9  a click in the header dismisses the pinned preview", a9)
    run("A9b a header control (刷新 / tabs) dismisses the preview and still does its job", a9b)
    run("A10 interactions inside the preview retain the pin", a10)
    run("A11 收藏 is the drawer button's job and it keeps the preview open", a11)
    run("A12 Escape closes the pinned preview and it does not reopen (focus first inside)", a12)
    run("A13 after Escape an explicit open still pins", a13)
    run("A14 the close control closes, restores focus to the row, and does not reopen", a14)
    run("A15 an explicit open still reads; a passive hover never does", a15)


def open_row(cdp, topic_id: int):
    return pointer_click(cdp, f'.item[data-id="{topic_id}"]')


def click_drawer_queue(cdp):
    return pointer_click(cdp, "#d-queue")


def phase_b(cdp, url: str, fixtures: dict) -> None:
    col1 = fixtures["col1"][0]
    col2 = fixtures["col2"][0]

    def b1():
        # Keyboard activation: Tab to the 精选 row, Enter opens AND pins it — and it must not
        # toggle the bookmark (the old handler queued the picked column on Enter).
        bc.StubHandler.state["queue"] = []
        bc.StubHandler.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token"
        park_mouse(cdp)
        selector = f'.cell--c2 .item[data-id="{col2}"]'
        if not tab_to_row(cdp, selector):
            return False, "never reached the 精选 row with Tab"
        pressed = state(cdp)
        press_enter(cdp)
        time.sleep(0.5)
        after_enter = state(cdp)
        queue_posts = posts(cdp, "/api/queue")
        park_mouse(cdp)
        time.sleep((CLOSE_MS + 500) / 1000.0)
        left = state(cdp)
        return (
            after_enter.get("open") and after_enter.get("openId") == col2
            and left.get("open") and left.get("openId") == col2
            and queue_posts == [],
            f"focus_preview={pressed} after_enter={after_enter} after_leave={left} "
            f"writes={json.dumps([r['body'] for r in queue_posts])}",
        )

    def b2():
        # Space on a 全部 row does the same.
        bc.StubHandler.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token"
        park_mouse(cdp)
        selector = f'.cell--c1 .item[data-id="{col1}"]'
        if not tab_to_row(cdp, selector):
            return False, "never reached the 全部 row with Tab"
        press_space(cdp)
        time.sleep(0.5)
        after_space = state(cdp)
        park_mouse(cdp)
        time.sleep((CLOSE_MS + 500) / 1000.0)
        left = state(cdp)
        return (
            after_space.get("open") and after_space.get("openId") == col1
            and left.get("open") and left.get("openId") == col1
            and posts(cdp, "/api/queue") == [],
            f"after_space={after_space} after_leave={left}",
        )

    def b3():
        # Keyboard focus is a passive preview: it may not steal a pin either.
        aimed, point = pointer_click(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not aimed:
            return False, f"could not click the row: {point}"
        park_mouse(cdp)
        time.sleep(0.3)
        pinned = state(cdp)
        js(cdp, "document.querySelector(%s).focus()" % json.dumps(f'.cell--c1 .item[data-id="{col1_other}"]'))
        press_tab(cdp)
        time.sleep(0.4)
        after_focus = state(cdp)
        park_mouse(cdp)
        time.sleep((CLOSE_MS + 500) / 1000.0)
        left = state(cdp)
        return (
            pinned.get("open") and pinned.get("openId") == col1
            and after_focus.get("openId") == col1 and left.get("openId") == col1 and left.get("open"),
            f"pinned={pinned} after_keyboard_focus_move={after_focus} after_leave={left}",
        )

    col1_other = fixtures["col1"][1]
    run("B1  Tab+Enter on the 精选 row opens and pins the preview (no bookmark write)", b1)
    run("B2  Tab+Space on a 全部 row opens and pins the preview", b2)
    run("B3  passive keyboard focus cannot replace a pinned preview", b3)


def phase_c(cdp, url: str, fixtures: dict) -> None:
    col1 = fixtures["col1"][0]
    col1_other = fixtures["col1"][1]

    def c1():
        # Touch: a tap pins (there is no hover to leave, so the sheet stays). The mobile sheet
        # covers the list, so dismissing goes through the scrim — and the next tap pins again.
        bc.StubHandler.requests.clear()
        ok, point = touch_tap(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not ok:
            return False, f"could not tap the row: {point}"
        first = state(cdp)
        time.sleep(0.8)
        still = state(cdp)                     # nothing closes a pinned sheet by itself
        scrim = dismiss_scrim(cdp)
        if scrim.get("missing") or not scrim.get("hits"):
            return False, f"no scrim to tap: {scrim}"
        closed = state(cdp)
        ok2, point2 = touch_tap(cdp, f'.cell--c1 .item[data-id="{col1_other}"]')
        if not ok2:
            return False, f"could not tap the second row: {point2}"
        second = state(cdp)
        writes = [r["body"] for r in all_posts()]
        return (
            first.get("open") and first.get("openId") == col1
            and still.get("open") and still.get("openId") == col1
            and not closed.get("open")
            and second.get("open") and second.get("openId") == col1_other
            and writes == [],
            f"tap1={first} still={still} scrim={scrim} after_scrim={closed} tap2={second} "
            f"writes={json.dumps(writes)}",
        )

    run("C1  touch: a tap pins, nothing closes it, the scrim dismisses, the next tap pins", c1)


def phase_d(cdp, url: str, fixtures: dict) -> None:
    col1 = fixtures["col1"][0]
    col1_other = fixtures["col1"][1]
    col2 = fixtures["col2"][0]

    def d1():
        # No activation path may write: not a row click in any column, not Enter, not a tap.
        bc.StubHandler.state["queue"] = []
        bc.StubHandler.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token"
        park_mouse(cdp)
        for selector in (f'.cell--c1 .item[data-id="{col1}"]',
                         f'.cell--c2 .item[data-id="{col2}"]',
                         f'.cell--c1 .item[data-id="{col1_other}"]'):
            aimed, point = pointer_click(cdp, selector)
            if not aimed:
                return False, f"could not click {selector}: {point}"
            park_mouse(cdp)
            time.sleep(0.25)
        selector = f'.cell--c2 .item[data-id="{col2}"]'
        if not tab_to_row(cdp, selector):
            return False, "never reached the 精选 row with Tab"
        press_enter(cdp)
        time.sleep(0.4)
        writes = [r["body"] for r in all_posts()]
        stored = js(cdp, "JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
        counts = js(cdp, "document.querySelector('#c-queue').textContent")
        return writes == [] and stored == [] and counts == "0", (
            f"writes={json.dumps(writes)} stored={stored} queue_count={counts}"
        )

    def d2():
        # The same on a touch viewport: the 精选 row (its own tab) and a 全部 row. The sheet
        # covers the list, so the two taps are separated by the scrim.
        bc.StubHandler.state["queue"] = []
        bc.StubHandler.requests.clear()
        fresh_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token"
        ok0, point0 = touch_tap(cdp, "#tab-picked")
        time.sleep(0.5)
        ok1, point1 = touch_tap(cdp, f'.cell--c2 .item[data-id="{col2}"]')
        if not ok1:
            return False, f"could not tap the 精选 row: {point1}"
        opened = state(cdp)
        scrim = dismiss_scrim(cdp)
        if scrim.get("missing") or not scrim.get("hits"):
            return False, f"no scrim to tap: {scrim}"
        touch_tap(cdp, "#tab-all")
        time.sleep(0.5)
        ok2, point2 = touch_tap(cdp, f'.cell--c1 .item[data-id="{col1}"]')
        if not ok2:
            return False, f"could not tap the 全部 row: {point2}"
        writes = [r["body"] for r in all_posts()]
        stored = js(cdp, "JSON.parse(localStorage.getItem('linuxdo-ai.queue')||'[]')")
        return (
            ok0 and opened.get("open") and opened.get("openId") == col2
            and writes == [] and stored == [],
            f"tab_tap={point0} opened={opened} writes={json.dumps(writes)} stored={stored}",
        )

    run("D1  desktop activation never writes a bookmark (clicks + keyboard)", d1)
    run("D2  touch activation never writes a bookmark", d2)


def fixture_facts() -> dict:
    data = json.loads((PUBLIC / "fixtures" / "state.sample.json").read_text(encoding="utf-8"))
    topics = data["topics"]
    col1 = [t["id"] for t in topics if t["state"] != "picked" and (t.get("body_text") or "").strip()]
    col2 = [t["id"] for t in topics if t["state"] == "picked"]
    if len(col1) < 2 or not col2:
        raise SystemExit("fixture precondition: need two 全部 topics with bodies and one 精选 topic")
    return {"col1": col1, "col2": col2}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if not Path(bc.CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    fixtures = fixture_facts()
    tmp = Path(tempfile.mkdtemp(prefix="preview-pin-"))
    profile = tmp / "profile"
    site = bc.build_site(tmp)
    bc.StubHandler.state = {
        "state": json.loads((PUBLIC / "fixtures" / "state.sample.json").read_text(encoding="utf-8")),
        "health": {"status": "ok", "attention": {"needed": False, "reason": "", "kind": None},
                   "auth": {"writes": "owner", "token_configured": True}},
        "queue": [],
        "feedback": [],
        "fail_vote": False,
        "refreshes": 0,
        "state_status": 200,
        "state_delay": 0.0,
    }
    bc.StubHandler.requests = []
    httpd, port = bc.serve(site)
    base = f"http://127.0.0.1:{port}"
    url = f"{base}/index.html"
    devtools_port = bc._free_port()
    proc = None
    cdp = None
    try:
        proc = bc.launch_chromium(devtools_port, profile, "about:blank")
        cdp = bc.CDP(bc.page_ws(devtools_port))

        open_page(cdp, url, width=1280, height=900, settle_s=2.0)
        if not ensure_owner(cdp):
            check(False, "precondition: the stub accepts the owner token")
            return 1
        js(cdp, "try{localStorage.clear();}catch(e){} true")

        print(f"--- phase A: mouse pin semantics, real mode 1280x900 at {url} ---")
        open_page(cdp, url, width=1280, height=900, settle_s=1.2)
        phase_a(cdp, url, fixtures)
        print("--- phase B: keyboard activation 1280x900 ---")
        phase_b(cdp, url, fixtures)
        print("--- phase C: touch 390x844 ---")
        open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.5)
        phase_c(cdp, url, fixtures)
        print("--- phase D: no implicit write ---")
        phase_d(cdp, url, fixtures)
        return 0 if all(ok for ok, _ in RESULTS) else 1
    finally:
        if cdp:
            cdp.close()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        httpd.shutdown()
        httpd.server_close()
        if args.keep:
            print("keeping evidence in", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    code = main()
    total = len(RESULTS)
    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{total - len(failed)}/{total} checks passed")
    if failed:
        print("failed: " + "; ".join(failed))
    raise SystemExit(code)
