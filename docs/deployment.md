# Permanent deployment — writer on bwgca (2026-09-29)

## Live endpoints and ownership

- Reader: https://youngzyl.github.io/linuxdo-ai-feed/
- API base: https://bwgca.youngzyl.me:8444/linuxdo-api
- Health: https://bwgca.youngzyl.me:8444/linuxdo-api/health
- Compatibility forward, not a second writer: source Caddy on tcstw still answers `https://tcstw.youngzyl.me:8444/linuxdo-api` and the TCP site on `tcstw.youngzyl.me` for `/linuxdo-api/*`, then strict-TLS forwards to `https://bwgca.youngzyl.me:8444` with `Host bwgca.youngzyl.me`. Inode `50376053` was preserved. File sha256 after that change: `f45f47269276eb76a7d691d676ab39ab0e60a0cea6d42ee1c681fcfe2fa7d030`.
- Source UDP 8443 is still NAT to Hysteria 443 (`port=8443:proto=udp:toport=443:toaddr=`). That rule was not changed. Old 8443 QUIC is not a working reader path. Do not probe or document it as HTTP/3.
- The string `https://tcstw.youngzyl.me:8443/linuxdo-api` remains only as the browser read-state namespace (a localStorage key). It is not a network endpoint.
- GitHub Pages source: `gh-pages`. Current static release: `87045586d0e1f172e45c5779c084c9d64e508530` (2026-10-04 CST mobile reading-sheet release; [review and live evidence](drawer-size-release.md)). Earlier SHAs `7386e782583eb14af0f01f02690e00363f62a49d` and `2e73e435c735a621501a72e44f6cb311efe0cdac` are historical.
- Backend: native Docker Compose on `youngzyl@bwgca.youngzyl.me` port 26244. Project `linuxdo-ai`, service `feed`, container `linuxdo-ai-feed-1`. Command `python3 /opt/linuxdo-ai/run.py serve`. Public write gate is open.
- Image: `linuxdo-ai-feed:tcstw-migration-caf2efe`, id `sha256:a6ea511f74b8e824328465782f89c89b6cc91e745f7238b9e3aeb21654fe4aed`.
- Source user unit `linuxdo-ai-feed.service` is inactive, MainPID 0, disabled. Data remains on tcstw for rollback. No second writer.
- Source repo checkout: `/workspace/linuxdo-ai`, repository `youngzyl/linuxdo-ai-feed`. HEAD at the image export: `caf2efeb791be60c1c037e3ea3be0dee28d6049b`.

The tcstw user-unit, security, and browser sections below are historical. They describe the writer before stamp `20260929T2305Z`. Do not use those paths to operate the current feed.

The backend is independent of the Hermes tool container. The retired local collector and its supervisor were stopped after a final state copy. Its files remain a rollback snapshot, not an active authority. The old temporary Cloudflare tunnel was stopped after the permanent frontend/API became usable. Never revive a second collector against the retired local state. Never revive the tcstw user unit as a writer.

## Current operations (bwgca Compose)

Run from `/home/youngzyl/services/linuxdo-ai` as `youngzyl`, with sudo for Docker:

`sudo -n docker compose -p linuxdo-ai -f /home/youngzyl/services/linuxdo-ai/compose.yaml --env-file /home/youngzyl/services/linuxdo-ai/compose.env`

Pinned SSH known_hosts for this host: `/workspace/tcstw-migration-20260927/bwgca-known_hosts`. Do not disable host-key or TLS checks.

Paths the running feed actually uses:

- Compose file: `/home/youngzyl/services/linuxdo-ai/compose.yaml` (mode 0600).
- Compose env: `/home/youngzyl/services/linuxdo-ai/compose.env` (mode 0600). Nonsecret keys: `LINUXDO_UID=1000`, `LINUXDO_GID=1000`, data/logs/token/service.env paths below, `LINUXDO_AI_ALLOWED_ORIGINS=https://youngzyl.github.io,https://tcstw.youngzyl.me:8443`, filter base `https://api.deepseek.com/v1`, model `deepseek-chat`.
- Data: `/home/youngzyl/services/linuxdo-ai/data` bind-mounted at `/opt/linuxdo-ai/data`. Do not let Compose create the host path.
- Logs: `/home/youngzyl/services/linuxdo-ai/logs` bind-mounted at `/opt/linuxdo-ai/logs`.
- Owner token file: `/home/youngzyl/.config/linuxdo-ai/owner-token`, mode 0600, uid 1000. Mounted read-only at `/run/secrets/owner-token`. The process env name is `LINUXDO_AI_OWNER_TOKEN_FILE=/run/secrets/owner-token`.
- Feed env file: `/home/youngzyl/.config/linuxdo-ai/service.env`, mode 0600, loaded by compose `env_file`. It is not a systemd `EnvironmentFile` anymore.
- `research.env`: `/home/youngzyl/.config/linuxdo-ai/research.env`, mode 0600. Preserved. Not loaded into the feed env or image.
- Listen: container port 8791 published only as `127.0.0.1:8791:8791`.
- Limits in the compose file: read-only rootfs, `no-new-privileges`, `cap_drop: ALL`, mem_limit 256m, cpus 0.5, pids_limit 64, tmpfs `/tmp` 64m, `restart: unless-stopped`, `pull_policy: never`.
- Public TLS: native Caddy, not the old tcstw `xray_caddy_1` container. Snippet `/etc/caddy/Caddyfile.d/linuxdo-ai-bwgca.caddyfile`. Main file `/etc/caddy/Caddyfile` still does not contain the linuxdo site; it imports `Caddyfile.d`. Current snippet is single `bind 0.0.0.0`, `handle_path /linuxdo-api/*` to `127.0.0.1:8791`, no migration `respond` gate. Native Caddy MainPID stayed 49118 when the gate was removed. Main file sha256 `f643a9cb53d52a23a403961b48b3ebbcb91789b501bd8a6c3fc6459d8cc8641d`. Gate-removed snippet sha256 `fffdbe53fffa75ac9709364df53409e9c3acb8ae8d9ac8a4c7ef706736d78207`.
- Caddy binary used by the infra helper: `/usr/local/libexec/caddy/caddy-2.9.1-naive`. Validate with that binary and the main Caddyfile before reload. Do not replace the main file by rename.

Anonymous visitors can read the feed, previews, and queue. Writes and raw feedback/failure reads require the owner bearer token. A missing configured token fails closed. Browser Origin checks are an exact allowlist. The published page's API base is baked into `runtime-config.js`, never taken from a URL query. The owner token is entered with the page's 管理 button and kept in `sessionStorage`. Do not paste it into chat, commit it, put it in a URL, or pass it on a curl command line.

Quiesced source snapshot: `/home/young/services/linuxdo-ai/cutover-snapshot-20260929T2305Z` (files mode 0400). Target pre-replace backup: `/home/youngzyl/services/linuxdo-ai/cutover-backup-20260929T2305Z`.

## Watchdog

Job `235d5a42aa94` is on its 20-minute schedule. It was paused during migration, then resumed after the deployed probe returned `source=http`, `service=up`, `attention=0`, `stale=0`.

- Host wrapper: `~/.hermes/scripts/linuxdo_ai_monitor.py`, copied from `scripts/host_monitor_wrapper.py`.
- Canonical target: `deploy/active-target.json`. Parent updated this to the bwgca Compose feed. `service_unit` is null. `service_manager` is `docker-compose`. `compose_service` is `feed`.
- Mission text: `deploy/watchdog-prompt.txt`. Parent updated it. It names the bwgca Compose feed and forbids reviving the tcstw writer.

A configured remote URL is authoritative: probe failure must not fall back to a healthy-looking local snapshot. The watchdog diagnoses and proposes changes. Automated research tuning is not enabled.

Parent independently checked `https://bwgca.youngzyl.me:8444/linuxdo-api` health, `/api/state?view=list`, and `/api/queue` over `--http1.1`, `--http2`, and `--http3-only`. All 9 were JSON 200 with Pages CORS. Evidence: `/workspace/tcstw-migration-20260927/bwgca-production-protocols.json`.

This documentation round did not verify a live browser. The `browser_use` host module is missing and the fallback Chromium CDP session is closed. Older browser pass counts below are historical. Do not cite them as a new live proof.

