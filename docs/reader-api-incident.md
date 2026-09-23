# Reader API incident investigation — 2026-09-23 CST

## Scope and outcome

Read-only investigation after repeated reader `NetworkError when attempting to fetch resource` reports. A recent connection reset is recorded, but no causal match to the user's browser failure is established. Do not label this incident fixed because a later request succeeds.

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

The user's latest failing request time/browser/device was requested for correlation; no answer was received in the clarification window. No exact failed-request trace, browser network status, or request ID exists to connect the UI error to the recorded reset.

Do not conclude that the user network, DNS, TLS interception, browser behavior, proxy connection handling or response transfer is healthy globally based on the agent's successful request path. Do not present the old deployment errors as the present cause.

## Product changes in this reader release

Independently of root cause, the requested UI work replaces speculative restart copy with truthful failure states, adds a bounded read timeout/in-flight deduplication, and retains an already-loaded snapshot when a subsequent refresh fails. These are resilience/UX changes, not a claimed repair of the unidentified transport problem. No automatic mutation retries are allowed.

A future diagnostic change may add a narrowly scoped, credential-redacted proxy access log and browser error correlation. It is not enabled by this investigation, and must avoid logging authorization headers, owner tokens or private feedback bodies.
