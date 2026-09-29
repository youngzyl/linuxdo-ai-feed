#!/usr/bin/env python3
"""U08 — desktop masthead toggle collapses the left 全部 lane.

Mobile tabs already switch lists. This control is desktop-only: it must not change
mobile tab behavior, must not mark read, fetch, open a preview, or change bookmarks.
The preference is browser-local (`linuxdo-ai.all-lane` = show|hide) and is not a
request destination.

What is asserted is observable geometry, never a test-only probe:
  * desktop click on #all-lane hides .cell--c1 and the first colhead label
  * .cell--c2 stays visible
  * aria-pressed flips (true = lane visible)
  * reload keeps the collapsed preference
  * a second click restores the lane
  * the toggle adds no /api request

Usage: TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1 python3 tests/browser_all_lane.py
Exit code 0 = every check passed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import browser_check as bc  # noqa: E402
import browser_density_position as dens  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""))
    return bool(ok)


def lane_visible(cdp) -> dict:
    """Observable geometry of the three lanes and the masthead control."""
    return dens.js(cdp, """(function(){
      function shown(sel) {
        var n = document.querySelector(sel);
        if (!n) return {exists: false, shown: false};
        var r = n.getBoundingClientRect();
        var cs = getComputedStyle(n);
        return {exists: true,
                shown: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0,
                display: cs.display, w: Math.round(r.width), h: Math.round(r.height)};
      }
      var btn = document.querySelector('#all-lane');
      var head = document.querySelector('.colhead');
      var first = head ? head.querySelector('.label') : null;
      var labels = head ? Array.prototype.map.call(head.querySelectorAll('.label'), function(n){
        var r = n.getBoundingClientRect();
        var cs = getComputedStyle(n);
        return {text: (n.textContent || '').trim(),
                shown: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0};
      }) : [];
      var pref = null;
      try { pref = localStorage.getItem('linuxdo-ai.all-lane'); } catch (e) { pref = 'ERR'; }
      var cols = getComputedStyle(document.querySelector('#board')).gridTemplateColumns;
      return {
        button: !!btn,
        pressed: btn ? btn.getAttribute('aria-pressed') : null,
        label: btn ? (btn.textContent || '').trim() : null,
        title: btn ? btn.getAttribute('title') : null,
        nextToDensity: !!(btn && btn.previousElementSibling && btn.previousElementSibling.id === 'density'),
        pref: pref,
        boardCollapsed: document.querySelector('#board').classList.contains('is-all-collapsed'),
        headCollapsed: !!(head && head.classList.contains('is-all-collapsed')),
        cols: cols,
        c1: shown('.cell--c1'),
        c2: shown('.cell--c2'),
        c3: shown('.cell--c3'),
        labels: labels,
        firstLabel: first ? (first.textContent || '').trim() : null,
        tab: document.querySelector('#board').getAttribute('data-tab'),
        drawerOpen: document.querySelector('#drawer').hidden === false
      };
    })()""") or {}


def main() -> int:
    if not Path(bc.CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    dens.reset_stub()
    tmp = Path(tempfile.mkdtemp(prefix="all-lane-"))
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
        dens.open_page(cdp, url, width=1280, height=900, settle_s=1.6)
        dens.js(cdp, "try{localStorage.clear();}catch(e){} true")
        dens.open_page(cdp, url, width=1280, height=900, settle_s=2.0)
        dens.arm_errors(cdp)
        dens.park_mouse(cdp)

        before = lane_visible(cdp)
        check(before.get("button") and before.get("label") == "全部"
              and before.get("pressed") == "true"
              and before.get("nextToDensity")
              and before.get("title") == "收起全部栏"
              and before.get("pref") in (None, "show")
              and before.get("c1", {}).get("shown")
              and before.get("c2", {}).get("shown")
              and not before.get("boardCollapsed"),
              "U08 default: #all-lane sits next to #density, lane visible, aria-pressed=true",
              f"state={json.dumps(before, ensure_ascii=False)}")

        mark = dens.req_mark()
        read_before = dens.read_marks(cdp)
        ok, point = dens.real_click(cdp, "#all-lane", pause=0.4)
        after = lane_visible(cdp)
        added = dens.api_calls(dens.reqs_since(mark))
        read_after = dens.read_marks(cdp)
        c1_hidden = after.get("c1", {}).get("exists") and not after.get("c1", {}).get("shown")
        labels = after.get("labels") or []
        first_hidden = bool(labels) and labels[0].get("text") == "全部" and not labels[0].get("shown")
        rest_shown = len(labels) >= 3 and labels[1].get("shown") and labels[2].get("shown")
        check(ok and c1_hidden and first_hidden and rest_shown
              and after.get("c2", {}).get("shown") and after.get("c3", {}).get("shown")
              and after.get("pressed") == "false"
              and after.get("title") == "显示全部栏"
              and after.get("pref") == "hide"
              and after.get("boardCollapsed") and after.get("headCollapsed")
              and after.get("drawerOpen") is False
              and added == []
              and read_after == read_before
              and not dens.errors(cdp),
              "U08 desktop click hides .cell--c1 and the first colhead label, c2 stays, no /api",
              f"click={point} state={json.dumps(after, ensure_ascii=False)} api={added} "
              f"read_changed={read_after != read_before} errors={dens.errors(cdp)}")

        dens.open_page(cdp, url, width=1280, height=900, settle_s=2.0)
        kept = lane_visible(cdp)
        check(kept.get("pressed") == "false" and kept.get("pref") == "hide"
              and kept.get("c1", {}).get("exists") and not kept.get("c1", {}).get("shown")
              and kept.get("c2", {}).get("shown")
              and kept.get("boardCollapsed"),
              "U08 reload keeps the collapsed lane",
              f"state={json.dumps(kept, ensure_ascii=False)}")

        dens.park_mouse(cdp)
        ok2, point2 = dens.real_click(cdp, "#all-lane", pause=0.4)
        restored = lane_visible(cdp)
        check(ok2 and restored.get("pressed") == "true"
              and restored.get("pref") == "show"
              and restored.get("title") == "收起全部栏"
              and restored.get("c1", {}).get("shown")
              and restored.get("c2", {}).get("shown")
              and not restored.get("boardCollapsed"),
              "U08 second click restores the 全部 lane",
              f"click={point2} state={json.dumps(restored, ensure_ascii=False)}")

        # Collapse again, then shrink to the mobile breakpoint. data-tab rules must still
        # show the 全部 list; the desktop collapse class must not hide that tab's content.
        dens.js(cdp, "try{localStorage.setItem('linuxdo-ai.all-lane','hide');}catch(e){} true")
        dens.open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=2.0)
        mobile = lane_visible(cdp)
        check(mobile.get("pressed") == "false"
              and mobile.get("pref") == "hide"
              and not mobile.get("boardCollapsed")
              and mobile.get("tab") == "all"
              and mobile.get("c1", {}).get("shown")
              and mobile.get("c1", {}).get("display") == "block",
              "U08 mobile 全部 tab still shows c1 even when the desktop preference is hide",
              f"state={json.dumps(mobile, ensure_ascii=False)}")

        # Empty 精选 on the stub only: demote every picked topic, then reload collapsed.
        # The notice must sit under 精选 (column 1 of the 2-col board), not over 全部's old slot.
        state = bc.StubHandler.state
        for topic in state["state"]["topics"]:
            if topic.get("state") == "picked":
                topic["state"] = "pending"
        state["queue"] = []
        dens.js(cdp, "try{localStorage.setItem('linuxdo-ai.all-lane','hide');}catch(e){} true")
        dens.open_page(cdp, url, width=1280, height=900, mobile=False, touch=False, settle_s=2.0)
        empty = dens.js(cdp, """(function(){
          function box(n) {
            if (!n) return null;
            var r = n.getBoundingClientRect();
            var cs = getComputedStyle(n);
            return {shown: !n.hidden && cs.display !== 'none' && cs.visibility !== 'hidden'
                            && r.width > 0 && r.height > 0,
                    display: cs.display, hidden: !!n.hidden,
                    gridColumn: cs.gridColumn,
                    left: Math.round(r.left), right: Math.round(r.right),
                    top: Math.round(r.top), bottom: Math.round(r.bottom)};
          }
          function overlaps(a, b) {
            if (!a || !b || !a.shown || !b.shown) return false;
            return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
          }
          var c1 = document.querySelector('#empty-c1');
          var c2 = document.querySelector('#empty-c2');
          var cell = document.querySelector('.cell--c2');
          var empties = document.querySelector('#empties');
          var b1 = box(c1), b2 = box(c2), bc2 = box(cell);
          return {c1: b1, c2: b2, cell: bc2,
                  emptiesCollapsed: !!(empties && empties.classList.contains('is-all-collapsed')),
                  emptiesCols: empties ? getComputedStyle(empties).gridTemplateColumns : null,
                  overlap: overlaps(b2, bc2)};
        })()""") or {}
        c2 = empty.get("c2") or {}
        c1 = empty.get("c1") or {}
        check(empty.get("emptiesCollapsed")
              and c2.get("shown") is True
              and str(c2.get("gridColumn") or "").startswith("1")
              and empty.get("overlap") is False
              and c1.get("shown") is not True,
              "U08 collapsed empty 精选 notice sits in column 1 and does not cover .cell--c2",
              f"geom={json.dumps(empty, ensure_ascii=False)}")

        failed = [label for ok_, label in RESULTS if not ok_]
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
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
