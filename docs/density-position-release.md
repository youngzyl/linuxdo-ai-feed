# Density-position release — 2026-09-26 CST

## Published artifact

- Reader: https://youngzyl.github.io/linuxdo-ai-feed/
- Source: `303fef1891368fcba6a5a1ca562e2d75e321e7a2`.
- Pages: `2e73e435c735a621501a72e44f6cb311efe0cdac`.
- Pages API: target commit `built`, error message null, updated `2026-09-25T19:30:43Z`.
- Frozen source diff SHA-256: `e1cd3fe41ea8938a436dfef663acc9a9dde2d8cf375dd891acbf20c2c545d3b6`.
- Published app SHA-256: `291ba7bd9f88ea010f374b9f055ff416ed29d81aa62c0fb656f0344a7bc10e81`.

The public app, index, runtime config and stylesheet were fetched over verified TLS and matched the approved bundle byte-for-byte. The reader root URL also matched the index. The Pages tree contains exactly `.nojekyll`, `app.js`, `index.html`, `runtime-config.js`, and `styles.css`.

## Behavior and boundaries

Density changes retain an in-memory article ID, originating lane and viewport offset. A saved topic's duplicate in another lane is not a substitute for the originating instance. Returning to the non-sticky header freezes the remembered reading position; subsequent deliberate browsing can advance it after the previous article leaves view. The toggle flushes pending viewport sampling before planning, compensates synchronously and skips density-only FLIP motion.

Every density reflow cancels pending passive hover and guards layout-generated hover. Typed mouse movement resumes it; touch movement does not. Where PointerEvent is unavailable, the declared conservative fallback requires an explicit click/tap/Enter/Space after reflow rather than guessing whether a compatibility mouse event came from a finger. Explicit preview activation remains available.

No new UI control, read-history key, API endpoint or backend deployment. Density/sampling do not mark read, save a topic or teach preferences. Mobile geometry is unchanged and receives no restoration scroll. Refresh/reopen continuation, pagination and the proposed action/RSS redesign are not included.

The network API remains `https://tcstw.youngzyl.me:8444/linuxdo-api`; browser-local read state retains the trusted old `https://tcstw.youngzyl.me:8443/linuxdo-api` namespace. No Caddy, firewall, collector, model or shared delegation configuration was changed by this release.

## Review and regression evidence

Implementation used isolated fixtures and RED/GREEN browser tests. Independent static reviews used fresh no-tools `openai-codex / gpt-6-sol / medium` sessions, without changing the shared delegation default. Full-diff reviews found defects that were fixed; the final targeted delta and preceding-blocker closure review passed against the frozen hash above, with no security concerns or logic errors. Static review is not a test rerun.

Final source gates:

- Density/browser: **42/42**.
- Preview pin/browser: **22/22**.
- Reading/browser: **54/54**.
- Focused build/reader-contract unit tests: **37**, OK.
- `node --check public/app.js`: exit 0.
- Source app and five-file candidate manifest: exact byte/hash match.

A non-blocking review suggestion remains: the legacy E10 test checks the expected preview opens but does not separately assert pin persistence. The existing modern pin suite and the shared explicit-activation path remain covered/reviewed.

## Public read-only acceptance

The final published-site run passed **31/31 checks**, including three asset gates rather than 31 distinct UX scenarios. Desktop and mobile emulation used separate fresh anonymous browser profiles. GET-only interception was installed before each first navigation; no owner token, production fixture, test bookmark or preference vote was used.

- The real existing bookmarked topic `2933507` was activated in its original selected lane, not its bookmark copy.
- Compact to gap retained its viewport position with a `-0.5 px` delta; gap to compact with `+0.5 px`; both stayed settled.
- Neither toggle opened a passive preview or changed the disposable profile's read state.
- Mobile tap, scrim dismissal, density activation, finger movement without hover and subsequent deliberate pinned preview all passed.
- All 12 distinct method+URL pairs were GET; all eight API method+URL pairs used port 8444. No blocked write attempts, console errors or failed app requests.
- Desktop and mobile screenshots were inspected. This is Chromium desktop/phone emulation, not a claim about an observed physical-phone reload.

Earlier harness attempts are retained: a pointer left over a row during programmatic header navigation opened an ordinary hover before the density click; the real pointer route was corrected. Reusing the deeply-scrolled desktop target then timed out during mobile setup before mobile API requests. A fresh mobile probe passed 7/7, and the final runner uses independent browser profiles. No product code was changed to bypass these harness failures.

Local evidence: `/workspace/linuxdo-density-evidence/cycle4-*.log`, and `/workspace/linuxdo-density-release/{review-freeze.json,codex-review.json,publish-result.json,public-verification.json,live-smoke/live-smoke.json}`. Earlier live attempts remain under `live-smoke-attempt1/` and `live-smoke-attempt2/`; the isolated mobile probe is under `mobile-isolation/`.
