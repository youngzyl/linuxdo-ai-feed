#!/usr/bin/env python3
"""U09 — the mobile preview sheet has two sizes: reading (default, ~96dvh) and peek (80vh).

The handle at the sheet top is the toggle (`#d-size`); the preference is browser-local
(`linuxdo-ai.drawer-size` = full|peek). The toggle only resizes the sheet: it must not
open/close the drawer, mark read, fetch, or change the pin. The scrim strip above the
sheet still closes on tap in both sizes; desktop keeps the 400px right panel and the
44vh body cap and never shows the toggle.

Usage: TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1 python3 tests/browser_drawer_size.py
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

ROW = ".cell--c1 .item"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""))
    return bool(ok)


def sheet_state(cdp) -> dict:
    """The sheet's observable size state: geometry, the toggle control, the pref."""
    return dens.js(cdp, """(function(){
      var d = document.querySelector('#drawer');
      var b = document.querySelector('#d-size');
      var body = document.querySelector('#d-body');
      var pref = null;
      try { pref = localStorage.getItem('linuxdo-ai.drawer-size'); } catch (e) { pref = 'ERR'; }
      var r = d.getBoundingClientRect();
      return {
        hidden: d.hidden,
        open: d.hidden === false,
        ratio: Math.round((r.height / window.innerHeight) * 1000) / 1000,
        isPeek: d.classList.contains('is-peek'),
        toggle: !!b,
        expanded: b ? b.getAttribute('aria-expanded') : null,
        label: b ? b.getAttribute('aria-label') : null,
        title: b ? b.getAttribute('title') : null,
        pref: pref,
        rowTitle: (document.querySelector('#d-title').textContent || '').slice(0, 30),
        bodyFont: body ? parseFloat(getComputedStyle(body).fontSize) : null,
        current: document.querySelectorAll('.item.is-current').length
      };
    })()""") or {}


TAP_TARGET = """(function(){
  var n = document.querySelector(%s);
  if (!n) return {missing: true};
  n.scrollIntoView({block: 'center', inline: 'nearest'});
  var r = n.getBoundingClientRect();
  var x = Math.round(r.left + r.width / 2);
  var y = Math.round(r.top + r.height / 2);
  var el = document.elementFromPoint(x, y);
  return {x: x, y: y, hits: !!el && (el === n || n.contains(el)),
          visible: r.bottom > 1 && r.top < window.innerHeight - 1 && r.width > 0};
})()"""


def tap_row(cdp, selector: str = ROW) -> tuple[bool, dict]:
    """A real touch tap on a row (scrolled into view first); waits for the sheet."""
    point = dens.js(cdp, TAP_TARGET % json.dumps(selector)) or {}
    ok, pt = dens._tap_at(cdp, point)
    if ok:
        dens.wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
        time.sleep(0.7)
    return ok, pt


def tap_toggle(cdp) -> tuple[bool, dict]:
    ok, pt = dens.real_tap(cdp, "#d-size")
    if ok:
        time.sleep(0.7)
    return ok, pt


def mobile_page(cdp, url: str) -> None:
    dens.open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=2.2)


