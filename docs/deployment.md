# Permanent deployment — 2026-09-22 (CST)

## Live endpoints and ownership

- Reader: https://youngzyl.github.io/linuxdo-ai-feed/
- API base: https://tcstw.youngzyl.me:8444/linuxdo-api
- Health: https://tcstw.youngzyl.me:8444/linuxdo-api/health
- GitHub Pages source: `gh-pages`, `/`, HTTPS enforced. Current static release: `0fffb302c2d4009bea56a1a522cb6082af2b8f18` (2026-09-25 cutover; earlier deployment evidence below is historical).
- Backend: `young@tcstw.youngzyl.me`, `/home/young/services/linuxdo-ai`.
- User unit: `linuxdo-ai-feed.service`, enabled; user lingering is enabled.
- Source: `/workspace/linuxdo-ai`, repository `youngzyl/linuxdo-ai-feed`.

The backend is independent of the Hermes tool container. The retired local collector and its supervisor were stopped after a final state copy. Its files remain a rollback snapshot, not an active authority. The old temporary Cloudflare tunnel was stopped after the permanent frontend/API became usable. Never revive a second collector against the retired local state.

## Access model

Anonymous visitors can read the feed, previews and queue. Writes (queue, feedback, refresh) and raw feedback/failure reads require the owner bearer token. A missing configured token fails closed. Browser Origin checks are an exact allowlist, not a wildcard. The published page's API base is baked into `runtime-config.js`, never taken from an arbitrary URL query.

Use the page's **管理** button to enter the owner token. The browser keeps it in `sessionStorage`, not localStorage or the URL. Token location on tcstw:

`/home/young/.config/linuxdo-ai/owner-token`

It is mode `0600` under a `0700` directory. Do not paste its contents into chat, commit it, put it in a URL, or pass it on a curl command line. The deployment tested valid, absent and wrong token cases through authenticated GETs only; production vote/queue/refresh POSTs were not used as tests.

The service reads `/home/young/.config/linuxdo-ai/service.env` (mode `0600`). Relevant nonsecret settings:

- `LINUXDO_AI_OWNER_TOKEN_FILE=/home/young/.config/linuxdo-ai/owner-token`
- `LINUXDO_AI_ALLOWED_ORIGINS=https://youngzyl.github.io,https://tcstw.youngzyl.me:8443`

The migrated filter still uses the previous `deepseek-chat` / `https://api.deepseek.com/v1` configuration. This is not proof that the planned CommandCode research route is available.

## Runtime and transport

