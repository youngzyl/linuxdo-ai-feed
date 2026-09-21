# Imagine Multi-Agent Research — Review and Implementation Plan

> **For Hermes:** Use subagent-driven-development for staged implementation. Delegate hands-on changes and verification to the persistent sidekick; retain policy/configuration decisions, final diff review, commits and user-facing decisions. Use fresh delegate reviewers for independent checks.

**Status:** SUPERSEDED for implementation by `2026-09-21_085937Z-independent-research-rust.md` following the user's latest architecture corrections. Retain this file as the source-audit/baseline record; do not implement its conflicting mandatory peer communication, Python research runtime or paired-start choices. No feature code, runtime configuration, cron, credentials or production state changed by this planning task.

**Goal:** Extend the existing read-only linux.do feed into an asynchronous research workspace: filter dispatches independent research questions, Imagine develops ideas with sandboxed code and search, a parallel Grok researcher uses native Web/X Search, and an independent reviewer checks actions and publication safety.

**Architecture:** Keep the existing lightweight Python and vanilla-JS application. Add transactional SQLite persistence, a single durable scheduler with logical agent sessions, a per-topic/per-run message channel, narrow credential/tool brokers, and disposable code sandboxes. Three communicating roles are Imagine, Grok researcher and reviewer; filter is the upstream dispatcher, not another permanently running group participant.

**Tech stack:** Python 3.11+, SQLite WAL on a local filesystem, bounded asyncio I/O (one pooled async HTTP client if adopted), existing vanilla JS/CSS, short cursor-based polling initially. No Redis, Kafka, vector DB, chat-platform dependency, or separate full Hermes process per research branch. Sandbox runtime choice is gated on host capability discovery; a venv or subprocess alone is NOT a security boundary.

---

## 1. Scope and corrected requirements

- Preserve current desktop All / Picked / Queue layout, slot alignment and compact/gap toggle.
- Main list: title and brief source-content preview, plus an unobtrusive status dot. Do not add a third analysis text block or a chat transcript.
- A rejected post can still inspire research. Reading value and research potential are separate decisions.
- Filter may dispatch multiple distinct questions for the same post. Replace the old one-agent-per-post / boolean-only proposal with `imagine_tasks[]`.
- Do not impose an arbitrary lifetime count of sidekicks. Admit logical tasks durably; control simultaneous resource use with backpressure. No unbounded in-memory queue or process creation.
- Primary Imagine: CommandCode `deepseek/deepseek-v4.1-flash`, high reasoning.
- After the primary retry policy is exhausted, use CommandCode GLM 5.3 Flash. Preserve the user's string `glm-5.3-flash`; resolve the exact catalog ID explicitly before configuration.
- Grok `grok-4.6`, high reasoning via **xai-oauth**, is a parallel research participant, NOT fallback for DeepSeek.
- SearXNG remains available to Imagine alongside page fetch, structured data processing and Code Execution.
- Remove old 3-tools / 60-seconds / 3k-output hard design. Research should continue through renewable work slices while making progress and remaining within owner-approved limits.
- All failed AI attempts, including recovered failures, fallback calls and reviewer failures, enter a durable incident stream reviewed by cron.
- Preserve independent `commandcode_apikey`; no silent reuse of unrelated credentials, DeepSeek-direct substitute, paid xAI API-key fallback, or silent downgrade of high reasoning.
- Only public source content goes to research providers. Owner feedback notes remain out of Grok/research prompts unless separately approved. Existing filter preference examples already leave the machine through the filter API; old comments implying otherwise are incorrect.

## 2. Review baseline and evidence

Repository inspected: `/workspace/linuxdo-ai`, HEAD `58c2cc7`. Initial status: only untracked `docs/PLAN-imagine.md`; previous GitHub push blocker is obsolete. Existing mobile `[hidden] { display: none !important; }` fix is present at `public/styles.css:14`.

### Confirmed from lead source inspection

**R1 — Filter failure escalation is reset too early (high).**
`pipeline.py:89-103` calls `Store.record_success()` before filtering; `store.py:361-368` resets `filter_consecutive_errors`. A successful fetch followed by a failed filter repeatedly leaves the filter streak at one. Separate stage counters and stage success timestamps; only filter success resets filter failure count. Add repeated-cycle tests, not only direct counter tests.

**R2 — Cooldown crosses unrelated upstreams (high).**
`http_util.py:87-115,231-265` has one global `_state`, gates every request, and clears it on every successful response. linux.do, Jina and model-provider traffic can block or reset each other. Separate per-origin/provider-account cooldown and retry policy; support both Retry-After seconds and HTTP dates.

