#!/usr/bin/env python3
"""Isolated browser checks for the authorized reader UI: 收藏 (bookmarks) + read state.

Runs against the Chromium already in the sandbox over the DevTools protocol (stdlib
only, no installs). A throwaway copy of `public/` plus a *stub* API is served on a
random loopback port — nothing touches the live service on :8791, no real vote is
cast and no token leaves the page.

Sections
  A. fixture mode, 1440x900: settled copy, bookmark membership (rejected + picked),
     read semantics (explicit preview vs hover vs no-body vs original link),
     duplicated instances, drawer read toggle, reload persistence, storage namespace,
     malformed/bounded/blocked storage, list order stability.
  B. fixture mode, 390x844 touch: tab membership without duplicates, tap-to-open
     marks read, single-line header/tabs.
  C. real mode against the stub API: auth geometry, bookmark wire shapes, vote
     decoupling, stale-refresh keeps content, truthfully distinguished failures,
     bounded read deadline, in-flight dedupe, no retry storm.
  D. widths 360/390/1440 in light+dark: no odd wrap, no overflow, screenshots.

Usage: python3 tests/browser_reading.py [--shots DIR] [--keep]
Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import browser_check as bc  # noqa: E402  (CDP client + stub backend + helpers)

# one token, defined by the stub backend: a second hard-coded value would be rejected (401)
OWNER_TOKEN = bc.OWNER_TOKEN
DEFAULT_SHOTS = Path("/tmp/linuxdo-reading-ui-evidence")
SUBTITLE = "浏览新帖，发现值得读的内容。"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


def run(label: str, fn) -> bool:
    """Run one check body; an exception is a failure, never a crash."""
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - the harness must keep going
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return check(ok, label, detail)


# --------------------------------------------------------------------- page utils


class ReadingStub:
    """Marker only: the stub backend lives in browser_check.StubHandler (state_status /
    state_delay keys drive its injectable failure modes)."""


def build_site(tmp: Path, api_base: str) -> Path:
    """The bundle under test, plus an `alt/` copy carrying an explicit apiBase."""
    site = tmp / "site"
    site.mkdir(parents=True)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copy2(PUBLIC / name, site / name)
    (site / "runtime-config.js").write_text(
        "window.LINUXDO_AI_RUNTIME = " + json.dumps({"apiBase": api_base}) + ";\n", encoding="utf-8"
    )
    (site / "fixtures").mkdir()
    for name in ("state.sample.json", "state.sample.next.json"):
        source = PUBLIC / "fixtures" / name
        if source.is_file():
            shutil.copy2(source, site / "fixtures" / name)
    return site


def write_alt_build(site: Path, api_base: str) -> None:
    alt = site / "alt"
    alt.mkdir(exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copy2(PUBLIC / name, alt / name)
    (alt / "runtime-config.js").write_text(
        "window.LINUXDO_AI_RUNTIME = " + json.dumps({"apiBase": api_base}) + ";\n", encoding="utf-8"
    )
    (alt / "fixtures").mkdir(exist_ok=True)
    for name in ("state.sample.json", "state.sample.next.json"):
        source = PUBLIC / "fixtures" / name
        if source.is_file():
            shutil.copy2(source, alt / "fixtures" / name)


def open_page(cdp, url, *, width, height, mobile=False, touch=False, dark=False, settle_s=1.8):
    cdp.call("Runtime.enable")
    cdp.call("Page.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=2 if mobile else 1, mobile=mobile)
    cdp.call("Emulation.setTouchEmulationEnabled", enabled=bool(touch), maxTouchPoints=5)
    cdp.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])
    cdp.call("Page.navigate", url=url)
    time.sleep(settle_s)


def fresh_page(cdp, url, **kw):
    """Navigate, wipe browser storage, reload: a deterministic starting point."""
    open_page(cdp, url, **kw)
    cdp.evaluate("try{localStorage.clear();sessionStorage.clear();}catch(e){} true")
    cdp.call("Page.reload")
    time.sleep(float(kw.get("settle_s", 1.8)))


def js(cdp, expression):
    return cdp.evaluate(expression)


def wait_js(cdp, expression, timeout=6.0, interval=0.15):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return value


def count(cdp, selector: str) -> int:
    return cdp.evaluate("document.querySelectorAll(%s).length" % json.dumps(selector))


def visible_items(cdp):
    return cdp.evaluate(
        """(function(){
             return Array.from(document.querySelectorAll('#board .item')).filter(function(n){
               var c = n.closest('.cell');
               return !!c && getComputedStyle(c).display !== 'none';
             }).map(function(n){ return Number(n.dataset.id); });
           })()"""
    )


def item_ids(cdp, selector=".item"):
    return [int(x) for x in cdp.evaluate("Array.from(document.querySelectorAll(%s)).map(function(n){return Number(n.dataset.id);})" % json.dumps(selector))]


def title_lines(cdp, selector: str):
    """Line boxes of an element's contents; None when it is not rendered (display:none).
    A hidden element has no line boxes, so it must not count as a wrapped title."""
    return cdp.evaluate(
        """(function(){
             var el = document.querySelector(%s);
             if (!el) return -1;
             var st = getComputedStyle(el);
             if (st.display === 'none' || st.visibility === 'hidden' || el.getClientRects().length === 0) return null;
             var r = document.createRange();
             r.selectNodeContents(el);
             return r.getClientRects().length;
           })()"""
        % json.dumps(selector)
    )


def read_key(cdp) -> str:
    return cdp.evaluate("'linuxdo-ai.read:' + (window.LINUXDO_AI_DEBUG.apiBase || location.origin)")


def read_set(cdp):
    key = read_key(cdp)
    return cdp.evaluate("(function(){try{return JSON.parse(localStorage.getItem(%s)||'[]');}catch(e){return 'PARSE-ERROR';}})()" % json.dumps(key))


def stored_json(cdp, key: str):
    return cdp.evaluate("(function(){try{return localStorage.getItem(%s);}catch(e){return 'BLOCKED';}})()" % json.dumps(key))


def item_state(cdp, topic_id: int) -> dict:
    return cdp.evaluate(
        """(function(id){
             var nodes = Array.from(document.querySelectorAll('.item[data-id="'+id+'"]'));
             return {
               count: nodes.length,
               read: nodes.filter(function(n){return n.classList.contains('is-read');}).length,
               cols: nodes.map(function(n){var c=n.closest('.cell');return c ? (c.className.match(/cell--c\\d/)||[''])[0] : '';}),
               label: nodes.length ? (nodes[0].getAttribute('aria-label')||'') : '',
               text: nodes.length ? (nodes[0].textContent||'') : '',
               weight: nodes.length ? getComputedStyle(nodes[0].querySelector('.item__title')).fontWeight : '',
               color: nodes.length ? getComputedStyle(nodes[0].querySelector('.item__title')).color : '',
               dot: nodes.length ? (function(){var s=getComputedStyle(nodes[0].querySelector('.item__title'),'::after');return {content:s.content,bg:s.backgroundColor,width:s.width};})() : null,
               cell: nodes.length ? nodes[0].closest('.cell').className : ''
             };
           })(%d)"""
        % topic_id
    )


def drawer_open(cdp) -> bool:
    return cdp.evaluate("document.querySelector('#drawer').hidden === false")


def drawer_visible_after(cdp, topic_id: int, timeout=4.0) -> bool:
    return bool(wait_js(cdp, "document.querySelector('#drawer').hidden === false && document.querySelector('#d-title').textContent.length > 0", timeout))


def open_drawer_js(cdp, selector: str) -> bool:
    """JS click: reliable for elements an overlay would cover; still the tap path."""
    js(cdp, "(function(){var n=document.querySelector(%s);if(!n)return false;n.click();return true;})()" % json.dumps(selector))
    return True


def close_drawer(cdp):
    js(cdp, "(function(){var b=document.querySelector('#d-close'); if(b) b.click(); return true;})()")
    time.sleep(0.4)


def click_js(cdp, selector: str):
    js(cdp, "(function(){var n=document.querySelector(%s);if(!n)return false;n.click();return true;})()" % json.dumps(selector))
    time.sleep(0.25)


def click_link_without_navigation(cdp, selector: str):
    """Click an <a target=_blank> while cancelling the navigation in the capture phase."""
    js(
        cdp,
        """(function(){
             var a = document.querySelector(%s);
             if (!a) return false;
             var guard = function(ev){ ev.preventDefault(); };
             document.addEventListener('click', guard, true);
             a.click();
             document.removeEventListener('click', guard, true);
             return true;
           })()"""
        % json.dumps(selector),
    )
    time.sleep(0.25)


def ensure_owner(cdp) -> bool:
    """fresh_page() wipes sessionStorage, so re-enter the token through the app's own path
    (prompt override + a real click on 管理) and wait for the verified chip."""
    if js(cdp, "document.querySelector('#authchip').textContent") == "已连接 · 可写":
        return True
    js(cdp, "window.prompt = function(){ return %s; };" % json.dumps(OWNER_TOKEN))
    js(cdp, "document.querySelector('#owner').click()")
    return bool(wait_js(cdp, "document.querySelector('#authchip').textContent === '已连接 · 可写'", timeout=5.0))


def park_mouse(cdp):
    """Move the real cursor off the list: a re-render under a resting cursor can fire a
    synthetic hover, which would open another row's preview mid-check."""
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=4, y=4, pointerType="mouse")
    time.sleep(0.35)


