# Mobile reading-sheet release — 2026-10-04 CST

## Published artifact

- Reader: https://youngzyl.github.io/linuxdo-ai-feed/
- Source: `ba090ad003d7cfaca056ca2fb999a5088b45a7b2`.
- Pages: `87045586d0e1f172e45c5779c084c9d64e508530`.
- Pages API: target commit `built`, error message null, updated `2026-10-03T17:06:49Z`.
- Published app SHA-256: `d328b69e9de72718ea37f18b2a5be128c31d854aa537b85ea5c510931c99645e`.

The public app, index, runtime config and stylesheet were fetched over verified TLS and matched the approved bundle byte-for-byte; the reader root URL also matched the index. The Pages tree contains exactly `.nojekyll`, `app.js`, `index.html`, `runtime-config.js`, and `styles.css`.

## Behavior

On mobile the preview opens as a reading sheet filling 96% of the viewport (96vh, with 96dvh where supported), and the body text is 16px/1.78. The drag-handle bar at the top of the sheet is now a real button: tap it to drop back to the old 80vh peek sheet, tap again to return. The choice is remembered per browser (`linuxdo-ai.drawer-size`) and touches no read, bookmark or filter state. The thin strip above the sheet still closes the preview on tap, and 关闭 / Escape are unchanged.

Desktop is untouched: the preview stays a 400px right panel with the 44vh body cap and no handle button.

## Evidence

- RED before implementation: the new suite failed 1/8 against the old 80vh sheet (`ratio 0.423`, body font 14.5, no toggle).
- Final local gates, re-run by lead: drawer-size 8/8, all-lane 6/6, preview pin 22/22, reading 54/54, build/reader-contract units 37 OK, `node --check` OK.
- Live read-only smoke against the published site, real rows, real touch/mouse, non-GET guard armed before the first navigation: **16/16**. Reading sheet measured 0.96 of the viewport, peek 0.8, body font 16px, scrim tap closes, size survives a real reload, desktop panel 400px with handle hidden, all requests GET to Pages or `bwgca.youngzyl.me:8444` only. Evidence: `/workspace/linuxdo-drawer-size-release/live-smoke.json`.

No backend, API, Caddy or deployment change. The earlier live harness failures (one browser reused across viewports; measuring a closed desktop drawer) were harness bugs, fixed before the final run; they changed no product code.