**R3 — No owner boundary on expensive/mutating endpoints (high).**
`server.py:114-169` accepts refresh, queue and feedback writes without authentication/authorization or Origin protection. A public tunnel is not an access-control boundary. Before exposing agent creation/retry/code tools, separate public reads from owner-authorized mutations; protect private diagnostics and add request limits. This is a source finding, not a claim that exploitation occurred.

**R4 — Static path containment uses a string prefix (high, conditional exposure).**
`server.py:184-198` tests `str(target).startswith(str(root))`. A sibling such as `public-other` can pass when files exist there. Use resolved path ancestry, test encoded traversal and symlinks on a temporary tree. Do not test against secrets.

**R5 — JSON snapshots are not multi-process transactions (high prerequisite).**
`store.py:163-167` protects a whole-file replace with an instance-local RLock. `get()`/`topics()` (`237-243`) expose mutable references. CycleLock does not protect every store writer or reload an old snapshot. Feedback journal helps recover vote records, but does not transactionally recover all effective topic/queue state. Move durable mutable state to one transactional store before parallel workers.

**R6 — UI acknowledges writes before the server does (medium/high).**
`public/app.js:818-875` drops network failures and HTTP failures for queue/feedback while immediately updating local state. `971-990` drops refresh POST failures and pulls state after a fixed delay. `1088-1105` unions server queue into localStorage, allowing stale removals to survive. Use acknowledged mutations, explicit pending/error states, version-aware state reconciliation and server-authoritative queue operations.

**R7 — Filter may permanently classify incomplete input (medium/high).**
`pipeline.py:93-120` filters before remaining detail fetches; detail selection excludes rejected posts. `filter.py:271` only selects pending topics. Later body arrival does not automatically re-evaluate earlier decisions. Track source/body version and provisional verdicts; late body availability must trigger bounded re-evaluation without overriding explicit user feedback.

**R8 — Partial model output can look successful (medium).**
`filter.py:284-305` records omitted indices but does not set `summary.ok=False`; `normalize_verdict` coerces weak schemas. Track per-topic incomplete/malformed decisions explicitly, reject duplicate/out-of-range identifiers, and persist attempts separately from accepted verdicts.

**R9 — Current watchdog cannot prove all failed calls were reviewed (high prerequisite).**
`scripts/monitor.py:75-89,122-127` summarizes health flags; it has no incident-consumption cursor. `store.py:199-203` truncates failure logs; journal write failures are swallowed. New failures can occur and recover between ticks without waking the monitor. Add append-only incident IDs, review cursors and explicit monitoring-storage failures.

**R10 — Secrets are passed through process arguments (high before code execution).**
`http_util.py:189-195` puts every header, including provider bearer credentials and any cookies, into curl argv. Same-namespace process inspection can expose them. Use in-process credentialed provider HTTP transport; keep curl only for public scraping without credential argv. Ensure the code sandbox cannot inspect broker processes, and redact nested incident context as well as error strings.

**R11 — Explicit user state is mixed with model state (medium/high).**
`learn.py:54-65` mutates topic state separately from vote journaling; `learn.remove():69-71` clears feedback but does not restore model-derived placement. `Store.set_verdict():251-265` can overwrite a concurrent manual choice. Store user overrides independently and compute effective state transactionally; define clear-vote behavior with tests.

### Verification evidence appendix

- Baseline: `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp/baseline-evidence/tmp python3 -m unittest discover -s tests -v` → exit 0, **64 tests passed** in 0.036s. Lead read `/tmp/baseline-evidence/unittest.log:66-69`. A guarded rerun with blocked network also passed (sidekick evidence: `/tmp/baseline-evidence/unittest-guarded.log`). Tests use temporary Stores and mocked collector HTTP, not the production feed.
- `node --check public/app.js` → exit 0 (sidekick report; `/tmp/baseline-evidence/nodecheck.log`).
- Independent mobile baseline on a temporary static copy with fixtures: Chromium 151, 390×844 emulation, closed scrim `display:none` with zero-sized box; refresh hit-test returns BUTTON, title opens the drawer. `/tmp/baseline-evidence/cdp_probe.log`, `cdp_closed.log`, `scrim-closed.png`, `scrim-mobile.png`. This is Chromium emulation, not physical iPhone/Safari coverage.
- Lead compared sidekick's before/after hash manifests: **35 tracked files unchanged**. Runtime files/logs continue changing under the existing live service and are not attributed to tests. The new `.hermes/plans/` document is the lead's permitted plan-only write.
- Public catalog downloaded successfully to `/tmp/baseline-evidence/models.json` and re-parsed by lead: 71 entries. Exact `glm-5.3-flash` ID is absent; display name **GLM-5.3 Flash** maps to **`z-ai/glm-5.3-flash`**, not `z-ai/glm-5.3-flashx`. Use the explicit mapped ID in the implementation after confirmation. DeepSeek exact ID is present. Both list `/chat/completions` and `/responses`. Catalog has no reasoning/tool-support fields, so presence is not a high-reasoning/tool-call smoke test.
- No authenticated model call was performed in this planning task. Project-specific CommandCode credential readiness and account-specific xAI native-search availability remain deployment gates, not claims based on old session state.