def mouse_move(cdp, selector: str):
    point = js(
        cdp,
        """(function(){
             var n = document.querySelector(%s);
             if (!n) return {missing: true};
             n.scrollIntoView({block: 'center', inline: 'nearest'});
             var r = n.getBoundingClientRect();
             return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
                     hits: (function(){var el=document.elementFromPoint(Math.round(r.left+r.width/2), Math.round(r.top+r.height/2));return !!el && (el===n || n.contains(el));})()};
           })()"""
        % json.dumps(selector),
    )
    if not point or point.get("missing"):
        return None
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=point["x"], y=point["y"], pointerType="mouse")
    return point


def touch_tap(cdp, selector: str) -> tuple[bool, dict]:
    point = js(
        cdp,
        """(function(){
             var n = document.querySelector(%s);
             if (!n) return {missing: true};
             n.scrollIntoView({block: 'center', inline: 'nearest'});
             var r = n.getBoundingClientRect();
             var x = Math.min(Math.max(Math.round(r.left + r.width/2), 1), window.innerWidth - 2);
             var y = Math.min(Math.max(Math.round(r.top + r.height/2), 1), window.innerHeight - 2);
             var el = document.elementFromPoint(x, y);
             return {x: x, y: y, hits: !!el && (el===n || n.contains(el)), at: el ? String(el.className||el.tagName).slice(0,40) : null};
           })()"""
        % json.dumps(selector),
    )
    if not point or point.get("missing") or not point.get("hits"):
        return False, point or {}
    cdp.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": point["x"], "y": point["y"]}])
    time.sleep(0.05)
    cdp.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
    time.sleep(0.6)
    return True, point


def screenshot(cdp, shots: Path, name: str, attempts: int = 3) -> str:
    """Chromium can drop a frame right after a viewport/media emulation switch; retry."""
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / name
    last = None
    for i in range(attempts):
        try:
            data = cdp.call("Page.captureScreenshot", format="png")
            path.write_bytes(base64.b64decode(data["data"]))
            return str(path)
        except Exception as err:          # noqa: BLE001 - a flaky frame must not abort the sweep
            last = err
            time.sleep(0.4 * (i + 1))
    print(f"      (screenshot {name} unavailable after {attempts} tries: {last})")
    return str(path)


# ------------------------------------------------------------------------- phases