## Writer cutover — done (stamp 20260929T2305Z)

- Final sync copied 9 files (data 4, logs 2, secrets 3 including `research.env`). `research.env` is preserved and not loaded into the feed env.
- At stop: topics 4258, queue 30, feedback file lines 5. Owner GET `/api/feedback` returned 4 items. Queue semantic hash `845bae67829772656c707a2db79d1665012df8edfa69fcdb1b10feef1b5f9963` matched on both sides.
- First target cycle after writer start: fetched 60 (7 new), judged 8, picked 3. topics became 4265. health `last_success_at` `2026-09-29T14:36:57Z`, fetch_ok and filter_ok true.
- bwgca public gate was then removed. Native Caddy MainPID stayed 49118.
- Public health/state/queue were 200 on both hostnames at gate-open. Anonymous feedback and anonymous queue POST were 401. Owner GET feedback was 200 with no redirect.
- No CI/CD change was made in this cutover. Parent owns Pages publication. No commit and no push from the documentation pass.

## Publishing another static release

Build into a fresh directory outside the repository:

`python3 scripts/build_pages.py --api-base https://bwgca.youngzyl.me:8444/linuxdo-api --read-state-namespace https://tcstw.youngzyl.me:8443/linuxdo-api --out /tmp/linuxdo-pages-next`

The read-state namespace is a localStorage key. It must stay the old 8443 string so existing unread choices survive. It must not be used as a request URL.

The only publishable entries are `index.html`, `app.js`, `styles.css`, `runtime-config.js`, `.nojekyll`. A reused output directory with extra files, directories, or symlinks is rejected before writing. Never publish the repository root, fixtures, production state, logs, or credentials.

Copy the verified five files into a checkout of `gh-pages` and make a normal commit/push (no force push). Verify the latest build commit and actual public asset hashes, then check the live API through the page's Origin. Current published commit is the SHA above. Do not rebuild it from this note.

## Historical: tcstw user unit and Caddy (before 20260929T2305Z)

These paths are not the current writer. Kept so a rollback reader can see what was deployed on tcstw.

`deploy/linuxdo-ai-feed.service` was the deployed unit source. It bound the application to loopback and applied `MemoryHigh=160M`, `MemoryMax=256M`, `CPUQuota=50%`, `TasksMax=32`, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only`, and write access only to project `data` and `logs`. `UMask=0077` protected new state/log files. No hardening was removed to get the unit running. That unit is now disabled.

Historical token and env paths on tcstw, retained with the source data:

- `/home/young/.config/linuxdo-ai/owner-token`
- `/home/young/.config/linuxdo-ai/service.env`
- `LINUXDO_AI_OWNER_TOKEN_FILE=/home/young/.config/linuxdo-ai/owner-token`
- `LINUXDO_AI_ALLOWED_ORIGINS=https://youngzyl.github.io,https://tcstw.youngzyl.me:8443`

The migrated filter still used `deepseek-chat` / `https://api.deepseek.com/v1`. That was not proof that the planned CommandCode research route was available. The same filter settings are what compose.env still sets. Research scheduling is still not enabled.

Historical transport: root-managed Caddy container `xray_caddy_1` handled HTTPS. Host file `/root/docker-compose/xray/config/caddy/Caddyfile`, container mount `/etc/caddy/Caddyfile`. Pre-change backup `/root/docker-compose/xray/config/caddy/Caddyfile.bak-20260921T172323Z`. It was a single-file bind mount: preserve inode, validate, reload. Only `8443/tcp` was added to firewalld at that original install. The 2026-09-25 reader cutover added a dedicated 8444 TCP/UDP listener on that same source Caddy, forwarding to `127.0.0.1:8791`. Those blocks now forward to bwgca. The UDP 8443 NAT to Hysteria 443 was not part of the reader and still is not.

Historical verification, not a current live browser proof:

- Final Python suite at the tcstw deploy: 208 tests passed locally (Python 3.11.15) and in a fresh isolated tcstw tree (Python 3.12.13). Remote tests used ephemeral servers, not production state.
- Fixture/stub browser suite: 47/47. Ephemeral local servers, not production.
- Real Pages and real desktop/mobile checks from that deploy are historical. Evidence dirs `/tmp/pages-batch-evidence/` and `/tmp/linuxdo-live-browser-zdz3ju_u/` were temporary.
- POST-to-failure-log fix evidence: `/tmp/linuxdo-final-security-evidence/`. Deployed `server.py` SHA256 at that restart was `6fecd95eb6506dc52fb3d9347dac5615575f4fb29008e0687e20deb55ea65b44`. The retained production access log had no matching POST at that inspection. That is not proof of complete historical absence.
- Deployed logs at that time: `/home/young/services/linuxdo-ai/logs/server.log` and the user service journal.

### Historical: 8444 reader cutover on tcstw — published 2026-09-25

A dedicated source Caddy listener served `https://tcstw.youngzyl.me:8444/linuxdo-api` to `127.0.0.1:8791`. Strict-TLS HTTP/1.1, HTTP/2, and HTTP/3 each returned 200 JSON with Pages CORS (9/9). Evidence: `/workspace/linuxdo-port8444-infra/{deploy-result.json,protocol-results.json}`. Release: source `d8b4a3f17572ab732fcaf11409fe9fb9a4084061`, Pages `0fffb302c2d4009bea56a1a522cb6082af2b8f18`. Live smoke 22/22 is historical: `/workspace/linuxdo-api-cutover-release/{manifest.json,review.diff,live-smoke/live-smoke.json}`. The unpublished `/workspace/linuxdo-port8444-release` variant was superseded and must not be deployed. This cutover did not change the UDP 8443 NAT.

## Research continuation: actual state, not a launch claim

The foundation repairs, permanent deployment, and user-space Rust build prerequisite are complete. Production research scheduling, Imagine/Grok execution, safety review integration, sandbox execution, and adaptive tuning are not enabled.

The isolated Rust spike is at `/home/young/build/linuxdo-spike` on tcstw, using Rust/Cargo 1.98.1, Tokio, and bundled rusqlite. That path was not moved with the feed. It is a build/storage experiment, not the production scheduler.

The CommandCode key lives in the mode-0600 `research.env` (`commandcode_apikey`). That file is not loaded by the feed, on tcstw or on bwgca. No Hermes credential pool or OAuth refresh file was copied into the image.

The CommandCode capability gate passed on tcstw before the move: six requests, no retry/fallback, two-turn custom-tool roundtrips on `deepseek/deepseek-v4.1-flash` and `z-ai/glm-5.3-flash`, plus independent GLM allow/block decisions on two synthetic safety fixtures. Secret-free results: `docs/evidence/commandcode-capabilities.json`. Probe v1 source SHA256 `70f237ec610c8d00bf81f6fd21b7e1d571a0d43f8b75e18ac3d0b5cf9a337701`. Probe v2 was not rerun against the API. v2 review `deleg_bda825d6`, source SHA256 `d2041e3c28b66442803b4a3f22f86df8adb69b37ebfc954f63ce590c1c8362c1`. This does not make research live.

## Historical: reader reliability release — 2026-09-24

The click-to-pin reader fix (U05) and the compatible `/api/state` remediation (I01) were released together on the tcstw unit.

- Backend deployed at 2026-09-24T04:37:45Z, backup `/home/young/services/linuxdo-ai/backups/reader-pin-20260924T043745Z`. Released `public/app.js` SHA256 `d13cd6c1a0f2d78b09003cab27a42bc6fb6c209e119cb3d4ccc0fb51af6859de`.
- Production state immediately after that startup: 2105 topics, 28 bookmarks, 4 feedback. The scheduler then moved on its own. No production POST was used for verification.
- Review: Codex `deleg_68c3d59f` and `deleg_a130c34e`. 430 Python tests, 55 browser contract checks, 54 reader lifecycle checks, 22-check pointer suite. Those counts are that release, not this documentation round.
- Pages build commit `23e325a6ccf208ad5daef1b11c05b176edd05b37`. Live browser smoke 20/20 is historical. Evidence: `/workspace/linuxdo-release-pin-20260924/{backend-readback.json,public-readback.json,unit-final.log,live-smoke/live-smoke.json}`.
- Payload observation at 2111 topics: public list 387,066 compressed bytes, no `body_text`. Not pagination.