Source review is not equivalent to a live integration test. No live vote/queue/refresh mutations are authorized for this planning task. Independent review/runtime details follow in the final evidence addendum.

## 3. Three communicating roles

### A. Imagine

Owns a research question, develops hypotheses, asks Grok for evidence or counterexamples, invokes SearXNG/fetch/code tools, and produces a cited research artifact. It can propose more independent branches, not directly spawn unrestricted processes. Novelty is encouraged; speculative claims must be labeled as hypotheses rather than facts. No mandatory fixed number of ideas or output word count.

Model routing: DeepSeek high first; CommandCode GLM fallback only after classified failures. High reasoning must be explicitly supported/probed; a rejected parameter is a configuration fault, not permission to silently remove it.

### B. Grok researcher

Starts alongside Imagine for an admitted research run. Uses native Responses `web_search` and `x_search`, returns source references, dates, counterevidence and its own associations. It can reply to Imagine and ask focused follow-up questions. It does not receive local credentials, owner notes, production files or an unrestricted local shell.

First release does not require provider-hosted Grok Code Execution; the user's primary request is a dedicated Imagine code environment. Grok's optional `code_interpreter` is a separately gated later capability, not confused with the local sandbox.

### C. Reviewer

Independent context and role, separate from Imagine's answer generation. Examines declared next actions, tool arguments/results, changed artifacts and public findings. It can allow an action within existing capabilities, request revision, pause, block or escalate. It cannot grant new capabilities, enlarge hard budgets, change auth, rewrite application code, or approve its own unavailable/failed review.

Proposed reviewer model: a separately configured GLM instance for model diversity, contingent on catalog/capability tests. This is NOT yet a user-selected model. Reusing DeepSeek is possible but provides role separation rather than model diversity; shared-provider outages remain correlated in either case.

Filter dispatches and continues its normal work. Imagine/Grok/reviewer messages are not fed back into personal taste automatically. Reading a dot or hovering never counts as a keep/skip vote.

## 4. A lightweight durable channel, not a permanent bot fleet

One room per `(topic_id, source_version, research_run_id)`. A room has multiple branch tasks and three role types, not necessarily exactly three total executions.

SQLite records:

- `topics`, `source_versions`, `verdicts`, `feedback`, `reading_queue`.
- `research_runs`, `tasks`, `attempts`, `tool_calls`, `artifacts`, `reviews`.
- `messages`, `message_receipts`, `incidents`, `monitor_cursors`.

Minimal message envelope (proposed contract, not implemented):

```json
{
  "message_id": "server-assigned",
  "room_id": "server-assigned",
  "task_id": "server-assigned",
  "sender_role": "imagine",
  "recipient_role": "researcher",
  "kind": "question",
  "reply_to": null,
  "body": "Find counterexamples to this proposed mechanism.",
  "artifact_refs": [],
  "source_refs": [],
  "trust": "agent_output",
  "sequence": 1
}
```

The server derives sender identity; the model cannot impersonate reviewer/owner by writing JSON or mentioning a role. Immutable messages, monotonically increasing cursors, explicit reply targets, idempotency keys and receipt checkpoints prevent replay from becoming new work. External posts/pages are untrusted data, never instructions or permission changes.

The scheduler wakes recipients on meaningful addressed messages. No full-transcript rebroadcast to every role on each token. Agent context uses recent relevant events plus summaries with links to full durable evidence. Large bodies/files stay out of messages; bounded references avoid resident-memory growth. Detect repeated question loops and no-progress cycles.

Delivery is at-least-once with idempotent application transitions, NOT a promise of exactly-once model billing. Persist provider request IDs and mark unknown outcomes after timeouts. Late replies from expired leases cannot publish. SQLite WAL requires local disk and bounded write transactions; do not put it on a network filesystem.

## 5. Retry/fallback semantics

Interpret **“退避重试4次” as first request plus up to four retries**, not four total attempts. This interpretation needs confirmation because the previous draft mixed these meanings.

Proposed retry delays: `5s, 20s, 60s, 180s`, bounded jitter ±20%, respecting provider Retry-After. Persist `next_attempt_at`; waiting tasks release worker capacity.