def phase_a(cdp, base: str, shots: Path, fixtures: dict) -> None:
    url = f"{base}/index.html?fixture=1"
    fresh_page(cdp, url, width=1440, height=900)
    rejected_id = fixtures["rejected"][0]
    picked_id = fixtures["picked"][0]
    body_id = fixtures["pending_with_body"][0]
    nobody_id = fixtures["no_body"]

    def a1():
        sub = js(cdp, "document.querySelector('.masthead__brand .brand__sub') ? document.querySelector('.masthead__brand .brand__sub').textContent : null")
        h1 = js(cdp, "document.querySelector('.masthead__brand h1').textContent")
        standfirst = js(cdp, "!!document.querySelector('.standfirst')")
        return sub == SUBTITLE and h1 == "每日精选" and not standfirst, f"sub={sub!r} h1={h1!r} standfirst={standfirst}"

    def a2():
        tabs = js(cdp, "Array.from(document.querySelectorAll('#tabs .tab')).map(function(b){return b.textContent;})")
        head = js(cdp, "Array.from(document.querySelectorAll('.colhead .label')).map(function(b){return b.textContent;})")
        labels = js(cdp, "Array.from(document.querySelectorAll('#countline .count__label')).map(function(b){return b.textContent;})")
        return tabs == ["全部", "精选", "收藏"] and head == ["全部", "精选", "收藏"] and labels == ["全部", "精选", "收藏"], f"tabs={tabs} head={head} labels={labels}"

    def a3():
        if count(cdp, f'.cell--c3 .item[data-id="{rejected_id}"]'):
            return False, "fixture precondition: the rejected topic starts unbookmarked"
        before_order = item_ids(cdp, ".cell--c1 .item")
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{rejected_id}"]')
        if not drawer_visible_after(cdp, rejected_id):
            return False, "drawer did not open for the rejected topic"
        click_js(cdp, "#d-queue")
        wait_js(cdp, f'document.querySelectorAll(\'.cell--c3 .item[data-id="{rejected_id}"]\').length > 0')
        in_c3 = count(cdp, f'.cell--c3 .item[data-id="{rejected_id}"]')
        in_c1 = count(cdp, f'.cell--c1 .item[data-id="{rejected_id}"]')
        after_order = item_ids(cdp, ".cell--c1 .item")
        queued = js(cdp, "document.querySelector('#c-queue').textContent")
        return (in_c3 == 1 and in_c1 == 1 and after_order == before_order and queued == "1"), f"c3={in_c3} c1={in_c1} order_kept={after_order == before_order} count={queued}"

    def a4():
        # An explicit click on the 精选 row opens (and pins) the preview; the bookmark itself is
        # the drawer's 收藏 button - a row click never books, in any column.
        if count(cdp, f'.cell--c3 .item[data-id="{picked_id}"]'):
            return False, "fixture precondition: the picked topic starts unbookmarked"
        aimed, point = bc.mouse_click(cdp, f'.cell--c2 .item[data-id="{picked_id}"]')
        time.sleep(0.5)
        opened = drawer_open(cdp)
        c3_after_click = count(cdp, f'.cell--c3 .item[data-id="{picked_id}"]')
        queued_after_click = js(cdp, "document.querySelector('#c-queue').textContent")
        click_js(cdp, "#d-queue")
        wait_js(cdp, f'document.querySelectorAll(\'.cell--c3 .item[data-id="{picked_id}"]\').length === 1')
        c2 = count(cdp, f'.cell--c2 .item[data-id="{picked_id}"]')
        c3 = count(cdp, f'.cell--c3 .item[data-id="{picked_id}"]')
        queued = js(cdp, "document.querySelector('#c-queue').textContent")
        return (
            aimed and opened and c3_after_click == 0 and queued_after_click == "1"
            and c2 == 1 and c3 == 1 and queued == "2",
            f"aimed={aimed} opened={opened} c3_after_click={c3_after_click} "
            f"count_after_click={queued_after_click} c2={c2} c3={c3} count={queued} point={point}",
        )

    def a5():
        before = item_state(cdp, body_id)
        if before["read"]:
            return False, "fixture precondition: topic starts unread"
        stored_before = read_set(cdp)
        move = mouse_move(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not move:
            return False, "could not aim at the row"
        time.sleep(0.7)  # > hoverMs: the preview opens, but a hover is not a read
        hover_state = item_state(cdp, body_id)
        if not drawer_open(cdp):
            return False, "hover did not open the preview"
        if hover_state["read"]:
            return False, "hover marked the topic read"
        if read_set(cdp) != stored_before:
            return False, f"hover changed the read store: {stored_before!r} -> {read_set(cdp)!r}"
        cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=move["x"], y=move["y"], button="left", buttons=1, clickCount=1, pointerType="mouse")
        cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=move["x"], y=move["y"], button="left", buttons=0, clickCount=1, pointerType="mouse")
        wait_js(cdp, f'(document.querySelector(\'.item[data-id="{body_id}"]\')||{{classList:{{contains:function(){{return false;}}}}}}).classList.contains("is-read")')
        after = item_state(cdp, body_id)
        stored = read_set(cdp)
        visible_badge = js(cdp, "Array.from(document.querySelectorAll('#board .item')).filter(function(n){return (n.textContent||'').indexOf('已读')>=0;}).length")
        ok = (
            after["read"] == 1
            and after["weight"] == "400"
            and after["weight"] != before["weight"]
            and after["color"] != before["color"]
            and after["dot"]["content"] == "none"
            and before["dot"]["content"] not in ("none", None)
            and isinstance(stored, list) and body_id in stored
            and visible_badge == 0
            and "已读" in after["label"]
        )
        return ok, f"before={before['weight']}/{before['dot']['content']} after={after['weight']}/{after['dot']['content']} stored={stored} badge_rows={visible_badge}"

    def a6():
        js(cdp, "(function(){try{localStorage.clear();}catch(e){} return true;})()")
        cdp.call("Page.reload")
        time.sleep(1.8)
        move = mouse_move(cdp, f'.cell--c1 .item[data-id="{nobody_id}"]')
        if not move:
            return False, "could not aim at the no-body row"
        cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=move["x"], y=move["y"], button="left", buttons=1, clickCount=1, pointerType="mouse")
        cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=move["x"], y=move["y"], button="left", buttons=0, clickCount=1, pointerType="mouse")
        time.sleep(0.6)
        if js(cdp, "document.querySelector('#d-title').textContent").strip() == "":
            return False, "drawer did not open for the no-body topic"
        empty_notice = js(cdp, "document.querySelector('#d-body').classList.contains('is-empty')")
        after_open = item_state(cdp, nobody_id)
        click_link_without_navigation(cdp, "#d-link")
        after_link = item_state(cdp, nobody_id)
        stored = read_set(cdp)
        return (
            bool(empty_notice) and after_open["read"] == 0 and after_link["read"] == 1 and isinstance(stored, list) and nobody_id in stored,
            f"empty_notice={empty_notice} read_after_open={after_open['read']} read_after_link={after_link['read']} stored={stored}",
        )

    def a7():
        # duplicated instances of one topic must move together. Self-contained: bookmark the
        # picked topic here if A4 did not already do it, so this check never depends on A4.
        if count(cdp, f'.cell--c3 .item[data-id="{picked_id}"]') != 1:
            open_drawer_js(cdp, f'.cell--c2 .item[data-id="{picked_id}"]')
            if not drawer_visible_after(cdp, picked_id):
                return False, "could not open the picked topic to bookmark it"
            click_js(cdp, "#d-queue")
            wait_js(cdp, f'document.querySelectorAll(\'.cell--c3 .item[data-id="{picked_id}"]\').length === 1')
        state0 = item_state(cdp, picked_id)
        if state0["count"] != 2:
            return False, f"a picked+bookmarked topic holds two slots (count={state0['count']} cols={state0['cols']})"
        # start from unread and a closed drawer, then open the 收藏 copy explicitly
        if state0["read"] != 0:
            click_js(cdp, "#d-read")
            time.sleep(0.2)
        close_drawer(cdp)
        open_drawer_js(cdp, f'.cell--c3 .item[data-id="{picked_id}"]')
        wait_js(cdp, f'(document.querySelector(\'.cell--c2 .item[data-id="{picked_id}"]\')||{{classList:{{contains:function(){{return false;}}}}}}).classList.contains("is-read")')
        state = item_state(cdp, picked_id)
        return state["count"] == 2 and state["read"] == 2, f"instances={state['count']} read={state['read']} cols={state['cols']}"

    def a8():
        label_before = js(cdp, "document.querySelector('#d-read').getAttribute('aria-label')")
        pressed_before = js(cdp, "document.querySelector('#d-read').getAttribute('aria-pressed')")
        text = js(cdp, "document.querySelector('#d-read').textContent.trim()")
        click_js(cdp, "#d-read")
        wait_js(cdp, f'(document.querySelector(\'.cell--c2 .item[data-id="{picked_id}"]\')||{{classList:{{contains:function(){{return true;}}}}}}).classList.contains("is-read") === false')
        after_unread = item_state(cdp, picked_id)
        stored_unread = read_set(cdp)
        # the control names the action it would perform, and flips with the state
        label_unread = js(cdp, "document.querySelector('#d-read').getAttribute('aria-label')")
        pressed_unread = js(cdp, "document.querySelector('#d-read').getAttribute('aria-pressed')")
        # the subtle point: with the drawer still open, neither a re-render nor re-opening
        # the SAME topic may re-mark it read - only a navigation change may
        park_mouse(cdp)                       # a re-render under a resting cursor must not hover
        js(cdp, "window.dispatchEvent(new Event('resize')); true")
        time.sleep(0.4)
        after_rerender = item_state(cdp, picked_id)
        open_drawer_js(cdp, f'.cell--c3 .item[data-id="{picked_id}"]')
        time.sleep(0.4)
        after_reopen = item_state(cdp, picked_id)
        click_js(cdp, "#d-read")
        wait_js(cdp, f'(document.querySelector(\'.cell--c2 .item[data-id="{picked_id}"]\')||{{classList:{{contains:function(){{return false;}}}}}}).classList.contains("is-read")')
        after_read = item_state(cdp, picked_id)
        label_after = js(cdp, "document.querySelector('#d-read').getAttribute('aria-label')")
        return (
            pressed_before == "true"
            and label_before == "标记为未读"
            and label_unread == "标记为已读"
            and pressed_unread == "false"
            and label_after == "标记为未读"
            and text == ""
            and after_unread["read"] == 0
            and after_rerender["read"] == 0
            and after_reopen["read"] == 0
            and isinstance(stored_unread, list) and picked_id not in stored_unread
            and after_read["read"] == 2,
            f"pressed={pressed_before}/{pressed_unread} label={label_before!r}->{label_unread!r}->{label_after!r} text={text!r} "
            f"unread={after_unread['read']} rerender={after_rerender['read']} reopen={after_reopen['read']} read={after_read['read']}",
        )

    def a9():
        order_before = item_ids(cdp)
        cdp.call("Page.reload")
        time.sleep(2.0)
        state = item_state(cdp, picked_id)
        order_after = item_ids(cdp)
        unread = item_state(cdp, rejected_id)
        return (
            state["read"] == 2 and unread["read"] == 0 and order_after == order_before,
            f"read_after_reload={state['read']} other_unread={unread['read']} order_kept={order_after == order_before}",
        )

    def a10():
        key = read_key(cdp)
        before = item_state(cdp, picked_id)
        js(cdp, "(function(){localStorage.setItem(%s, '[]'); return true;})()" % json.dumps(key))
        untouched = item_state(cdp, picked_id)
        js(cdp, "window.dispatchEvent(new StorageEvent('storage', {key: %s, newValue: '[]'})); true" % json.dumps(key))
        time.sleep(0.3)
        synced = item_state(cdp, picked_id)
        js(cdp, "(function(){localStorage.setItem(%s, JSON.stringify([%d])); return true;})()" % (json.dumps(key), picked_id))
        js(cdp, "window.dispatchEvent(new StorageEvent('storage', {key: %s, newValue: '[%d]'})); true" % (json.dumps(key), picked_id))
        time.sleep(0.3)
        restored = item_state(cdp, picked_id)
        return (
            before["read"] == 2 and untouched["read"] == 2 and synced["read"] == 0 and restored["read"] == 2,
            f"before={before['read']} untouched={untouched['read']} synced={synced['read']} restored={restored['read']}",
        )

    def a11():
        # malformed JSON must not break the page
        key = read_key(cdp)
        js(cdp, "(function(){localStorage.setItem(%s, 'not-json{'); return true;})()" % json.dumps(key))
        cdp.call("Page.reload")
        time.sleep(1.8)
        items = count(cdp, ".item")
        state = item_state(cdp, picked_id)
        # bounded: an over-long stored set is trimmed to the most recent entries
        js(cdp, "(function(){var a=[];for(var i=1;i<=1500;i++)a.push(i);localStorage.setItem(%s, JSON.stringify(a));return true;})()" % json.dumps(key))
        cdp.call("Page.reload")
        time.sleep(1.8)
        stored = read_set(cdp)
        long_ok = isinstance(stored, list) and len(stored) == 1000 and stored[0] == 501 and stored[-1] == 1500
        return (items > 0 and state["read"] == 0 and long_ok), f"items={items} read_after_malformed={state['read']} stored_len={len(stored) if isinstance(stored, list) else stored} first={stored[0] if isinstance(stored, list) else '-'}"

    def a12():
        # blocked storage: the app must keep working, in memory only
        script = cdp.call(
            "Page.addScriptToEvaluateOnNewDocument",
            source="Object.defineProperty(window, 'localStorage', {get: function(){ throw new Error('blocked by test'); }});",
        )
        try:
            cdp.call("Page.reload")
            time.sleep(1.8)
            items = count(cdp, ".item")
            open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
            wait_js(cdp, f'(document.querySelector(\'.item[data-id="{body_id}"]\')||{{classList:{{contains:function(){{return false;}}}}}}).classList.contains("is-read")')
            state = item_state(cdp, body_id)
            return (items > 0 and state["read"] >= 1), f"items={items} read={state['read']}"
        finally:
            cdp.call("Page.removeScriptToEvaluateOnNewDocument", identifier=script["identifier"])

    run("A1  subtitle sits in the masthead brand block, no far-left standfirst", a1)
    run("A2  tabs/columns/counts use 全部 · 精选 · 收藏", a2)
    run("A3  a bookmarked rejected topic shows under 收藏 and stays in 全部", a3)
    run("A4  a bookmarked picked topic keeps 精选 and gains 收藏", a4)
    run("A5  hover opens the preview without reading; the click marks it read", a5)
    run("A6  a no-body preview stays unread until the original link is opened", a6)
    run("A7  duplicated instances of one topic update together", a7)
    run("A8  the drawer icon toggles read/unread (no visible text)", a8)
    run("A9  read state survives a reload, order and position unchanged", a9)
    run("A10 a storage event from another tab re-syncs the rows", a10)
    run("A11 malformed + over-long read storage is handled and bounded", a11)
    run("A12 blocked localStorage does not break the page", a12)
    screenshot(cdp, shots, "reading-desktop-1440-light.png")


