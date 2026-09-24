# Reader API incident investigation — 2026-09-23 CST

## Scope and outcome

The initial read-only investigation could not correlate repeated reader `NetworkError when attempting to fetch resource` reports to a specific request. A later user-supplied HAR supplies the missing timing and incomplete-body evidence; see the follow-up below. Do not label the user incident fixed because an agent-side request or an isolated test succeeds.

No restart, CORS/firewall change, TLS bypass, provider request or production mutation was used for diagnosis.

## Evidence

The application file log, root-readable service journal, Caddy container error log and parsed Caddy configuration were examined. The nominal query window was 48 hours; available log coverage, not the query window, determines what can be concluded.

- Application `logs/server.log`: 485 lines at the snapshot, including 40 GET `/api/state` records, all status 200. No traceback, timeout or thread-exhaustion marker in this file.
- Those 200 statuses are logged by `BaseHTTPRequestHandler.send_response` before body delivery. They do not prove successful receipt by a browser.
- Unit snapshot: active/running, `NRestarts=0`, started September 22 at 02:00:52 CST. No OOM/thread-limit signal found in the inspected service records.
- `journalctl --user -u linuxdo-ai-feed.service` returned zero rows. That was a logging-visibility limitation, not proof that no errors occurred. Root-readable `journalctl _SYSTEMD_USER_UNIT=linuxdo-ai-feed.service` returned 523 rows and revealed two connection-reset traces.
- Recent trace: September 23 at **01:29:53 CST**, `ConnectionResetError` in Python `http.server.handle_one_request` / `socket.readinto` while reading the next request on a connection. This identifies a peer-side connection reset, not a process crash or a proven failure while generating `/api/state`. The immediate HTTP peer is the reverse proxy, not necessarily the end browser.
- Earlier trace: September 22 at 02:00:36 CST, same class of connection reset, near the prior deployment.
- Caddy recorded four 502/connect-refused events on September 22 at 01:26–01:27 CST, including one `/linuxdo-api/api/state`. These are historical deployment-window errors, not evidence for the latest repetitions.
- Caddy configuration has no complete HTTP access log configured. Its error log cannot reconstruct all successful, cancelled or client-side-failed requests, and missing entries do not prove a request never reached the proxy.

Sanitized local evidence:

- `/workspace/linuxdo-api-log-audit.json`
- `/workspace/linuxdo-api-log-followup.json`
- Lead-authored read-only collectors: `/workspace/linuxdo-api-log-audit.py`, `/workspace/linuxdo-api-log-followup.py`

## What remains unknown

At the initial investigation, the user identified a computer but did not know the failure time. There was no exact failed-request trace or request ID to connect those UI errors to the historical reset. The later HAR now narrows one reproduction, but does not establish that every earlier NetworkError had the same cause.

Do not conclude that the user network, DNS, TLS interception, browser behavior, proxy connection handling or response transfer is healthy globally based on the agent's successful request path. Do not present the old deployment errors as the present cause.

## Product changes in this reader release

Independently of root cause, the requested UI work replaces speculative restart copy with truthful failure states, adds a bounded read timeout/in-flight deduplication, and retains an already-loaded snapshot when a subsequent refresh fails. These are resilience/UX changes, not a claimed repair of the unidentified transport problem. No automatic mutation retries are allowed.

A future diagnostic change may add a narrowly scoped, credential-redacted proxy access log and browser error correlation. It is not enabled by this investigation, and must avoid logging authorization headers, owner tokens or private feedback bodies.

## HAR follow-up — September 23, 23:18–23:19 CST

The user provided a Firefox 158 HAR with nine entries. It remains outside the repository; only sanitized observations are recorded here. Source SHA256: `ba6970e43d7af411a902de31e73bb4d7768db413c4099b9af83f1e9436a1de0d`.

### Observed, not inferred

- `/health` began at 23:18:47.405, status 200, wait 7,672 ms; `/api/queue` began at 23:18:48.008, status 200, wait 7,066 ms. Both completed before the state request below.
- `/api/state` began at **23:18:55.093**. It received status 200 with the expected Pages CORS grant and no `Content-Encoding`. `Content-Length` was **2,718,951 bytes**.
- State headers arrived after **257 ms**; the recorded receive phase lasted **11,791 ms**, for **12,048 ms** total. The response content size was only **275,712 bytes** (10.14% of the declared size). The exported body is incomplete JSON.
- The deployed client has a **12,000 ms** AbortController deadline covering headers and `res.json()`. Incomplete transfer ending at that deadline strongly supports client cancellation before the body finished. The HAR alone does not expose the exact JavaScript exception or prove why throughput was low.
- The next `/health` began at 23:19:07.157 and has HAR status 0 despite recorded headers and a health JSON body. Status 0 is not an HTTP status from the server; it cannot establish a server outage.
- A read-only exact-window application-log query found state 200 at 15:18:55 UTC and health 200 at 15:19:07 UTC. Root-readable service journal records a `ConnectionResetError` at **15:19:07.146140 UTC**, approximately **5.14 ms** after the HAR state request ended. The stack is `handle_one_request` → `socket.readinto` (reading the next request), not a failed JSON-generation stack. The HTTP peer is Caddy. The timing is strong correlation, not a request-ID-linked causal trace.
- Another reset occurred at 15:18:47.389210 UTC, just before this page load. No OOM marker was found in the inspected 40-second journal window.