`deploy/linuxdo-ai-feed.service` is the deployed unit source. It binds the application to loopback and applies `MemoryHigh=160M`, `MemoryMax=256M`, `CPUQuota=50%`, `TasksMax=32`, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only`, and write access only to project `data` and `logs`. `UMask=0077` protects new state/log files. No hardening was removed to get the unit running.

The existing root-managed Caddy container `xray_caddy_1` handles HTTPS on 8443. Only `/linuxdo-api/*` was added, stripping that prefix and forwarding to `127.0.0.1:8791`. Existing global port settings and unrelated proxy services were preserved. The pre-existing 443 listener was not replaced. TLS hostname/certificate verification remains enabled.

Caddy configuration:

- Host file: `/root/docker-compose/xray/config/caddy/Caddyfile`
- Container mount: `/etc/caddy/Caddyfile`
- Pre-change backup: `/root/docker-compose/xray/config/caddy/Caddyfile.bak-20260921T172323Z`

This is a single-file bind mount: preserve its inode when updating the file, validate, then reload Caddy. Replacing the file by rename can leave the container reading the old inode. Only `8443/tcp` was added to firewalld (runtime and permanent); existing ports/forwarding were retained.

## Watchdog

Existing job `235d5a42aa94` remains on its 20-minute schedule. It was paused during migration, then resumed after the deployed probe returned `source=http`, `service=up`, `attention=0`, `stale=0`.

- Host wrapper: `~/.hermes/scripts/linuxdo_ai_monitor.py`, copied from `scripts/host_monitor_wrapper.py` and read back byte-for-byte.
- Canonical deployment record: `deploy/active-target.json`.
- Mission/authority source: `deploy/watchdog-prompt.txt`.

A configured remote URL is authoritative: probe failure must not fall back to a healthy-looking local snapshot. Invalid target configuration fails visibly. The watchdog diagnoses and proposes changes; automated research tuning is not enabled without approved bounds and an authenticated revisioned endpoint.

## Verification evidence

- Final Python suite: **208 tests passed** both locally (Python 3.11.15) and in a fresh isolated tcstw tree (Python 3.12.13), including 9 added tests for the final method/auth fix. Remote tests used ephemeral servers and temporary stores, not production state.
- `node --check public/app.js` and `git diff --check`: passed.
- Fixture/stub browser suite: **47/47 checks passed**. It uses ephemeral local servers, not the production backend.
- Real Pages site: index, JS, CSS and runtime config returned 200 and matched the built SHA256 values.
- Real desktop page rendered 645 topics at the check; real 390×844 touch emulation opened the preview and exposed its original linux.do link. Captured browser traffic contained no write requests and no network failures.
- Real HTTPS API returned health/state 200, expected Pages ACAO, preflight 204; unauthenticated feedback 401 and disallowed-origin preflight 403.
- The restarted, fixed backend completed a real cycle: fetched 60, 2 new, judged 2, picked 1. The two existing feedback journal records were unchanged by deployment.
- Credential-bearing HTTP requests cannot put secrets in curl argv and refuse **all redirects**, including HTTPS downgrade. Public scraping retains its previous transport behavior.
- Final source review found that POST could reach the GET-only owner failure-log handler without auth. Isolated regression tests reproduced the disclosure, then confirmed POST is rejected with 405 before reading the feed, with declared bodies forcing connection close. GET/HEAD still require owner auth. Final evidence: `/tmp/linuxdo-final-security-evidence/`; the retained production access log had no matching POST requests at the inspection (not proof of complete historical absence).
- The final fix passed an independent review (44 focused tests), then was deployed during the scheduler's idle window. The restarted unit was active/enabled with `NRestarts=0`; deployed `server.py` SHA256 matched `6fecd95eb6506dc52fb3d9347dac5615575f4fb29008e0687e20deb55ea65b44`. Live GET/HEAD failure-log requests without auth returned 401. The boot cycle completed: fetched 60, 4 new, judged 4, picked 2; queue and explicit feedback were preserved. No production POST was used for this check.

Local evidence: `/tmp/pages-batch-evidence/`, `/tmp/linuxdo-live-browser-zdz3ju_u/`. Deployed logs: `/home/young/services/linuxdo-ai/logs/server.log` and the user service journal. Temporary evidence directories are not a permanent archive.

## Publishing another static release

Build into a fresh directory outside the repository, for example:

`python3 scripts/build_pages.py --api-base https://tcstw.youngzyl.me:8444/linuxdo-api --read-state-namespace https://tcstw.youngzyl.me:8443/linuxdo-api --out /tmp/linuxdo-pages-next`

### 8444 reader cutover — published 2026-09-25

A dedicated Caddy listener now serves `https://tcstw.youngzyl.me:8444/linuxdo-api`, forwarding to the unchanged loopback backend at `127.0.0.1:8791`. The new TCP/UDP listener passed strict-TLS read-only probes: HTTP/1.1, HTTP/2 and HTTP/3 each returned 200 JSON with the Pages CORS grant for health, `state?view=list` and queue (9/9, repeated at publication). Evidence: `/workspace/linuxdo-port8444-infra/{deploy-result.json,protocol-results.json}`. The existing Hysteria forwarding rule `port=8443:proto=udp:toport=443:toaddr=` was not modified; do not repurpose that UDP port for the reader. Caddy was reloaded with the bind-mounted inode preserved; the backend was not restarted (`NRestarts=0`, start time unchanged). Runtime and permanent firewall rules add only `8444/tcp` and `8444/udp`.

The published build selects 8444 solely for requests. Its optional, build-validated `readStateNamespace` retains the prior 8443 browser-local read key (`linuxdo-ai.read:<old API base>`), including manual unread choices, without migrating or merging localStorage. When omitted, deployments keep their previous API-base (or same-origin) isolation. Neither value is read from a query parameter or localStorage; the namespace is not a network endpoint. Both runtime-config.js and app.js references are versioned to avoid mixing old and new cached scripts. The active-target record and actual watchdog prompt use 8444; the installed monitor returned `service=up`, `attention=0`, `stale=0`.

Release: source `d8b4a3f17572ab732fcaf11409fe9fb9a4084061`, Pages `0fffb302c2d4009bea56a1a522cb6082af2b8f18`. Independent review `deleg_28a362ca` passed the exact diff with no security/logic blockers (diff SHA256 `bd7bb7050106c5bb9cc34bd725d1dae2482f5351171426c1b7edce7a9cbe52d1`). Candidate checks: 53 focused unit, 11 namespace browser, 48 reader, 22 pin; reviewer reran 23 build unit and 11 namespace checks. The full Python suite was not rerun for this variant.

Pages reported this exact commit built; public app/index/runtime hashes matched the release manifest. Live desktop/mobile-emulation smoke passed **22/22**, with 9 browser requests (5 API), all GET, all API traffic on8444, no failed requests or console errors. Evidence: `/workspace/linuxdo-api-cutover-release/{manifest.json,review.diff,live-smoke/live-smoke.json}`. This confirms this test route, not every user network; ask the user to reload the reader, not erase their local storage. The separate unpublished `/workspace/linuxdo-port8444-release` variant is superseded and must not be deployed.

The only publishable entries are `index.html`, `app.js`, `styles.css`, `runtime-config.js`, `.nojekyll`. A reused output directory with extra files, directories or symlinks is rejected before writing. Never publish the repository root, fixtures, production state, logs or credentials.

Copy the verified five files into a checkout of `gh-pages` and make a normal commit/push (no force push). When initially changing Pages source, an explicit Pages build request was necessary; settings alone did not build the new branch. Verify the latest build commit and actual public asset hashes, then check the live API through the page's Origin.

## Research continuation: actual state, not a launch claim

The foundation repairs, permanent deployment and user-space Rust build prerequisite are complete. Production research scheduling, Imagine/Grok execution, safety review integration, sandbox execution, and adaptive tuning are **not enabled**.

The isolated Rust spike is at `/home/young/build/linuxdo-spike`, using Rust/Cargo 1.98.1, Tokio and bundled rusqlite. A release binary was built with one build job; a SQLite WAL row survived a fresh process. This is a build/storage experiment, not the production scheduler or evidence of container isolation. Podman reports rootless cgroup v2 with CPU/memory/PID controllers; negative runtime isolation tests still have to run.

The owner subsequently provisioned the independent CommandCode key in the mode-`0600` file:

`/home/young/.config/linuxdo-ai/research.env`

The exact variable is `commandcode_apikey`. This file is deliberately **not loaded by the current feed**, so the research key cannot accidentally replace the working filter's credential while its base URL still points to DeepSeek. No Hermes credential pool or OAuth refresh file was copied.

The CommandCode capability gate has now passed on tcstw: six requests with no retry/fallback, two-turn custom-tool roundtrips on both `deepseek/deepseek-v4.1-flash` and `z-ai/glm-5.3-flash`, plus independent GLM allow/block decisions on two synthetic safety fixtures. Returned model IDs matched the requests. `reasoning_effort=high` was requested; this is not proof the provider honored that effort internally. The reviewer fixtures are limited regression evidence, not a general harmlessness guarantee. Secret-free results are in [`docs/evidence/commandcode-capabilities.json`](evidence/commandcode-capabilities.json); the hand-run probe is `scripts/probe_research_commandcode.py`.

Evidence provenance matters: the immutable live record was produced by probe **v1**, source SHA256 `70f237ec610c8d00bf81f6fd21b7e1d571a0d43f8b75e18ac3d0b5cf9a337701`. Its six HTTP statuses, returned model IDs and outcome fields were independently checked, and the remote artifact/log were checked for the known credential with no match. Review then found gaps in how the probe enforced failure conditions and filtered provider metadata. Probe **v2** tightens these checks and is tested with synthetic fixtures; it was **not** rerun against the API or retroactively substituted for the v1 producer. No extra live requests were spent. The original producer remains under `/home/young/build/linuxdo-commandcode-capability/`.

The v2 probe passed independent narrow review (`deleg_bda825d6`, source SHA256 `d2041e3c28b66442803b4a3f22f86df8adb69b37ebfc954f63ce590c1c8362c1`). Its 96 isolated tests passed locally and on tcstw/Python 3.12.13. The complete local suite now has 304 passing tests; the deployed feed's earlier 208-test acceptance remains unchanged. A separate mode-`0700` staging directory, `/home/young/build/linuxdo-commandcode-capability-v2/`, contains only the hardened script and fake-transport tests, not credentials. These tests made no live provider calls.

This removes the CommandCode credential/capability blocker only. OAuth search/tool capability tests, production role policy authoring, remaining feed correctness/transaction repairs, durable scheduler/outbox and sandbox isolation gates remain required before advertising research as live. No research loop or generated-code execution was enabled by this probe.

## Reader reliability release — 2026-09-24

The click-to-pin reader fix (U05) and the compatible `/api/state` remediation (I01) were released together.

- Backend deployed at **2026-09-24T04:37:45Z** (`20260924T043745Z`), with backup `/home/young/services/linuxdo-ai/backups/reader-pin-20260924T043745Z`. All five remote source files matched the staged SHA256 values on readback. Released `public/app.js` SHA256 `d13cd6c1a0f2d78b09003cab27a42bc6fb6c209e119cb3d4ccc0fb51af6859de`.
- Production state was byte-identical immediately after startup and the read probes: **2105 topics, 28 bookmarks, 4 feedback**. The normal scheduler then progressed on its own to **2111 topics, 354 picked, 1757 other, 28 bookmarks**. No production POST/feedback/bookmark probe was used for verification, and the later feedback/queue readback was unchanged.
- The unit is active/running with `Result=success` and `NRestarts=0`; health returned OK.
- Review and tests: independent Codex review PASS for U05 (`deleg_68c3d59f`) and for the previous I01 set (`deleg_a130c34e`); 430 Python tests, 55 browser contract checks, 54 reader lifecycle checks, and a 22-check real-input pointer suite.
- Public build: Pages build commit `23e325a6ccf208ad5daef1b11c05b176edd05b37`; the four public assets matched the built bytes. A bounded live browser smoke run passed **20/20** including the asset gates — that counts checks, not distinct UX cases. It used real mouse and touch input: a 53 ms click against the 250 ms hover threshold, the pinned preview surviving mouseleave and a hover on another row, outside/close/Escape-with-inside-focus closing without reopening, and mobile tap/scrim/reopen. The browser made 9 requests (5 to the API), all GET, with 0 non-GET attempts, 0 failed requests and 0 console errors.
- Payload observations: the public list at 2111 topics is **387,066 compressed bytes** and carries no `body_text`; the default legacy `/api/state` still includes bodies; on-demand detail reads were confirmed. This is **not** pagination and does not guarantee that the user's own network timeout is eliminated.
- Evidence: `/workspace/linuxdo-release-pin-20260924/{backend-readback.json,public-readback.json,unit-final.log,live-smoke/live-smoke.json}`. No button or RSS changes were made.