def phase_b(cdp, base: str, fixture_file: dict) -> None:
    url = f"{base}/index.html?fixture=1"
    fresh_page(cdp, url, width=390, height=844, mobile=True, touch=True)
    rejected_id = fixture_file["rejected"][0]
    body_id = fixture_file["pending_with_body"][0]

    def b1():
        tabs = js(cdp, "Array.from(document.querySelectorAll('#tabs .tab')).map(function(b){return b.textContent;})")
        visible = visible_items(cdp)
        dupes = [i for i in set(visible) if visible.count(i) > 1]
        return (tabs == ["全部", "精选", "收藏"] and len(visible) == len(fixture_file["topics"]) and not dupes), f"tabs={tabs} visible={len(visible)} dupes={dupes}"

    def b2():
        ok, point = touch_tap(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not ok:
            return False, f"could not tap the row: {point}"
        opened = drawer_visible_after(cdp, body_id)
        state = item_state(cdp, body_id)
        close_drawer(cdp)
        click_js(cdp, "#tab-queue")
        time.sleep(0.4)
        return (opened and state["read"] == 1), f"drawer={opened} read={state['read']} point={point}"

    def b3():
        # bookmark a rejected topic through the drawer, then find it in 收藏
        click_js(cdp, "#tab-all")
        time.sleep(0.3)
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{rejected_id}"]')
        if not drawer_visible_after(cdp, rejected_id):
            return False, "drawer did not open"
        click_js(cdp, "#d-queue")
        close_drawer(cdp)
        click_js(cdp, "#tab-queue")
        time.sleep(0.4)
        visible = visible_items(cdp)
        queue_label = js(cdp, "document.querySelector('#tab-queue').textContent")
        return (visible == [rejected_id] and queue_label == "收藏"), f"visible={visible} label={queue_label!r}"

    def b4():
        click_js(cdp, "#tab-picked")
        time.sleep(0.4)
        visible = visible_items(cdp)
        picked = [t for t in fixture_file["topics"] if t["state"] == "picked"]
        return (len(visible) == len(picked) and len(set(visible)) == len(visible)), f"visible={len(visible)} picked={len(picked)}"

    def b5():
        lines = [title_lines(cdp, s) for s in (".brand", ".brand__sub", "#tab-all", "#tab-picked", "#tab-queue")]
        visible = [n for n in lines if n is not None]
        overflow = js(cdp, "document.documentElement.scrollWidth - window.innerWidth")
        return (len(visible) == 5 and all(n == 1 for n in visible) and overflow <= 1), f"lines={lines} overflow={overflow}"


    def b6():
        # a tap on the 收藏 row opens the preview sheet and keeps the bookmark
        click_js(cdp, "#tab-queue")
        time.sleep(0.4)
        ok, point = touch_tap(cdp, f'.cell--c3 .item[data-id="{rejected_id}"]')
        if not ok:
            return False, f"could not tap the bookmark: {point}"
        opened = drawer_visible_after(cdp, rejected_id)
        text = js(cdp, "document.querySelector('#d-queue').textContent")
        close_drawer(cdp)
        still = count(cdp, f'.item[data-id="{rejected_id}"]')
        return (opened and text == "取消收藏" and still >= 1), f"opened={opened} action={text!r} instances={still}"

    run("B1  mobile tabs show every topic exactly once", b1)
    run("B2  a tap opens the preview sheet and marks the topic read", b2)
    run("B3  a rejected topic bookmarked on mobile shows in the 收藏 tab", b3)
    run("B4  精选 lists every picked topic exactly once", b4)
    run("B5  mobile header/tabs stay on one line, no overflow", b5)
    run("B6  the 收藏 row still opens the preview and keeps its bookmark", b6)


BODY_ONLY_TEXT = "只有正文没有摘要的帖子内容。" * 30
BODY_ONLY_ID = 900000001


def phase_c(cdp, base: str, fixtures: dict, shots: Path) -> None:
    url = f"{base}/index.html"
    rejected_id = fixtures["rejected"][0]
    picked_id = fixtures["picked"][0]
    body_id = fixtures["pending_with_body"][0]

    # The fixture has no topic that carries a body without an excerpt, and that is the only
    # shape that proves `has_body` schedules a read without counting as displayed content.
    # It is added to the stub's raw state (real mode only); the fixture file is untouched.
    raw = bc.StubHandler.state["state"]
    if not any(t.get("id") == BODY_ONLY_ID for t in raw["topics"]):
        sample = dict(fixtures["topics"][0])
        raw["topics"].append(
            {
                "id": BODY_ONLY_ID,
                "title": "只有正文没有摘要的帖子（测试）",
                "url": "https://linux.do/t/topic/%d" % BODY_ONLY_ID,
                "state": "rejected",
                "created_at": sample.get("created_at"),
                "bumped_at": sample.get("bumped_at"),
                "reply_count": 3,
                "views": 42,
                "category": sample.get("category"),
                "tags": sample.get("tags") or [],
                "excerpt": "",
                "body_text": BODY_ONLY_TEXT,
                "filter": {"score": 1, "summary": "只有正文没有摘要。", "decision": "reject"},
            }
        )
    fresh_page(cdp, url, width=1280, height=900)

    def c1():
        owner_text = js(cdp, "document.querySelector('#owner').textContent")
        chip = js(cdp, "document.querySelector('#authchip').textContent")
        width = js(cdp, "document.querySelector('#owner').getBoundingClientRect().width")
        disabled = js(cdp, "document.querySelector('#refresh').disabled")
        return (owner_text == "管理" and chip == "只读" and disabled is True and width > 0), f"owner={owner_text!r} chip={chip!r} width={width} refresh_disabled={disabled}"

    def c2():
        before = js(cdp, "document.querySelector('#owner').getBoundingClientRect().width")
        js(cdp, "window.prompt = function(){ return %s; };" % json.dumps(OWNER_TOKEN))
        js(cdp, "document.querySelector('#owner').click()")
        time.sleep(1.4)
        after = js(cdp, "document.querySelector('#owner').getBoundingClientRect().width")
        text = js(cdp, "document.querySelector('#owner').textContent")
        chip = js(cdp, "document.querySelector('#authchip').textContent")
        local = js(cdp, "(function(){try{return localStorage.getItem('linuxdo-ai.ownerToken');}catch(e){return 'BLOCKED';}})()")
        session = js(cdp, "(function(){try{return !!sessionStorage.getItem('linuxdo-ai.ownerToken');}catch(e){return false;}})()")
        dot = js(cdp, "document.querySelector('#owner').dataset.owner")
        return (
            abs(before - after) < 0.5 and text == "管理" and chip == "已连接 · 可写" and local is None and session is True,
            f"width {before}->{after} text={text!r} chip={chip!r} localStorage={local!r} session={session} dot={dot!r}",
        )

    def c3():
        cc = bc.StubHandler
        cc.state["queue"] = []
        cc.requests.clear()
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{rejected_id}"]')
        if not drawer_visible_after(cdp, rejected_id):
            return False, "drawer did not open for the rejected topic"
        click_js(cdp, "#d-queue")
        time.sleep(1.2)
        posts = [r for r in cc.requests if r["path"] == "/api/queue" and r["method"] == "POST"]
        body = posts[0]["body"] if posts else None
        local = stored_json(cdp, "linuxdo-ai.queue")
        in_c3 = count(cdp, f'.cell--c3 .item[data-id="{rejected_id}"]')
        in_c1 = count(cdp, f'.cell--c1 .item[data-id="{rejected_id}"]')
        return (
            len(posts) == 1 and isinstance(body, dict) and set(body) == {"add"} and body["add"] == rejected_id
            and posts[0]["authorization"] == f"Bearer {OWNER_TOKEN}"
            and in_c3 == 1 and in_c1 == 1 and local == str([rejected_id]).replace(" ", ""),
            f"posts={json.dumps([r['body'] for r in posts])} c3={in_c3} c1={in_c1} local={local!r}",
        )

    def c4():
        cc = bc.StubHandler
        cc.requests.clear()
        queue_before = list(cc.state["queue"])
        click_js(cdp, "#d-keep")
        time.sleep(1.2)
        votes = [r for r in cc.requests if r["path"] == "/api/feedback" and r["method"] == "POST"]
        queue_posts = [r for r in cc.requests if r["path"] == "/api/queue" and r["method"] == "POST"]
        local = stored_json(cdp, "linuxdo-ai.queue")
        in_c2 = count(cdp, f'.cell--c2 .item[data-id="{rejected_id}"]')
        in_c3 = count(cdp, f'.cell--c3 .item[data-id="{rejected_id}"]')
        ok = (
            len(votes) == 1
            and isinstance(votes[0]["body"], dict) and votes[0]["body"].get("vote") == "keep"
            and queue_posts == []
            and cc.state["queue"] == queue_before
            and in_c2 == 1 and in_c3 == 1
        )
        return ok, f"votes={json.dumps([r['body'] for r in votes])} queue_posts={len(queue_posts)} server_queue={cc.state['queue']} c2={in_c2} c3={in_c3} local={local!r}"

    def c5():
        cc = bc.StubHandler
        cc.state["state_status"] = 500
        items_before = count(cdp, ".item")
        cc.requests.clear()
        js(cdp, "document.querySelector('#refresh').click()")
        time.sleep(3.0)
        items_after = count(cdp, ".item")
        board_hidden = js(cdp, "document.querySelector('#board').hidden")
        error = js(cdp, "document.querySelector('#errorstate').hidden ? '' : document.querySelector('#errordetail').textContent")
        gets = [r for r in cc.requests if r["path"] == "/api/state"]
        refreshes = [r for r in cc.requests if r["path"] == "/api/refresh" and r["method"] == "POST"]
        opstatus = js(cdp, "document.querySelector('#opstatus').hidden ? '' : document.querySelector('#opstatus').textContent")
        time.sleep(1.5)
        gets_later = [r for r in cc.requests if r["path"] == "/api/state"]
        busy = js(cdp, "document.querySelector('#refresh').getAttribute('aria-busy')")
        disabled = js(cdp, "document.querySelector('#refresh').disabled")
        # collapsed + safe technical facts: category, fixed endpoint, client ISO time, last success
        tech_hidden = js(cdp, "document.querySelector('#errortech').hidden")
        tech_open = js(cdp, "document.querySelector('#errortech').open")
        facts = js(cdp, "(function(){var d=document.querySelector('#errortech');if(!d||d.hidden)return '';d.open=true;return document.querySelector('#errorfacts').textContent;})()")
        stamps = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", facts or "")
        safe_facts = (
            "http 500" in (facts or "")
            and "/api/state" in (facts or "")
            and "客户端时间" in (facts or "")
            and "上次成功读取" in (facts or "")
            and len(stamps) >= 2                       # failure time + last successful read
            and OWNER_TOKEN not in (facts or "")
            and "Bearer" not in (facts or "")
            and "authorization" not in (facts or "").lower()
            and "http://" not in (facts or "")         # never a full URL
            and "?" not in (facts or "")               # never a query string
        )
        return (
            items_after == items_before and items_after > 0 and board_hidden is False
            and "HTTP 500" in (error or "") and "重启" not in (error or "")
            and len(gets) == 1 and len(gets_later) == 1 and busy in (None, "0", False) and disabled is False
            and tech_hidden is False and tech_open is False and safe_facts,
            f"items={items_before}->{items_after} board_hidden={board_hidden} error={error!r} gets={len(gets)}/{len(gets_later)} "
            f"refresh_posts={len(refreshes)} busy={busy!r} op={opstatus!r} "
            f"tech={'collapsed+ok' if safe_facts else repr(facts)}",
        )

    def c6():
        cc = bc.StubHandler
        cc.state["state_status"] = 200
        cc.requests.clear()
        js(cdp, "document.querySelector('#retry').click()")
        time.sleep(1.5)
        error_hidden = js(cdp, "document.querySelector('#errorstate').hidden")
        items = count(cdp, ".item")
        gets = [r for r in cc.requests if r["path"] == "/api/state"]
        return (error_hidden is True and items > 0 and len(gets) == 1), f"error_hidden={error_hidden} items={items} gets={len(gets)}"

    def c7():
        cc = bc.StubHandler
        fresh_page(cdp, url + "?readtimeout=800", width=1280, height=900, settle_s=2.0)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token after the page reset"
        items = count(cdp, ".item")
        # a slow read is bounded by the deadline; the rendered snapshot stays
        cc.state["state_delay"] = 4.0
        cc.requests.clear()
        js(cdp, "document.querySelector('#refresh').click()")
        time.sleep(2.6)
        error = js(cdp, "document.querySelector('#errorstate').hidden ? '' : document.querySelector('#errordetail').textContent")
        kept = count(cdp, ".item")
        # the collapsed facts must name the category and carry a safe client timestamp
        tech_open_before = js(cdp, "document.querySelector('#errortech').open")
        facts = js(cdp, "(function(){var d=document.querySelector('#errortech');if(!d||d.hidden)return '';d.open=true;return document.querySelector('#errorfacts').textContent;})()")
        cc.state["state_delay"] = 0.0
        js(cdp, "document.querySelector('#retry').click()")
        time.sleep(1.6)
        recovered = js(cdp, "document.querySelector('#errorstate').hidden")
        timeout_facts = (
            tech_open_before is False
            and "timeout" in (facts or "")
            and "/api/state" in (facts or "")
            and bool(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", facts or ""))
            and OWNER_TOKEN not in (facts or "")
            and "http://" not in (facts or "")
        )
        return (
            kept == items and items > 0 and "超时" in (error or "") and "重启" not in (error or "") and recovered is True
            and timeout_facts,
            f"items={items}->{kept} error={error!r} recovered={recovered} "
            f"tech={'collapsed+ok' if timeout_facts else repr(facts)}",
        )

    def c8():
        cc = bc.StubHandler
        cc.state["state_delay"] = 1.2
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.0)
        cc.state["state_delay"] = 1.2
        cc.requests.clear()
        js(cdp, "document.querySelector('#refresh').click()")   # POST + sleep(1200) then pull
        time.sleep(1.5)                                          # its pull is now in flight
        js(cdp, "document.querySelector('#retry').click()")      # a second, overlapping pull
        time.sleep(1.8)
        gets = [r for r in cc.requests if r["path"] == "/api/state"]
        cc.state["state_delay"] = 0.0
        return (len(gets) == 1), f"state_gets={len(gets)}"

    def c9():
        # the token never lands in localStorage, the URL or the DOM
        local = js(cdp, "(function(){try{return localStorage.getItem('linuxdo-ai.ownerToken');}catch(e){return 'BLOCKED';}})()")
        url_has = OWNER_TOKEN in cdp.evaluate("location.href")
        return (local is None and url_has is False), f"localStorage={local!r} in_url={url_has}"

    run("C1  the management control reads 管理 and the chip says 只读", c1)
    run("C2  auth state changes without moving the button (fixed geometry)", c2)
    run("C3  bookmarking a rejected topic is one authenticated POST", c3)
    run("C4  纳入精选 is one vote POST and never touches the bookmark", c4)
    run("C5  a failed refresh keeps the rendered snapshot + a truthful notice", c5)
    run("C6  重试 recovers after the failure without a storm", c6)
    run("C7  a slow read is bounded and reports a timeout, not a guess", c7)
    run("C8  overlapping pulls are deduplicated into one read", c8)
    def c10():
        # the list payload carries has_body; the body itself arrives from /api/topic/<id>
        cc = bc.StubHandler
        cc.state["topic_status"] = 200
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        expected = fixtures["body_text_of"][body_id]
        if not expected:
            return False, "fixture precondition: the topic has no body"
        listed = [r for r in cc.requests if r["path"] == "/api/state"]
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not drawer_visible_after(cdp, body_id):
            return False, "drawer did not open for the body topic"
        matched = wait_js(
            cdp,
            "(function(){var b=document.querySelector('#d-body');return !!(b && b.textContent === %s);})()" % json.dumps(expected),
            timeout=6.0,
        )
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        empty = js(cdp, "document.querySelector('#d-body').classList.contains('is-empty')")
        note_hidden = js(cdp, "document.querySelector('#d-bodynote').hidden")
        first = [r["path"] for r in cc.requests if r["path"].startswith("/api/topic/")]
        # re-opening the same topic draws on the cache: no second body read
        close_drawer(cdp)
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        time.sleep(0.8)
        again = [r["path"] for r in cc.requests if r["path"].startswith("/api/topic/")]
        return (
            # fresh_page navigates and then reloads, so /api/state itself is read twice here;
            # what matters is that the body route is read exactly once and then served from cache
            bool(matched) and len(first) == 1 and len(again) == 1 and len(listed) >= 1
            and empty is False and note_hidden is True,
            f"match={bool(matched)} route_gets={first} after_reopen={len(again)} state_gets={len(listed)} "
            f"empty={empty} note_hidden={note_hidden} shown={shown[:30]!r}",
        )

    def c11():
        # a hover preview is a prefetch of the row: it neither reads nor fetches the body
        cc = bc.StubHandler
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        before = item_state(cdp, body_id)
        move = mouse_move(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not move:
            return False, "could not aim at the body row"
        time.sleep(0.8)          # > hoverMs: the preview really opened
        reads = [r["path"] for r in cc.requests if r["path"].startswith("/api/topic/")]
        after = item_state(cdp, body_id)
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        excerpt = fixtures["excerpt_of"][body_id]
        return (
            drawer_open(cdp) and reads == [] and before["read"] == 0 and after["read"] == 0 and shown == excerpt,
            f"reads={reads} before={before['read']} after={after['read']} excerpt_shown={shown == excerpt} shown={shown[:30]!r}",
        )

    def c12():
        # a failed body read keeps the excerpt, adds one truthful line, and still marks read
        cc = bc.StubHandler
        cc.state["topic_status"] = 500
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        excerpt = fixtures["excerpt_of"][body_id]
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not drawer_visible_after(cdp, body_id):
            return False, "drawer did not open for the body topic"
        flagged = wait_js(
            cdp,
            "(function(){var n=document.querySelector('#d-bodynote');return !!(n && !n.hidden && n.textContent);})()",
            timeout=6.0,
        )
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        note = js(cdp, "document.querySelector('#d-bodynote').textContent")
        reads = [r["path"] for r in cc.requests if r["path"].startswith("/api/topic/")]
        state = item_state(cdp, body_id)
        cc.state["topic_status"] = 200
        return (
            bool(flagged) and shown == excerpt and len(reads) == 1 and state["read"] >= 1
            and "重启" not in note and "正文读取失败" in note,
            f"note={note!r} excerpt_kept={shown == excerpt} reads={len(reads)} read_marked={state['read']}",
        )

    BODY_ONLY_ROW = f'.cell--c1 .item[data-id="{BODY_ONLY_ID}"]'
    BODY_ONLY_SHOWN = "(function(){var b=document.querySelector('#d-body');return !!(b && b.textContent.indexOf('只有正文没有摘要的帖子内容') === 0);})()"
    NOTE_VISIBLE = "(function(){var n=document.querySelector('#d-bodynote');return !!(n && !n.hidden && n.textContent);})()"

    def c13():
        # the reader asks for the body-free list view; the same stub still answers the legacy read
        cc = bc.StubHandler
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        gets = [r for r in cc.requests if r["path"] == "/api/state" and r["method"] == "GET"]
        legacy, listview = bc.state_shapes(base)
        return (
            bool(gets) and all("view=list" in (r.get("query") or "") for r in gets)
            and any("body_text" in t for t in legacy["topics"])
            and all("body_text" not in t and "has_body" in t for t in listview["topics"])
            and any(t["has_body"] for t in listview["topics"]),
            f"queries={[r.get('query') for r in gets][:2]} "
            f"legacy_body={'body_text' in legacy['topics'][0]} list_body={'body_text' in listview['topics'][0]}",
        )

    def c14():
        # a body-only topic stays unread while its body loads, and becomes read once it is shown
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 1.8}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        time.sleep(0.8)                      # the read is still in flight
        loading = js(cdp, "document.querySelector('#d-body').textContent")
        during = item_state(cdp, BODY_ONLY_ID)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=8.0)
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        after = item_state(cdp, BODY_ONLY_ID)
        cc.state["topic_delay_ids"] = {}
        return (
            during["read"] == 0 and "正在读取正文" in (loading or "") and bool(arrived)
            and shown.startswith("只有正文没有摘要的帖子内容") and after["read"] >= 1,
            f"while_loading={loading[:24]!r} read_during={during['read']} arrived={bool(arrived)} read_after={after['read']}",
        )

    def c15():
        # a body-only topic whose read fails stays unread, with an honest line and no stale promise
        cc = bc.StubHandler
        cc.state["topic_status"] = 500
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_status"] = 200
            return False, "drawer did not open for the body-only topic"
        flagged = wait_js(cdp, NOTE_VISIBLE, timeout=8.0)
        note = js(cdp, "document.querySelector('#d-bodynote').textContent")
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        reads = [r["path"] for r in cc.requests if r["path"].startswith("/api/topic/")]
        after = item_state(cdp, BODY_ONLY_ID)
        cc.state["topic_status"] = 200
        return (
            bool(flagged) and "正文读取失败" in (note or "") and "重启" not in (note or "")
            and "正在读取正文" not in (shown or "") and len(reads) == 1 and after["read"] == 0,
            f"note={note!r} shown={shown[:24]!r} reads={len(reads)} read={after['read']}",
        )

    def c16():
        # a manual 未读 while the body is still coming wins: the arriving body must not re-mark it
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {body_id: 1.8}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
        if not drawer_visible_after(cdp, body_id):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body topic"
        marked = item_state(cdp, body_id)        # the excerpt is shown, so the open marks it
        click_js(cdp, "#d-read")                 # the reader says 未读 while the body loads
        time.sleep(0.3)
        manual = item_state(cdp, body_id)
        arrived = wait_js(
            cdp,
            "(function(){var b=document.querySelector('#d-body');return !!(b && b.textContent === %s);})()"
            % json.dumps(fixtures["body_text_of"][body_id]),
            timeout=8.0,
        )
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        after = item_state(cdp, body_id)
        cc.state["topic_delay_ids"] = {}
        return (
            marked["read"] >= 1 and manual["read"] == 0 and bool(arrived) and after["read"] == 0
            and shown == fixtures["body_text_of"][body_id],
            f"after_open={marked['read']} after_manual_unread={manual['read']} body_arrived={bool(arrived)} "
            f"read_after_body={after['read']} body_shown={shown == fixtures['body_text_of'][body_id]}",
        )

    def c17():
        # switching topic mid-read: the late answer must not mark or replace the topic on screen
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 1.8}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)                       # slow, body-only
        time.sleep(0.2)
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')   # fast, excerpt + body
        if not drawer_visible_after(cdp, body_id):
            cc.state["topic_delay_ids"] = {}
            return False, "the drawer did not follow the switch"
        settled = wait_js(
            cdp,
            "(function(){var b=document.querySelector('#d-body');return !!(b && b.textContent === %s);})()"
            % json.dumps(fixtures["body_text_of"][body_id]),
            timeout=8.0,
        )
        time.sleep(2.0)              # the slow topic answers AFTER the switch
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        reads = sorted(r["path"] for r in cc.requests if r["path"].startswith("/api/topic/"))
        stale = item_state(cdp, BODY_ONLY_ID)
        current = item_state(cdp, body_id)
        cc.state["topic_delay_ids"] = {}
        return (
            bool(settled) and shown == fixtures["body_text_of"][body_id] and stale["read"] == 0
            and current["read"] >= 1 and reads == sorted([f"/api/topic/{body_id}", f"/api/topic/{BODY_ONLY_ID}"]),
            f"reads={reads} body_kept={shown == fixtures['body_text_of'][body_id]} stale_read={stale['read']} "
            f"current_read={current['read']}",
        )

    def c18():
        # the body read is bounded by the same deadline, and a later visit really retries
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 3.0}
        cc.requests.clear()
        fresh_page(cdp, url + "?readtimeout=700", width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        flagged = wait_js(cdp, NOTE_VISIBLE, timeout=8.0)
        note = js(cdp, "document.querySelector('#d-bodynote').textContent")
        first = item_state(cdp, BODY_ONLY_ID)
        cc.state["topic_delay_ids"] = {}          # the next read answers immediately
        close_drawer(cdp)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=8.0)
        after = item_state(cdp, BODY_ONLY_ID)
        return (
            bool(flagged) and "正文读取失败" in (note or "") and first["read"] == 0
            and bool(arrived) and after["read"] >= 1,
            f"note={note!r} read_after_timeout={first['read']} retry_arrived={bool(arrived)} read_after_retry={after['read']}",
        )

    def c19():
        # headers fast, body past the deadline: the reader must report the deadline, not a payload
        cc = bc.StubHandler
        cc.state["state_stream_delay"] = 0.0
        fresh_page(cdp, url + "?readtimeout=700", width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            return False, "could not re-enter the owner token after the page reset"
        items = count(cdp, ".item")
        cc.state["state_stream_delay"] = 3.0      # headers now, body long after the deadline
        cc.requests.clear()
        js(cdp, "document.querySelector('#refresh').click()")
        time.sleep(3.4)
        error = js(cdp, "document.querySelector('#errorstate').hidden ? '' : document.querySelector('#errordetail').textContent")
        facts = js(cdp, "(function(){var d=document.querySelector('#errortech');if(!d||d.hidden)return '';d.open=true;return document.querySelector('#errorfacts').textContent;})()")
        kept = count(cdp, ".item")
        cc.state["state_stream_delay"] = 0.0
        return (
            kept == items and items > 0
            and "未能读完响应" in (error or "") and "没有响应" not in (error or "")
            and "超时" in (error or "") and "重启" not in (error or "")
            and "timeout" in (facts or ""),
            f"items={items}->{kept} error={error!r} facts={facts!r}",
        )

    def c20():
        # an accepted snapshot invalidates a body read that was already in flight.
        # The 刷新 click is an outside click, so the pinned preview closes with it (settled
        # outside-click contract); the assertion is that the in-flight read for the OLD
        # snapshot never displays and never marks, whatever the drawer is doing.
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 2.4}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            cc.state["topic_delay_ids"] = {}
            return False, "could not re-enter the owner token"
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        js(cdp, "document.querySelector('#refresh').click()")    # a new snapshot, body still coming
        time.sleep(1.0)
        dismissed = not drawer_open(cdp)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=5.0)      # it must NOT be displayed
        time.sleep(0.5)
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        state = item_state(cdp, BODY_ONLY_ID)
        cc.state["topic_delay_ids"] = {}
        return (
            dismissed and not arrived and state["read"] == 0 and "只有正文没有摘要的帖子内容" not in (shown or ""),
            f"preview_dismissed={dismissed} stale_body_shown={bool(arrived)} read={state['read']} shown={shown[:24]!r}",
        )

    def c21():
        # the body cache stays bounded: the cap evicts the oldest entry, memory stays flat
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        before = js(cdp, "window.LINUXDO_AI_DEBUG.bodyCacheSize()")
        js(cdp, "(function(){for (var i = 1; i <= 60; i++) window.LINUXDO_AI_DEBUG.bodyCachePut(700000 + i, 'B' + i);})()")
        size = js(cdp, "window.LINUXDO_AI_DEBUG.bodyCacheSize()")
        ids = js(cdp, "window.LINUXDO_AI_DEBUG.bodyCacheIds()")
        return (
            before == 0 and size == 50 and ids[0] == 700011 and ids[-1] == 700060,
            f"before={before} after={size} window={ids[0] if ids else None}..{ids[-1] if ids else None}",
        )

    # ---------------------------------------------------------------- deadline acceptance
    # The incident HAR captured 275712 body bytes over 11.791 s of receive time: 23383 B/s
    # (its content-length was 2718951). The compressed list shape is ~347 KB, so a real read of it
    # takes ~14.8 s on that connection - longer than the old 12 s bound and comfortably inside the
    # shipped 30 s. Both checks below use the SAME representative payload at the SAME rate; only
    # the bound differs, and both are timed.
    THROTTLE_BPS = 23383.3
    THROTTLE_BYTES = 347000

    def throttle(on: bool):
        cc = bc.StubHandler
        cc.state["state_throttle_bps"] = THROTTLE_BPS if on else 0
        cc.state["state_throttle_bytes"] = THROTTLE_BYTES
        cc.state["state_delay"] = 0.0
        cc.state["state_stream_delay"] = 0.0
        cc.state["throttle_wire_bytes"] = 0

    def c22():
        # RED side: the old 12 s bound aborts a read that is still progressing
        throttle(True)
        start = time.time()
        open_page(cdp, url + "?readtimeout=12000", width=1280, height=900, settle_s=1.0)
        failed = wait_js(
            cdp,
            "(function(){return document.querySelector('#errorstate').hidden === false;})()",
            timeout=30.0,
        )
        elapsed = time.time() - start
        error = js(cdp, "document.querySelector('#errordetail').textContent")
        items = count(cdp, ".item")
        facts = js(cdp, "(function(){var d=document.querySelector('#errortech');if(!d||d.hidden)return '';d.open=true;return document.querySelector('#errorfacts').textContent;})()")
        wire = bc.StubHandler.state.get("throttle_wire_bytes")
        throttle(False)
        return (
            bool(failed) and items == 0 and "未能读完响应" in (error or "") and "12" in (error or "")
            and "timeout" in (facts or "") and 10.0 < elapsed < 27.0,
            f"elapsed={elapsed:.1f}s bound=12000 wire_bytes={wire} items={items} error={error!r}",
        )

    def c23():
        # GREEN side: the shipped 30 s bound reads the same payload to the end, in real time
        throttle(True)
        start = time.time()
        open_page(cdp, url, width=1280, height=900, settle_s=1.0)
        rendered = wait_js(
            cdp,
            "(function(){return document.querySelectorAll('.item').length > 0;})()",
            timeout=45.0,
        )
        elapsed = time.time() - start
        items = count(cdp, ".item")
        error_hidden = js(cdp, "document.querySelector('#errorstate').hidden")
        bound = js(cdp, "window.LINUXDO_AI_DEBUG.readDeadlineMs")
        wire = bc.StubHandler.state.get("throttle_wire_bytes")
        throttle(False)
        rate = (wire / elapsed) if (wire and elapsed) else 0
        return (
            bool(rendered) and items > 0 and error_hidden is True and bound == 30000
            and elapsed > 12.0 and elapsed < 30.0,
            f"elapsed={elapsed:.1f}s bound={bound} wire_bytes={wire} items={items} "
            f"error_hidden={error_hidden} effective={rate:.0f} B/s",
        )

    run("C9  the owner token stays out of localStorage and the URL", c9)
    run("C10 the drawer body arrives from /api/topic/<id>, fetched once and cached", c10)
    run("C11 a hover preview neither reads nor fetches the body", c11)
    run("C12 a failed body read keeps the excerpt and adds a truthful note", c12)
    run("C13 the reader asks for ?view=list and the legacy read still carries bodies", c13)
    run("C14 a body-only topic reads only once its body is on screen", c14)
    run("C15 a failed body read leaves a body-only topic unread", c15)
    run("C16 a manual 未读 during the read survives the arriving body", c16)
    run("C17 a late body neither marks nor replaces the topic now on screen", c17)
    run("C18 the body read has a deadline and a later visit retries it", c18)
    run("C19 a stalled body reads as a deadline, never as a payload", c19)
    run("C20 an accepted snapshot drops a body read still in flight", c20)
    run("C21 the body cache evicts oldest first and stays bounded", c21)
    # ---------------------------------------------------------------- lazy-body races
    # A close/reopen, a repeated pinned open and an accepted snapshot all arrive while a detail
    # read is in flight. The request lifecycle must follow the VISIT (object identity), not just
    # the topic id, or a body lands on a visit that never subscribed to it - or a fresh visit
    # waits for a request that belongs to a snapshot it no longer shows.
    RACE_NEW_TEXT = "新的正文内容（刷新后的快照）。" * 20
    NEW_SHOWN = "(function(){var b=document.querySelector('#d-body');return !!(b && b.textContent.indexOf('新的正文内容') === 0);})()"

    def detail_reads():
        return [r["path"] for r in bc.StubHandler.requests if r["path"].startswith("/api/topic/")]

    def race_topic():
        return next(t for t in bc.StubHandler.state["state"]["topics"] if t["id"] == BODY_ONLY_ID)

    def state_gets():
        return len([r for r in bc.StubHandler.requests if r["path"] == "/api/state" and r["method"] == "GET"])

    def wait_for_snapshot(before: int, timeout: float = 8.0) -> bool:
        """Barrier: the refresh's pull has been served. A short settle then covers applyState, so
        the checks below observe a real snapshot boundary instead of racing one."""
        end = time.time() + timeout
        while time.time() < end:
            if state_gets() > before:
                time.sleep(0.4)
                return True
            time.sleep(0.1)
        return False

    def c24():
        # race A: close + explicit reopen of the SAME body-only topic while the read is pending
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 2.4}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        time.sleep(0.5)
        close_drawer(cdp)
        open_drawer_js(cdp, BODY_ONLY_ROW)          # explicit reopen, read still pending
        during = item_state(cdp, BODY_ONLY_ID)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=9.0)
        after = item_state(cdp, BODY_ONLY_ID)
        reads = detail_reads()
        cc.state["topic_delay_ids"] = {}
        return (
            len(reads) == 1 and during["read"] == 0 and bool(arrived) and after["read"] >= 1,
            f"reads={reads} read_before_content={during['read']} arrived={bool(arrived)} read_after={after['read']}",
        )

    def c25():
        # race B: the same reopen sequence, but the reader toggled 已读 then 未读 before the answer
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 2.4}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        click_js(cdp, "#d-read")                    # manual 已读
        time.sleep(0.2)
        click_js(cdp, "#d-read")                    # manual 未读 - the reader's last word
        time.sleep(0.2)
        manual = item_state(cdp, BODY_ONLY_ID)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=9.0)
        after = item_state(cdp, BODY_ONLY_ID)
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        cc.state["topic_delay_ids"] = {}
        return (
            manual["read"] == 0 and bool(arrived) and after["read"] == 0
            and shown.startswith("只有正文没有摘要的帖子内容"),
            f"after_manual_unread={manual['read']} arrived={bool(arrived)} read_after={after['read']} "
            f"body_shown={shown.startswith('只有正文没有摘要的帖子内容')}",
        )

    def c26():
        # race C: a repeated explicit open of the topic the drawer already pins, while pending
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 2.4}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        time.sleep(0.4)
        open_drawer_js(cdp, BODY_ONLY_ROW)          # same pinned topic again: still one request
        repeat_reads = len(detail_reads())
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=9.0)
        after = item_state(cdp, BODY_ONLY_ID)
        reads = detail_reads()
        cc.state["topic_delay_ids"] = {}
        return (
            repeat_reads == 1 and len(reads) == 1 and bool(arrived) and after["read"] >= 1,
            f"reads_at_repeat={repeat_reads} reads={len(reads)} arrived={bool(arrived)} read_after={after['read']}",
        )

    def c27():
        # race C, manual variant: 未读 survives both the repeated open and the arriving body
        cc = bc.StubHandler
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 2.4}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        click_js(cdp, "#d-read")
        time.sleep(0.2)
        click_js(cdp, "#d-read")                    # manual 未读
        time.sleep(0.2)
        open_drawer_js(cdp, BODY_ONLY_ROW)          # repeated pinned open must not undo it
        manual = item_state(cdp, BODY_ONLY_ID)
        arrived = wait_js(cdp, BODY_ONLY_SHOWN, timeout=9.0)
        after = item_state(cdp, BODY_ONLY_ID)
        cc.state["topic_delay_ids"] = {}
        return (
            manual["read"] == 0 and bool(arrived) and after["read"] == 0 and len(detail_reads()) == 1,
            f"after_repeat={manual['read']} arrived={bool(arrived)} read_after={after['read']} reads={len(detail_reads())}",
        )

    def c28():
        # race D: a refreshed snapshot lands while the old detail read is still pending
        cc = bc.StubHandler
        topic = race_topic()
        original = topic["body_text"]
        cc.state["topic_delay_ids"] = {BODY_ONLY_ID: 6.0}   # the old read will not answer soon
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            cc.state["topic_delay_ids"] = {}
            return False, "could not re-enter the owner token"
        open_drawer_js(cdp, BODY_ONLY_ROW)                 # read #1, belongs to the old snapshot
        if not drawer_visible_after(cdp, BODY_ONLY_ID):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body-only topic"
        time.sleep(0.4)
        topic["body_text"] = RACE_NEW_TEXT                 # local stub data change only
        gets_before = state_gets()
        js(cdp, "document.querySelector('#refresh').click()")
        landed = wait_for_snapshot(gets_before)            # barrier: the new snapshot is in
        cc.state["topic_delay_ids"] = {}                   # the current-snapshot read answers at once
        close_drawer(cdp)
        start = time.time()
        open_drawer_js(cdp, BODY_ONLY_ROW)                 # must request NOW, not wait for read #1
        arrived = wait_js(cdp, NEW_SHOWN, timeout=5.0)
        elapsed = time.time() - start
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        reads = detail_reads()
        state = item_state(cdp, BODY_ONLY_ID)
        time.sleep(0.6)                                    # the old request would answer about here
        kept = js(cdp, "document.querySelector('#d-body').textContent")
        topic["body_text"] = original
        return (
            bool(landed) and len(reads) == 2 and bool(arrived) and elapsed < 4.0
            and shown == RACE_NEW_TEXT and kept == RACE_NEW_TEXT and state["read"] >= 1,
            f"snapshot_landed={bool(landed)} reads={len(reads)} elapsed={elapsed:.2f}s "
            f"new_shown={shown == RACE_NEW_TEXT} kept_after_old_pending={kept == RACE_NEW_TEXT} read={state['read']}",
        )

    def c29():
        # race D, manual variant, under the settled outside-click contract: the 刷新 click
        # dismisses the pinned preview, so the visit that carried the manual 未读 ends with it.
        # The refreshed snapshot is then explicitly re-opened, and the reader's last word is
        # applied to THAT visit. What this still pins down: the new snapshot's body is fetched
        # for the current visit (never served by the old in-flight request), the old request
        # cannot mark or replace anything, and 未读 survives the arriving body.
        cc = bc.StubHandler
        topic = next(t for t in cc.state["state"]["topics"] if t["id"] == body_id)
        original = topic["body_text"]
        cc.state["topic_delay_ids"] = {body_id: 6.0}
        cc.requests.clear()
        fresh_page(cdp, url, width=1280, height=900, settle_s=2.2)
        if not ensure_owner(cdp):
            cc.state["topic_delay_ids"] = {}
            return False, "could not re-enter the owner token"
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')   # read #1: the old snapshot
        if not drawer_visible_after(cdp, body_id):
            cc.state["topic_delay_ids"] = {}
            return False, "drawer did not open for the body topic"
        topic["body_text"] = RACE_NEW_TEXT
        gets_before = state_gets()
        js(cdp, "document.querySelector('#refresh').click()")
        landed = wait_for_snapshot(gets_before)
        dismissed = not drawer_open(cdp)
        cc.state["topic_delay_ids"] = {}          # the current-snapshot read answers at once
        open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')   # explicit reopen
        click_js(cdp, "#d-read")                  # the reader's last word, for this visit
        time.sleep(0.2)
        manual = item_state(cdp, body_id)
        arrived = wait_js(cdp, NEW_SHOWN, timeout=5.0)
        after = item_state(cdp, body_id)
        shown = js(cdp, "document.querySelector('#d-body').textContent")
        reads = len(detail_reads())
        topic["body_text"] = original
        return (
            bool(landed) and dismissed and manual["read"] == 0 and bool(arrived)
            and after["read"] == 0 and shown == RACE_NEW_TEXT and reads == 2,
            f"snapshot_landed={bool(landed)} preview_dismissed_by_refresh={dismissed} "
            f"after_manual_unread={manual['read']} arrived={bool(arrived)} read_after={after['read']} "
            f"new_shown={shown == RACE_NEW_TEXT} reads={reads}",
        )

    run("C22 acceptance RED: ~347 KB at ~23.4 KB/s fails the old 12 s bound", c22)
    run("C23 acceptance GREEN: the same payload completes under the shipped 30 s bound", c23)
    run("C24 race A: a close+reopen while the read is pending still reads once the body lands", c24)
    run("C25 race B: manual 未读 survives the close+reopen and the arriving body", c25)
    run("C26 race C: a repeated pinned open is one request and still reads on arrival", c26)
    run("C27 race C manual: 未读 survives the repeated pinned open and the body", c27)
    run("C28 race D: a refreshed snapshot request starts now and the old one cannot replace it", c28)
    run("C29 race D manual: the refreshed snapshot reads for the current visit, 未读 survives its body", c29)
    screenshot(cdp, shots, "reading-desktop-1280-real.png")