1. Imagine issues DeepSeek high request with a persisted logical step ID.
2. Transient failures (timeouts, selected network errors, 429/5xx) use that retry ladder.
3. After retry exhaustion, continue the SAME logical task/checkpoint on CommandCode GLM. Do not rerun completed code or discard previous research.
4. GLM retry count is not specified by the user: proposal is the same four retries, configurable independently.
5. All failures and provider transitions are incident events, even if later recovered.
6. A same-provider account-wide 401/403/quota block will often affect both models. Classify it and surface `blocked_auth`/quota-wait instead of generating a retry storm. OAuth gets one synchronized refresh-and-replay on 401 via its credential owner; do not run many simultaneous refreshes.
7. Schema/unsupported-parameter errors, safety blocks and explicit cancellation do not receive blind transient retries. Provider-reported refusal is not bypassed by fallback. A model-specific unavailable error may route to the approved fallback with the reason recorded.
8. Retry only the failed request/step. A crash after an external request may have incurred cost; preserve `outcome_unknown` until reconciled instead of claiming it did not run.
9. Account-wide circuit breaker and resource caps apply across roles. Grok branch failure does not erase safe Imagine output; publication can be explicitly partial after review.

## 6. Long-running work without runaway execution

Initial **proposed**, tunable operating envelope:

- Research work slice: 15 active minutes or 40 external tool calls, then checkpoint/review.
- A reviewer can recommend another slice if there is useful progress and no safety issue. The scheduler grants it only within owner-configured cumulative limits.
- Proposed unattended ceiling: two hours active runtime per run. Reaching it pauses/resumes later; it does not delete work or claim completion. Owner may raise it after measured runs.
- Model request timeout is separate from work-slice time; start with 300 seconds plus explicit stream-idle detection. Long reasoning is not automatically a hung task. Retry delays and provider quota waits do not count as useful reasoning progress.
- Local code starts at a 120-second per-command timeout, extendable for reviewed compute tasks; separately constrain CPU/memory/PIDs/output/files.
- No fixed limit of one branch per post or 32 total agents. Simultaneous model calls and running code sandboxes are separately bounded. Scheduler stores queued tasks on disk and delays admission under memory/quota/disk pressure. Resource exhaustion is visible, not silent task loss.
- Cancellation is checked before every new step. Own subprocess groups are terminated by precise task ownership, never broad process-name kills. Already-running remote native tools may not be cancellable; report that uncertainty.
- Track usage, reasoning tokens, tool use and estimated cost per provider/task. Unknown pricing remains unknown. No unlimited auto-extension or retry-on-safety-block loop.

Do not confuse “keep thinking” with an infinite single API request: durable slices preserve useful state without accumulating unlimited context or requiring constant RAM.

## 7. Dedicated Code Execution environment

Threat model: forum text, retrieved pages and generated programs are untrusted. Reviewer can miss problems; sandbox enforcement must remain effective even when all models approve malicious input.

Proposed boundary:

- On-demand disposable sandbox per active branch, with a branch-owned workspace and reviewed artifact export; dormant branches keep files/checkpoints, not a live process.
- Rootless container or stronger existing sandbox runtime, non-root UID, read-only base filesystem, all capabilities dropped, no-new-privileges, seccomp/LSM where supported, CPU/memory/PID/storage limits.
- No host home/project/data/auth mounts, no container daemon socket, no SSH keys, no environment credentials, no host PID namespace.
- Network off by default. Internet access only through broker tools that validate destinations and return bounded content. Code cannot bypass fetch policy using curl/sockets/DNS.
- URL fetch broker permits http(s) public destinations only, revalidates redirects/DNS, blocks loopback/private/link-local/metadata addresses, caps compressed/decompressed bytes and time. Trusted local SearXNG is an explicitly configured broker endpoint, never an arbitrary URL exception.
- Preinstalled small Python scientific/tooling image. Packages beyond it require reviewed build/install policy; no free-form network package installation during a run.
- Sanitized input copies in; approved text/image artifacts out. No generated HTML/JS execution in the feed origin; downloads use safe content disposition and size limits.
- Broker/runner are trusted services, not model-generated shell scripts. Renderer treats all model output as data.
- Runtime prerequisites and host deployment must be checked. If isolation cannot be enforced, Code Execution remains disabled; never fall back to `exec()` or shell in the web-server process.

This reduces risk; neither reviewer nor container isolation proves universal harmlessness. State the residual kernel/runtime/provider risks explicitly.

## 8. Review mechanism — event-driven safety plus operational cron

### Inline deterministic checks

Every tool invocation passes schema, identity, capability, URL/path and resource checks. Unsupported requests are blocked before execution. No LLM approval can override them.

### Independent semantic review

Proposed cadence: review a meaningful delta after five completed tool calls or 60 seconds of active work with new evidence, whichever arrives first. No changes means no repeated LLM call. Additional mandatory checkpoints:

- Before first generated-code execution and before new code/action changes with new risk.
- Before a new data egress category or capability expansion (expansion needs owner approval, not reviewer alone).
- On suspicious instructions, policy violations, unexpected artifacts or repeated loops.
- Before each public result version becomes `ready`.

Reviewer receives compact deltas with full-evidence references, objective, permitted capabilities and current budgets. It receives observable plans/actions/results and concise rationale, not a demand for hidden chain-of-thought.

Proposed review record:

```json
{
  "review_id": "server-assigned",
  "task_id": "server-assigned",
  "through_event_seq": 42,
  "decision": "continue",
  "findings": [],
  "required_changes": [],
  "evidence_refs": [],
  "artifact_version": "server-assigned",
  "capability_version": "server-assigned",
  "next_review_trigger": "after_action_delta"
}
```

Allowed decisions: `continue`, `revise`, `pause`, `block`, `escalate`. A review applies to exact action/artifact/capability versions; changing them invalidates stale approval. Reviewer failure pauses gated code/publication, not the whole feed or previously approved results. Reviewer queues get reserved scheduling capacity so research cannot starve its oversight.

### Operational cron

Existing job `235d5a42aa94` is enabled every 20 minutes (read-only discovery confirmed during this task). It currently watches collector health. Proposed update AFTER approval:

- Cheap deterministic scan every five minutes of unreviewed incident IDs, overdue heartbeats/leases and unresolved faults; invoke RCA only when actionable new state exists.
- All failed AI calls remain in incident records, including recovered attempts, fallback, Grok and reviewer. Durable cursor ensures they are eventually reviewed even when the UI returned to healthy between ticks.
- RCA summary: affected role/model/task; failure category and recurrence; recovered/pending status; likely cause and evidence; proposed fix; tests run and their result; remaining risks.
- Distinguish safety reviewer from repair agent. The reviewer must not gain production code-write access. Operational repair works in an isolated branch/worktree and passes independent review before deployment; no silent broad patching or weakening of safety policy.
- Detector output contains stable incident watermarks/signatures, not timestamps that spuriously wake the LLM. Recovery and unresolved overdue work must still produce deliberate review transitions.

## 9. API and UI

### API shape

- `/api/state` retains existing feed contract during transition and adds only lightweight research status per topic: status, pending/running/failed counts, reviewed-result version and update sequence. No research prose, tool transcript or raw errors.
- This is **status metadata**, not an AI-written summary. Clicking fetches full reviewed content. Do not claim the entire existing `/api/state` is small: it currently includes source bodies; source-detail pagination is a later compatible optimization.
- `GET /api/imagine/<topic_id>` returns only published/reviewed artifacts and reader-safe status. Aggregate multiple branches without one dot per agent.
- Mutating `/api/imagine/<topic_id>` and task retry/pause/cancel endpoints are owner-authorized, explicit actions. Hover/GET cannot spawn a billable task.
- Internal message/review endpoints are role-scoped, authenticated and never routed through the public frontend. Owner diagnostics are separately authorized.
- Start with cursor/version-based polling while tasks are active; stop in background tabs. Consider SSE only if measurements justify it. Existing ThreadingHTTPServer must not acquire one permanent thread per stream without an explicit migration.

### Presentation

- Dot visible as soon as a task is queued, before any result exists. Small visual diameter, at least 44px mobile hit target without expanding row height unnecessarily.
- Queued: hollow; running: subtle motion; retry-wait: distinct paused/hollow variant; ready: solid; failed: clear failure shape; blocked/review-pending: accessible textual status inside details. Color is not the only distinction; reduced-motion supported.
- Desktop hover shows a compact preview of approved content/status; click pins/opens the canonical drawer. Nested dot events must not also trigger row hover, title actions, queue toggles or mobile swipe handling.
- Mobile dot opens the existing drawer at the research section; row tap remains source preview. No mandatory visible “imagine/联想” label beside titles. A neutral section label inside details is optional for orientation, not product branding.
- No raw conversation or unsafe partial result flashes before review. Show progress counts/status while running; safe reviewed partial versions can be read as branches continue. Failure in one branch does not hide already-approved findings.
- Dot visibility on rejected posts must work in All. Keep original column placement rules independent of research state.
- Close/Esc/backdrop/focus restoration, original-post link, refresh and scrolling get real mobile touch tests. Keep the corrected `[hidden]` rule.
- No unrelated swipe redesign or original-link redesign is silently bundled into this feature.

## 10. Implementation order and acceptance gates

All paths below are relative to `/workspace/linuxdo-ai`. New paths are proposed and do not yet exist. Each change follows: write failing regression test → run it → minimal implementation → run focused suite → independent review → lead-controlled commit. Do not combine unrelated phases in one commit.

### Phase 0 — Freeze evidence and capability spikes