Sanitized correlation evidence: `/workspace/linuxdo-har-correlate.json`, generated by `/workspace/linuxdo-har-correlate.py`. These collectors never emit raw authorization headers, cookies, or log messages.

### Corrections to earlier interpretation

- The initial health and queue delays **precede** this state request. It is incorrect to claim this response starved those requests. The cause of those delays is still unknown.
- A server 200 does not mean the browser received the complete JSON. The captured partial body makes that distinction observable here.
- A nested catch around `res.json()` currently labels body-transfer cancellation as `payload` before the outer catch can recognize the aborted signal. Timeout classification must check cancellation first. Copy must say the response was not fully read within the deadline, not that the server never responded.

### Bounded remediation under review, not a production claim

- Negotiate gzip with correct `Content-Length`, CORS `Vary`, HEAD behavior, explicit refusals and wildcard handling.
- New clients request `/api/state?view=list`, omitting all topic bodies and using `has_body`; plain `/api/state` retains the legacy full shape for cached/open old clients. A public read-only `/api/topic/<id>` returns one body on an explicit preview open.
- Body availability is not displayed content: a body-only preview is marked read only after a successful current-visit render. Failed loads, hover, stale responses and a manual unread override cannot cause a false read mark.
- On the same 1,907-topic state copy, the preliminary list payload measured **346,653 gzipped bytes**, versus **2,718,965 identity bytes** before. This is an isolated payload measurement, not a production latency result; exact final bytes depend on the reviewed implementation.
- The HAR receive data implies roughly 23,383 bytes/s over that interval. At that same rate the smaller list would still take about **14.82 seconds**, exceeding the old 12-second budget. Pair payload reduction with a bounded **30-second** deadline, and exercise slow body transfer in an isolated real-browser test. This is not a guarantee for every connection.
- Reviewer `gpt-6-sol` / `openai-codex` / medium found two initial blockers: Unicode/oversized IDs produced 500 rather than 404, and wildcard gzip permission was ignored. Fixes and regression tests must pass rereview before release. No fix is described here as deployed.

This section is retained exactly as recorded at review time. Release status changed later: the remediation was deployed on 2026-09-24 — see the appended section below.

### Remediation released — 2026-09-24

- The bounded remediation described above is **deployed** (backend 2026-09-24T04:37:45Z, `20260924T043745Z`) with backup `/home/young/services/linuxdo-ai/backups/reader-pin-20260924T043745Z`. All five remote source files matched the staged SHA256 values on readback. Released `public/app.js` SHA256 `d13cd6c1a0f2d78b09003cab27a42bc6fb6c209e119cb3d4ccc0fb51af6859de`.
- Independent review: `deleg_a130c34e` PASS for the repair set and `deleg_68c3d59f` PASS for the released frontend bytes. Suites: 430 Python tests, 55 browser contract checks, 54 reader lifecycle checks, 22-check real-input pointer suite.
- Readback: production state was byte-identical immediately after startup and the read probes (2105 topics, 28 bookmarks, 4 feedback); the normal scheduler later progressed to 2111 topics, 354 picked, 1757 other, 28 bookmarks. Unit active/running with `Result=success`, `NRestarts=0` and health OK. No production POST/feedback/bookmark probe was used, and the later feedback/queue readback was unchanged.
- Public build `23e325a6ccf208ad5daef1b11c05b176edd05b37`; the four public assets matched the built bytes. Bounded live smoke 20/20 including the asset gates (checks, not distinct UX cases; 9 browser requests, 5 of them API, all GET, 0 non-GET attempts, 0 failed requests, 0 console errors).
- Payload at 2111 topics: 387,066 compressed bytes, no `body_text`; the default legacy `/api/state` still includes bodies; on-demand detail reads were confirmed. This is not pagination.
- **What this release does not establish:** the user-visible NetworkError path is not proven gone. The smaller payload and the 30 s deadline are a mitigation, not a bound on list growth, and recurrence on the user's own network was neither reproduced nor disproved. The measured list bytes also differ from the earlier 1,907-topic isolation measurement because the data set changed.
- Evidence: `/workspace/linuxdo-release-pin-20260924/{backend-readback.json,public-readback.json,unit-final.log,live-smoke/live-smoke.json}`.