def phase_d(cdp, base: str, shots: Path) -> None:
    url = f"{base}/index.html?fixture=1"
    for width, height, mobile, touch in ((1440, 900, False, False), (390, 844, True, True), (360, 780, True, True)):
        for dark in (False, True):
            open_page(cdp, url, width=width, height=height, mobile=mobile, touch=touch, dark=dark)

            def wrap_check(width=width, dark=dark):
                lines = [title_lines(cdp, s) for s in (".brand", ".brand__sub", "#tab-all", "#tab-picked", "#tab-queue")]
                # the tabs are display:none on desktop: only rendered elements are measured
                visible = [n for n in lines if n is not None]
                overflow = js(cdp, "document.documentElement.scrollWidth - window.innerWidth")
                scheme = js(cdp, "getComputedStyle(document.body).backgroundColor")
                return (
                    len(visible) >= 1 and all(n == 1 for n in visible) and overflow <= 1
                ), f"lines={lines} measured={len(visible)} overflow={overflow} bg={scheme}"

            run(f"D  {width}px {'dark' if dark else 'light'}: title/tabs on one line, no overflow", wrap_check)
            screenshot(cdp, shots, f"reading-{width}-{'dark' if dark else 'light'}.png")
    # the drawer with an open, read preview (desktop, light)
    open_page(cdp, url, width=1440, height=900)
    js(cdp, "document.querySelector('.cell--c1 .item').click()")
    time.sleep(0.6)
    screenshot(cdp, shots, "reading-drawer-desktop.png")