0.1 Record HEAD/status and isolated baseline test logs; preserve production votes/queue.
0.2 Catalog-map exact model IDs and capabilities. Run later credential-scoped one-turn high-reasoning/tool-call probes; never print keys.
0.3 Test OAuth Web Search and X Search separately, then together; capture actual tool events/citations/usage/provider identity. HTTP success or a textual answer alone does not pass.
0.4 Discover host sandbox runtime, enforceable limits and broker placement. Measure baseline RSS before selecting concurrency.

Files: proposed `tests/integration/test_provider_capabilities.py`, `scripts/probe_capabilities.py`; artifacts under ignored `data/evidence/`. Plan-only stage does not create/run authenticated probes.
Gate: exact routing and capability evidence or explicitly blocked feature; no silent provider/auth fallback.

### Phase 1 — Repair foundation before adding agents

1.1 Tests for repeated filter failures and separate successful fetch; fix `pipeline.py`, `store.py` counters.
1.2 Per-origin cooldown tests with injected clock, 429 HTTP-date handling; refactor `http_util.py`.
1.3 Temporary-server auth/Origin/schema/rate-limit tests; add `auth.py`, harden `server.py` and static containment. No tunnel exposure of agent writes before this gate.
1.4 Failed POST and two-client conflict tests; change queue/feedback acknowledgments and refresh-job state in `public/app.js`.
1.5 Body-version/provisional-filter tests; change `collector.py`, `filter.py`, `pipeline.py`; preserve explicit user override separately.
1.6 Missing/duplicate/invalid model-result tests and all-attempt incident recording in `filter.py`.

Tests: new `tests/test_pipeline_health.py`, `test_http_policy.py`, `test_server_security.py`, `test_filter_versions.py`; browser checks use temp fixtures/mocked routes, never real votes.
Gate: baseline remains green and each identified defect has a failing-before/passing-after regression.

### Phase 2 — Transactional store and durable jobs

2.1 Add `db.py`, `migrations/001_core.sql`, `tests/test_db_migration.py`; migrate topics/verdicts/feedback/queue with stable IDs and separate human overrides.
2.2 Add `jobs.py`, `migrations/002_research.sql`, `tests/test_jobs.py`; define idempotency, leases, heartbeat, next-attempt time, cancellation, attempt history and incident IDs.
2.3 Add `scripts/migrate_state.py`; produce an offline snapshot import, verify counts and feedback replay, then exercise rollback/export on the copy. Do not run production migration during planning.
2.4 Adapt `store.py` API facade to transactions; remove competing JSON writes. Cutover under one controlled writer pause, retain immutable backup; no uncontrolled dual-write interval.

Gate: forced worker crash/restart reclaims tasks safely; late/stale leases cannot write results; two clients do not lose queue/votes; corruption causes clear error rather than empty-data overwrite. External model billing remains at-least-once/unknown-outcome aware.

### Phase 3 — Typed providers and retry engine

3.1 Add `providers/base.py`, `providers/commandcode.py`, `retry_policy.py`, `tests/test_provider_retry.py`.
3.2 Tests cover primary initial attempt + four retries, exact fallback ID, GLM retries, permanent auth failure, account circuit break, Retry-After, no high downgrade and no full-task replay.
3.3 Add `providers/xai_oauth.py` client contract and narrow `scripts/xai_broker.py`; broker lives where Hermes-managed credentials live, refresh owner serialized. Client cannot supply endpoint/headers/raw credential request.
3.4 Test native Responses outputs, function calls, citations and reasoning-only/empty responses; preserve unknown-outcome IDs. No silent paid-key fallback.

Gate: deterministic fake-transport tests plus small real probes; mocked success is never labeled live provider success. Exact credential scope and broker authentication must be decided before deployment.

### Phase 4 — Message channel and scheduler

4.1 Add `channel.py`, `scheduler.py`, `tests/test_channel.py`, `tests/test_scheduler.py`.
4.2 Implement role-authored immutable messages, cursor acknowledgments, targeted wakeups and outbox delivery in one transaction.
4.3 Add priority/reserved reviewer capacity, account-scoped concurrency/backpressure, no-progress detection and graceful shutdown.
4.4 Tests: duplicate delivery, forged sender, cross-room references, restart during publication, orphan children, queue pressure and requester cancellation.

Gate: many dormant logical agents do not require many live processes; memory follows active work and bounded context. Resource pressure is observable without silently dropping tasks.

### Phase 5 — Policy broker and code sandbox