def main() -> int:
    if not Path(bc.CHROMIUM).exists():
        print("FAIL  chromium not found")
        return 2

    dens.reset_stub()
    tmp = Path(tempfile.mkdtemp(prefix="drawer-size-"))
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

        # ------------------------------------------------ mobile 390x844 ------
        dens.open_page(cdp, url, width=390, height=844, mobile=True, touch=True, settle_s=1.6)
        dens.js(cdp, "try{localStorage.clear();}catch(e){} true")
        mobile_page(cdp, url)
        dens.arm_errors(cdp)

        tapped1, pt1 = tap_row(cdp)
        st1 = sheet_state(cdp)
        check(tapped1 and st1.get("open") and (st1.get("ratio") or 0) >= 0.92
              and st1.get("toggle") and st1.get("expanded") == "true"
              and st1.get("label") == "收起预览" and st1.get("title") == "收起预览"
              and not st1.get("isPeek") and (st1.get("bodyFont") or 0) >= 16
              and st1.get("current", 0) >= 1,
              "U09 T1 default open on a row tap is reading (>=0.92), toggle says 收起预览, body font >= 16",
              f"tap={pt1} state={json.dumps(st1, ensure_ascii=False)}")

        # T5 measurement wraps the toggle: no /api request, no read change.
        mark = dens.req_mark()
        reads_before = dens.read_marks(cdp)
        tapped2, pt2 = tap_toggle(cdp)
        st2 = sheet_state(cdp)
        added = dens.api_calls(dens.reqs_since(mark))
        reads_after = dens.read_marks(cdp)

        check(tapped2 and st2.get("open") and (st2.get("ratio") or 1) <= 0.85
              and st2.get("expanded") == "false"
              and st2.get("label") == "展开阅读" and st2.get("title") == "展开阅读"
              and st2.get("pref") == "peek" and st2.get("isPeek")
              and st2.get("current", 0) >= 1
              and st2.get("rowTitle") == st1.get("rowTitle"),
              "U09 T2 tap #d-size peeks (<=0.85), flips to 展开阅读, pref=peek, sheet stays open",
              f"tap={pt2} state={json.dumps(st2, ensure_ascii=False)}")

        check(tapped2 and added == [] and reads_after == reads_before,
              "U09 T5 the size toggle adds no /api request and no read mark",
              f"added={added} reads_changed={reads_after != reads_before}")

        # T3: reload keeps peek; tapping again restores reading; pref persists as full.
        mobile_page(cdp, url)
        tapped3, pt3 = tap_row(cdp)
        st3 = sheet_state(cdp)
        tapped4, pt4 = tap_toggle(cdp)
        st3b = sheet_state(cdp)
        check(tapped3 and st3.get("open") and (st3.get("ratio") or 1) <= 0.85
              and st3.get("pref") == "peek" and st3.get("expanded") == "false"
              and tapped4 and (st3b.get("ratio") or 0) >= 0.92
              and st3b.get("expanded") == "true" and st3b.get("pref") == "full",
              "U09 T3a reload keeps peek; tapping again restores reading (pref=full)",
              f"peek={json.dumps(st3, ensure_ascii=False)} full={json.dumps(st3b, ensure_ascii=False)}")

        mobile_page(cdp, url)
        tapped5, pt5 = tap_row(cdp)
        st3c = sheet_state(cdp)
        check(tapped5 and st3c.get("open") and (st3c.get("ratio") or 0) >= 0.92
              and st3c.get("pref") == "full" and st3c.get("expanded") == "true",
              "U09 T3b a second reload keeps reading (size pref=full)",
              f"tap={pt5} state={json.dumps(st3c, ensure_ascii=False)}")

        # T4: reading top strip is the scrim; a real tap there closes; 关闭 also closes.
        strip = dens.js(cdp, """(function(){
          var s = document.querySelector('#scrim');
          if (!s || s.hidden) return {missing: true};
          var x = Math.round(window.innerWidth / 2), y = 8;
          var el = document.elementFromPoint(x, y);
          return {x: x, y: y, scrim: el === s, hits: el === s, visible: true,
                  at: el ? String(el.id || el.className || el.tagName).slice(0, 40) : null};
        })()""") or {}
        if strip.get("scrim"):
            dens._tap_at(cdp, strip)
        closed = bool(dens.wait_js(cdp, "document.querySelector('#drawer').hidden === true", timeout=4.0))
        time.sleep(0.2)
        st4 = sheet_state(cdp)
        check((st3c.get("ratio") or 0) >= 0.92 and strip.get("scrim") and closed and st4.get("hidden"),
              "U09 T4 reading keeps a top scrim strip; a real tap there closes the sheet",
              f"strip={strip} closed={closed} state={json.dumps(st4, ensure_ascii=False)}")

        tapped6, pt6 = tap_row(cdp)
        okc, ptc = dens.real_tap(cdp, "#d-close")
        time.sleep(0.8)
        closed2 = bool(dens.wait_js(cdp, "document.querySelector('#drawer').hidden === true", timeout=4.0))
        check(tapped6 and okc and closed2,
              "U09 T4b the 关闭 control still closes the sheet",
              f"reopen={pt6} close={ptc} closed={closed2}")

        # ------------------------------------------------ desktop 1280x900 -----
        # loaded with the peek pref: the class must have no desktop effect at all
        dens.js(cdp, "try{localStorage.setItem('linuxdo-ai.drawer-size','peek');}catch(e){} true")
        dens.open_page(cdp, url, width=1280, height=900, mobile=False, touch=False, settle_s=2.2)
        dens.js(cdp, "document.querySelector('.cell--c1 .item').scrollIntoView({block:'center'})")
        time.sleep(0.25)
        okd, ptd = dens.real_click(cdp, ROW, pause=0.5)
        dens.wait_js(cdp, "document.querySelector('#drawer').hidden === false", timeout=4.0)
        time.sleep(0.5)
        desk = dens.js(cdp, """(function(){
          var d = document.querySelector('#drawer');
          var b = document.querySelector('#d-size');
          var body = document.querySelector('#d-body');
          var dr = d.getBoundingClientRect();
          return {
            open: d.hidden === false,
            width: Math.round(dr.width),
            right: Math.round(window.innerWidth - dr.right),
            toggle: b ? {display: getComputedStyle(b).display,
                         w: Math.round(b.getBoundingClientRect().width),
                         h: Math.round(b.getBoundingClientRect().height)} : null,
            bodyMaxH: parseFloat(getComputedStyle(body).maxHeight),
            innerH: window.innerHeight
          };
        })()""") or {}
        tgl = desk.get("toggle") or {}
        check(okd and desk.get("open") and desk.get("width") == 400 and desk.get("right") == 0
              and tgl.get("display") == "none"
              and tgl.get("w") == 0 and tgl.get("h") == 0
              and abs((desk.get("bodyMaxH") or 0) - 0.44 * (desk.get("innerH") or 0)) <= 2
              and not dens.errors(cdp),
              "U09 T6 desktop keeps the 400px panel and 44vh body; #d-size stays hidden",
              f"click={ptd} state={json.dumps(desk, ensure_ascii=False)} errors={dens.errors(cdp)}")

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