def fixture_facts() -> dict:
    data = json.loads((PUBLIC / "fixtures" / "state.sample.json").read_text(encoding="utf-8"))
    topics = data["topics"]
    return {
        "topics": topics,
        "rejected": [t["id"] for t in topics if t["state"] == "rejected"],
        "picked": [t["id"] for t in topics if t["state"] == "picked"],
        "pending_with_body": [t["id"] for t in topics if t["state"] == "pending" and (t.get("body_text") or "").strip()],
        "no_body": next(t["id"] for t in topics if not (t.get("body_text") or "").strip()),
        "body_text_of": {t["id"]: (t.get("body_text") or "") for t in topics},
        "excerpt_of": {t["id"]: (t.get("excerpt") or "") for t in topics},
    }


def restart_browser(proc, devtools_port: int, profile: Path):
    """A fresh browser between phases: no leaked tab state, no half-dead websocket."""
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    proc = bc.launch_chromium(devtools_port, profile, "about:blank")
    time.sleep(0.6)
    return proc, bc.CDP(bc.page_ws(devtools_port))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", default=str(DEFAULT_SHOTS))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-layout", action="store_true", help="skip phase D (the width sweep)")
    args = parser.parse_args()
    shots = Path(args.shots)

    if not Path(bc.CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    fixtures = fixture_facts()
    tmp = Path(tempfile.mkdtemp(prefix="reading-ui-"))
    profile = tmp / "profile"
    site = build_site(tmp, "")
    bc.StubHandler.state = {
        "state": json.loads((PUBLIC / "fixtures" / "state.sample.json").read_text(encoding="utf-8")),
        "health": {"status": "ok", "attention": {"needed": False, "reason": "", "kind": None}, "auth": {"writes": "owner", "token_configured": True}},
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
    write_alt_build(site, base)   # an apiBase-carrying copy for the namespace check
    devtools_port = bc._free_port()
    proc = None
    cdp = None
    try:
        proc = bc.launch_chromium(devtools_port, profile, "about:blank")
        cdp = bc.CDP(bc.page_ws(devtools_port))

        def run_phase(label: str, fn) -> None:
            """Run one phase; if the DevTools socket drops, re-attach once and redo it.
            The checks recorded by the aborted attempt are dropped, so the final count
            stays honest."""
            nonlocal cdp, proc
            start = len(RESULTS)
            for attempt in (1, 2):
                print(label)
                try:
                    fn(cdp)
                    return
                except RuntimeError as err:
                    if attempt == 2 or "websocket" not in str(err):
                        raise
                    print(f"      (devtools connection dropped: {err}; restarting the browser)")
                    del RESULTS[start:]
                    try:
                        cdp.close()
                    except Exception:
                        pass
                    proc, cdp = restart_browser(proc, devtools_port, profile)

        run_phase(f"--- phase A: fixture mode, desktop 1440x900 at {base}/index.html?fixture=1 ---",
                  lambda c: phase_a(c, base, shots, fixtures))
        proc, cdp = restart_browser(proc, devtools_port, profile)
        run_phase("--- phase B: fixture mode, mobile 390x844 (touch) ---",
                  lambda c: phase_b(c, base, fixtures))
        proc, cdp = restart_browser(proc, devtools_port, profile)
        run_phase("--- phase C: real mode against the stub API, 1280x900 ---",
                  lambda c: phase_c(c, base, fixtures, shots))
        proc, cdp = restart_browser(proc, devtools_port, profile)

        def a13():
            open_page(cdp, f"{base}/alt/index.html?fixture=1", width=1280, height=900)
            js(cdp, "try{localStorage.clear();}catch(e){}")
            cdp.call("Page.reload")
            time.sleep(1.8)
            key = read_key(cdp)
            api_base = js(cdp, "window.LINUXDO_AI_DEBUG.apiBase")
            body_id = fixtures["pending_with_body"][0]
            open_drawer_js(cdp, f'.cell--c1 .item[data-id="{body_id}"]')
            wait_js(cdp, f'document.querySelector(\'.item[data-id="{body_id}"]\').classList.contains("is-read")')
            keys = js(cdp, "Object.keys(localStorage)")
            read_keys = [k for k in keys if k.startswith("linuxdo-ai.read")]
            stored = read_set(cdp)
            return (
                api_base != ""
                and len(read_keys) == 1
                and read_keys[0].endswith(api_base)
                and isinstance(stored, list) and body_id in stored,
                f"apiBase={api_base!r} read_keys={read_keys} keys={keys} stored={stored}",
            )

        def a13_phase(c):
            run("A13 the read store is namespaced by the trusted apiBase", a13)

        run_phase("--- phase A2: namespace scope of the read store (explicit apiBase) ---", a13_phase)
        if not args.skip_layout:
            run_phase("--- phase D: width sweep 360/390/1440, light+dark ---",
                      lambda c: phase_d(c, base, shots))
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