5.1 Add `tools/policy.py`, `tools/search.py`, `tools/fetch.py`, `tests/test_tool_policy.py`; block SSRF/redirect/private-IP destinations and enforce bounded downloads.
5.2 Add `sandbox/runner.py`, `sandbox/image/Containerfile`, `tests/test_sandbox_contract.py` only after selecting a supported runtime.
5.3 Negative tests: credentials absent, production paths inaccessible, network unavailable, path traversal/symlink export blocked, CPU/PID/memory limits enforced, cancellation cleans only owned processes.
5.4 Verify positive Python calculation/file artifact roundtrip in a disposable workspace. Record runtime/image digest and exact command/output.

Gate: actual isolation tests, not merely “container started.” Unsupported isolation fails closed.

### Phase 6 — Agent roles and independent review

6.1 Add `imagine.py`, `researcher.py`, `reviewer.py`; lead authors role prompts and review rubric before delegation.
6.2 Add filter `imagine_tasks[]` tool/schema plus durable dispatch keyed by source version and normalized research question, not filter verdict or chosen fallback model.
6.3 Test rejected-post dispatch, multiple questions, DeepSeek/Grok overlap in time, cross-agent follow-up and partial branch failure.
6.4 Implement renewable slices/checkpoints, step-level retries, mandatory pre-code/publication review and failed-review pause.
6.5 Test injected forum/tool instructions, forged reviewer messages, stale approvals, repetitive conversations and bounded cancellation.

Gate: useful novel work backed by sources; review blocks unsafe actions without blocking ordinary benign research. No fixed 3–6-ideas formatting or shallow arbitrary truncation of source context; retrieval supplies more source text when needed.

### Phase 7 — Read API and minimal UI

7.1 Add status-only projection + versioned artifact endpoints in `server.py`, facade in `store.py`, tests `tests/test_imagine_api.py`.
7.2 Add dot and drawer section in `public/index.html`, `public/app.js`, `public/styles.css`; no redesign of existing layout.
7.3 Implement cache-by-version and active-task polling; GET/hover must never create work.
7.4 Add automated browser cases for desktop and touch mobile; include WebKit/Safari coverage if available, otherwise report it as not tested.

Gate: before completion dot exists; failed/retrying visible; one failed branch does not erase ready content; dot and row gestures do not collide; page never gets covered by a hidden scrim; no unreviewed artifact content appears.

### Phase 8 — Monitoring, evaluation and staged rollout

8.1 Extend `scripts/monitor.py` and `scripts/host_monitor_wrapper.py` to durable incident cursors, role health and stalled leases; update the existing cron only after approval, then read back exact schedule/prompt/provider.
8.2 Add `tests/test_monitor_incidents.py`: failure then recovery between ticks, reviewer unavailable, circuit breaker, incident-storage failure, crash before cursor acknowledgement, duplicate wakeup.
8.3 Create fixed representative evaluation fixtures (release, technical comparison, low-value post with useful research lead, misinformation, injection, code-needed problem, no-useful-result case). Lead owns scoring rubric: novelty, grounding, uncertainty, relevance, harmlessness; also measure latency/tokens/search use/memory.
8.4 Shadow run on selected public posts with owner-approved budget; then owner-visible subset; only later broaden scheduling. Do not auto-prewarm old backlog or resurrect the previous prewarm_n=2 proposal.
8.5 Independent spec/security/code review; lead inspects final diff, test evidence and redaction before commit/push/deploy. Maintain feature flags and tested rollback.

Gate: meaningful reviewed findings, graceful failure, usable mobile interaction and measured resource envelope. No success claim based solely on unit tests.

## 11. Test evidence contract for all contributors

Each implementation handoff includes exact scope, settled interfaces/policy, known prior failures, and verification commands. Reports must include:

- Base commit and changed paths; no unrelated modifications.
- Exact test command, exit status and count; failing-before and passing-after evidence for bugs.
- Log/screenshot/artifact paths; whether data are fixtures, sandbox integration or live public inputs.
- External side-effect handles if authorized; lead reads back exact target before success claims.
- Provider/model/auth route actually used, reasoning parameter behavior and tool/citation events; redact credentials.
- Untested browsers/runtimes/capabilities and remaining failures.
- Learning/pitfall: what failed, root cause and reusable prevention. A report is not proof until evidence is inspected.

Suggested future commands: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`; `node --check public/app.js`. New browser runner will be specified once existing tooling is inspected. Never point mutation tests at port 8791 or the live public tunnel.

## 12. Decisions to confirm before implementation

1. Retry meaning: initial call followed by four retries; propose the same count for GLM. Permanent configuration/auth/safety errors get typed handling, not blind repeated calls.
2. Reviewer model and cadence: separate GLM role proposed; active delta every five completed tools or 60 seconds, plus pre-code/publication gates. Reviewer cannot override hard policy.
3. Long-work envelope: renewable 15-minute/40-tool slices, proposed two-hour unattended pause ceiling, measured concurrency rather than an arbitrary lifetime agent-count cap. Hard cost/memory totals must be owner-configured before enabling automatic runs.
4. Access model: recommend public read-only feed + owner-authenticated mutations/diagnostics. Login mechanism can remain local owner-session based; never place API keys in browser code.
5. Host deployment capability: rootless/strong sandbox and OAuth broker location remain prerequisites, not assumed installed. OAuth native searches require an actual capability pass with this account.

The primary direction is agreed; these are operational defaults and capability gates, not excuses to revert to the obsolete small-tool-budget design.

## 13. Final evidence addendum and reviewer reconciliation

### Independent read-only reviews

Three delegate review tracks completed: backend correctness; frontend/API/monitor architecture; xAI OAuth/native-tool integration. Their host-side completion transcripts are:

- `/home/young/.hermes/cache/delegation/live/deleg_16661721/task-0.log`
- `/home/young/.hermes/cache/delegation/live/deleg_16661721/task-1.log`
- `/home/young/.hermes/cache/delegation/live/deleg_d10ea6b8/task-0.log`

Automatic consolidated reports did not appear in the parent transcript; the lead recovered the completed transcripts through the approved host-agent read interface and checked the important findings directly against source. Those logs abbreviate long messages, so they are not represented as full verbatim audit reports. The source-cited findings in section 2 are the lead's reconciled review, not an unverified copy of a delegate verdict.

Accepted findings: stage-counter reset, whole-file concurrent state hazards, missing owner auth, static-prefix containment bug, credential argv exposure, transient UI success, late-body filtering and monitor gaps. Rejected recommendations from exploratory review: revert Grok to API-key-only, use the old small tool budget, or treat reviewer as an unrestricted production repair agent. Those contradict current requirements. A suggestion that Code Execution could be in-process when no sandbox binary exists is explicitly rejected: feature remains disabled until isolation works.

### Sandbox prerequisite discovery

Sidekick reported Python 3.11.15, SQLite 3.46.1, two CPUs, 2 GiB cgroup memory ceiling and limited free scratch disk. Lead independently confirmed SQLite 3.46.1 and no `docker`, `podman`, `bwrap`, `nsjail` or `runsc` on THIS sandbox's PATH. Sidekick found no Docker/Podman/containerd sockets and reported current feed RSS about 12.4 MiB; these are point-in-time observations, not a memory guarantee for future agents.

Host runtimes and enforceable isolation remain **unverified**. Do not install a nested privileged daemon or mount a host Docker socket into generated-code sandboxes as a shortcut. Capability discovery on the host and an approved isolated runner deployment are a separate implementation prerequisite.

### OAuth details verified against source and official documentation

Source snapshot: `/workspace/hermes-agent-src` HEAD `b81383ec215400cbbc7d9768cf4ce45a19f9092a`; sparse checkout files inspected with `git show HEAD:<path>`. This is an inspected snapshot, not proof of the exact running host revision.

- `tools/xai_http.py:296-325`: resolver may fall back to API-key auth; `prefer_api_key=True` explicitly favors paid-key auth because OAuth X Search has returned no-citation explanations (#88040).
- `tools/x_search_tool.py:143`: existing X-search wrapper requests that API-key preference. Therefore do not blindly reuse it for the user's OAuth-only branch. Assert provider identity and reject unintended paid-key fallback.
- `agent/transports/codex.py:675-687`: chat adapter may inject native `web_search`.
- `agent/codex_responses_adapter.py:442-450`: inspected built-in tool allowlist does not contain `x_search`. Starting a normal Hermes chat does not establish native combined Web/X Search support. Prefer a narrow direct Responses broker with the two explicitly permitted tools, retaining Hermes credential ownership rather than cloning credential files.
- OAuth capability smoke tests must distinguish genuine empty results from degraded no-search prose. Use a known-public-source test query and inspect actual native tool events plus citations; absent citations on an arbitrary query alone does not prove a failure.
- Native server-side search is not a sequence of locally interceptable calls. Review before submitting each request, restrict capabilities/egress content, inspect events when available, and review the result before publishing. Do not promise a reviewer can approve every remote internal search before it executes or that disconnect guarantees remote cancellation.

Official sources consulted:

- https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth
- https://hermes-agent.nousresearch.com/docs/user-guide/security
- https://docs.x.ai/developers/tools/web-search
- https://docs.x.ai/developers/tools/x-search
- https://docs.x.ai/developers/tools/code-execution
- Public catalog: https://api.commandcode.ai/provider/v1/models

Public API docs establish API feature shapes, not this account's OAuth entitlement. No live authenticated provider/tool probe, sandbox isolation test, new research feature, migration, cron modification or deployment was performed in this planning task. Existing 64 unit tests passing does not cover the defects found here; phase-1 regression cases are mandatory.
