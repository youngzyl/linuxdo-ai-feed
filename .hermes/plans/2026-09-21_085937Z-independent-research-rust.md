# Independent Research Harness Implementation Plan — Revision 2

> **For Hermes:** Use subagent-driven-development for implementation. Delegate hands-on changes/tests to the persistent sidekick; lead owns role policies, resource configuration, final diff review and deployment decisions.

**Goal:** Run useful, autonomous research alongside the read-only linux.do feed for long periods with low memory/disk use, prioritizing service stability over result latency.

**Architecture:** Keep the existing Python collector/filter/frontend service initially. Add a single Rust research daemon with local SQLite on tcstw, subject to access/runtime checks. Imagine and Grok are independent research peers, each capable of completing a task; neither is a mandatory stage of the other. GLM reviews safety; the existing Hermes watchdog becomes the operational supervisor.

**Tech Stack:** Proposed Rust + Tokio + serde + reqwest/rustls + rusqlite, local SQLite WAL, existing vanilla JS frontend, isolated remote code containers. Python is an on-demand analysis language and existing feed runtime, not a new full agent process per research task. Dependencies and versions must be locked during the build spike; none are installed by this plan.

**Status:** Revised implementation specification, not a research deployment claim. The discovery snapshot below is dated 2026-09-21. Subsequent user authorization enabled deployment: as of 2026-09-22 the feed runs on tcstw with a GitHub Pages frontend; Rust/toolchain and an isolated storage spike are prepared. The production research harness is not implemented or enabled. See [current deployment status](../../docs/deployment.md) for the actual running system, completed gates and remaining credential/capability blockers.

**Precedence:** This document supersedes conflicting choices in `2026-09-21_161455-imagine-multiagent.md` and `docs/PLAN-imagine.md`. The earlier source audit R1–R11 and baseline evidence still apply. In particular, mandatory paired Grok starts, live group-chat workflow, Python-only research runtime and escalating routine reviewer decisions to the owner are superseded.

---

## 1. Settled interpretation of the latest user request

- Both research peers can search, reason, investigate, generate hypotheses and produce a complete cited result. DeepSeek uses SearXNG and bounded public-page fetch; Grok additionally uses its native X/Web tools after OAuth capability tests.
- Provide the same brokered Code Execution capability to either peer once that provider's custom-tool roundtrip is proven. Native X Search is Grok-specific, not something to falsely claim for DeepSeek. Until a capability passes, advertise the actual smaller tool set explicitly.
- Do not automatically run Grok after/beside every Imagine task. Route each task to one peer; selected paired runs are independent comparisons, not a dependency barrier. Paired runs can execute sequentially under low resources.
- No live peer chat in v1. Neither sees the other's draft before its own answer is frozen. This reduces anchoring, duplicate work and prompt-injection propagation, and makes comparisons meaningful.
- Reviewer: independent CommandCode `z-ai/glm-5.3-flash` role. Its job is harmlessness of actions/results, not deciding which research is more interesting.
- Main Imagine route remains CommandCode `deepseek/deepseek-v4.1-flash` high, then `z-ai/glm-5.3-flash`. Initial call plus four retries is retained as the working interpretation; same retry count for fallback is configurable. Permanent auth/config errors and safety blocks are not retried blindly.
- Keep the UI agreement: one status dot and the existing drawer, not two full answers side by side.
- Memory/OOM, available disk, provider availability and cost headroom are the primary admission constraints. Fast completion is secondary.
- Filter sees global workload but can still propose research. Proposing a task does not reserve a running process or guarantee immediate execution.

## 2. Read-only discovery and current blockers

Lead-verified on 2026-09-21:

- Current `pipeline.py:147-179` sleeps **15 minutes after a cycle completes**, not on an exact quarter-hour wall-clock schedule. Failure backoff can increase that delay.
- `config.json` filter batch size is 8. Existing successful-cycle log sample: 94 cycles over 22.26 hours; excluding initial backfill, 399 new posts, **17.92 new posts/hour**. This is a one-day observation, not a long-term forecast or research arrival rate.
- No Imagine dispatcher exists today. Questions per post, research duration and completion throughput have not been measured. Do not infer task arrival rate from picked-post count; rejected posts can generate questions too.
- Live state reports filter model `deepseek-chat` and key source `env:LINUXDO_AI_FILTER_API_KEY`. Current filtering is not evidence that the intended CommandCode route is operational.
- Existing watchdog `235d5a42aa94` is enabled every 20 minutes, with `linuxdo_ai_monitor.py` as monitor and continuity enabled. It has not been changed. User's spoken “Chrome job” is understood in this context as this cron job, not a requirement to run a Chrome browser continuously.
- `rustc` and `cargo` are absent from the current sandbox PATH. `/workspace` had approximately 772 MiB free at the probe. Do not install a Rust toolchain or build artifacts here before providing suitable build storage.
- Host-approved read-only SSH probe: `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 tcstw.youngzyl.me ...` failed with SSH public-key authentication denial (exit 255). No host-key bypass, credential guessing or remote changes were attempted. tcstw CPU/memory/disk/runtime remain **unverified**, regardless of old assumptions about its CPU count.

Supporting scratch report: `/tmp/imagine-revision-evidence.txt`. Some secondary process/RSS observations there are point-in-time self-reports and must not be used as capacity guarantees. Lead recomputed the post-arrival statistic above rather than adopting the report's mixed backfill-inclusive rate.

Deployment prerequisites: approved tcstw access, measured capacity/isolation, independent CommandCode credential provisioning, OAuth native-tool and custom-tool evidence. No secrets are to be pasted in chat.

## 3. Rust runtime and storage ownership

### Why Rust here

Rust is a reasonable choice for a small long-lived coordinator with bounded buffers and no per-agent interpreter. It is not a promise of a particular RSS or immunity to OOM. Long contexts, HTTP bodies, container workspaces and artifact retention can dominate either language.

Do not rewrite the working feed just to change languages. Build the new research service separately and compare idle/active RSS, disk growth and restart behavior. Compile release binaries in an approved build environment, not on the capacity-constrained serving host. Ship the binary, migrations and immutable code image, not a Rust build cache.

### Runtime layout

- One Rust daemon hosts scheduler, provider adapters, review coordination and status APIs. Logical sessions are database rows plus checkpoints, not full Hermes processes.
- A small explicitly configured Tokio runtime; synchronous SQLite work stays on a dedicated bounded database worker, never blocking the async event loop. Bound HTTP connections, response sizes, active context loads and blocking jobs.
- SQLite WAL on tcstw's local filesystem stores research tasks/events/reviews/config revisions. Short write transactions; no transaction remains open during a model request. Bounded cache, busy timeout, WAL checkpoints and tested backup/restore.
- Large source snapshots and artifacts are files addressed by server-issued references; database rows carry metadata/hash/size, not repeated large blobs. Enforce workspace/log/cache quotas and retention. Do not delete unresolved incident evidence silently.
- Feed state remains owned by the feed service during the first rollout. Its local SQLite transaction/outbox migration repairs R5/R11. Research state is owned only by Rust. There is no shared SQLite over SSH/NFS and no two-master writing of the same table.
- Feed outbox delivers idempotent research proposals to tcstw. Research events/result versions return through a durable cursor into a bounded local read cache. Remote outage never prevents opening the existing feed.

### Lightweight scheduler means

The scheduler is a loop inside the Rust daemon: wait for an event or next persisted due time, inspect capacity, lease eligible work transactionally, run one bounded step, save checkpoint, release resources. It is not another LLM or an additional heavyweight scheduling platform.

No queue-wide in-memory load; query an indexed page of eligible tasks. Dormant/retry-wait/reviewer-wait tasks do not hold a research slot or a container. Provider in-flight requests do hold their appropriate network/in-flight slot until completion or uncertain termination handling.

## 4. Independent peers, evidence and one UI result

Each task has `task_id`, source/version reference, research question, assigned peer, optional comparison group, priority reason, status and budget ledger. Each peer has separate context and workspace.

Internal event types replace the general chat room: task proposed/accepted, step started/completed, artifact produced, review requested/decided, retry scheduled, operational incident. Sender, task, permissions and event sequence are supplied by trusted code, never inferred from model-authored role labels.

A peer submits actions/artifacts to reviewer; reviewer returns continue/revise/pause/block. Routine feedback goes back to that peer, not to the other researcher. Both peers may use public information independently. Future intentional collaboration is a separate, explicit experiment, not the default.

Paired comparison uses the same source snapshot and question with each peer's context independent. Record actual tools/cost/time used; tool asymmetry must be disclosed rather than treated as a controlled model benchmark. A separate offline evaluation can compare grounding, novelty, uncertainty and resource use. Reviewer is not repurposed into that quality grader.

UI v1 uses one canonical reviewed artifact. Default peer output remains canonical; when unavailable, another explicitly assigned and reviewed result may replace it with provenance recorded. A later comparison result does not automatically overwrite or merge into the displayed answer. No prerequisite that both peers must finish.

## 5. Global workload feedback and backpressure

Filter receives a compact, trusted scheduler snapshot:

- queued/running/retry-wait/review-wait counts by peer;
- oldest eligible queue age, recent arrival/completion rates, observed task-duration distribution;
- current concurrency and revision, code-slot availability, resource-pressure category;
- provider circuit/quota status and snapshot age;
- summaries/IDs of relevant existing tasks for deduplication, not every full research context.

Filter may propose new questions, reference an existing question, or suggest deferral/priority. It cannot author capacity facts or directly change concurrency. A stale snapshot is labeled stale; the scheduler still makes admission decisions against current local state.

Admission is separate from proposal. Durable bounded outbox and finite disk remain real constraints: at a storage high-water mark, return a retryable deferred status rather than falsely acknowledging persistence or silently dropping work. Record pressure so cron can recommend policy changes. Queue aging/fairness prevents old tasks from starving; filter cannot label all work urgent to bypass this.

Startup operating proposal: one active research task total across peers, tunable to two after headroom measurements; one active Code Execution container; at most one reviewer request, with reserved capacity. Research API waiting is not the same as CPU utilization. The container and service each need enforced limits plus host-wide reserve; exact MiB/PID/disk values require tcstw measurements.

Long-term stability requires completion throughput to keep up with admitted task arrivals. If proposals grow faster, no language choice can fix an infinite backlog on finite disk. Respond with better deduplication, priority/deferral, adjustable work depth and owner-discussed dispatch policy—not only more workers.

## 6. Timing and renewable work

**Two hours means a provisional active-work ceiling for one research task/peer**, not the lifetime of the daemon, not how long every task should take, and not time sitting in the backlog. In comparison mode each peer is a separate task and shared project budgets still apply.

Track separately:

- task age/wall-clock latency since proposal;
- active model/tool step wall time, including in-flight model generation and retries actually running;
- queue, backoff, quota and reviewer-wait time;
- CPU seconds, tool count, token usage and cost estimates.

Persist accounting across restart/fallback/checkpoint/resume; resuming does not reset the cumulative budget. Pausing at two hours saves the task and releases its execution resources. Cron can assess progress and request extension under an approved envelope; without such approval the task stays paused. A hung remote request cannot be made harmless by calling its time “paused.” Keep in-flight accounting and outcome-unknown status until reconciled.

15 active minutes / 40 local tool invocations are starting checkpoint hints, not immutable quality limits. Normal useful work may continue across checkpoints under the cumulative allowance. Native server-side search counts/events must be recorded where provided; local counters alone do not bound invisible provider work or cost.

Fair scheduling uses checkpoints to yield; one long research task cannot occupy all progress capacity for the whole day. Do not force-kill an otherwise healthy step merely because a fairness slice elapsed; apply the actual per-step deadline and cancel semantics.

## 7. Reviewer independence without constant human interruption

Fixed policy/context is owned by the application and loaded separately from research data. Use a fresh compact review context for each review or a bounded trusted finding history; do not accumulate a shared researcher/reviewer conversation. Raw web/X/code output is attached as explicitly untrusted evidence. Summaries inherit that trust status; labeling alone is not a complete prompt-injection defense.

Reviewer examines concrete proposed actions, arguments, provenance and observed outcomes. It does not execute commands copied from a page. It has no shell, secret access, production-write tool or self-editable policy. Tool availability and authorization come from the application, not its judgment. A separate role reduces contamination risk but cannot guarantee it will never be fooled, particularly when Imagine's fallback and reviewer use the same model family.

Two layers, not an LLM approval on every read:

1. Cheap enforced boundaries: authenticated role/task scope, no secrets/production mounts, bounded paths/output/resources, allowed public fetch destinations and isolation. These remain effective when models misjudge content.
2. Semantic review: meaningful deltas, suspicious/new-risk actions, generated-code execution and publication. Existing permitted read-only search/fetch does not need a fresh LLM approval every time. Start with the earlier five-tool/60-second-with-new-progress review hint and tune its frequency from evidence.

Routine issue: revise or checkpoint-pause, record incident, let cron diagnose repeated false positives/failures; no user confirmation prompt for every ordinary step. Severe evidence of boundary breach, credential exposure, destructive action or persistent unsafe operation: block/contain the affected branch and notify promptly. Reviewer outage pauses gated code/publication, not feed browsing or already-reviewed content.

Learning means versioned evidence/test cases and proposed policy refinements. It does NOT mean ingesting web text as new policy or automatically relaxing a safety boundary because previous executions succeeded.

## 8. Operational cron / supervisor contract

Extend the existing project watchdog, not an unrelated cron and not one full Hermes per research task. Runtime emergency admission control is immediate in Rust; it cannot wait for cron to prevent OOM.

Proposed behavior:

- Cheap scan every five minutes for new incident cursors, stale leases, queue growth and resource state. A bounded periodic summary-window event must wake it for throughput/tuning review even when there are no failures. Do not gate solely on incidents and then claim adaptive performance review.
- When meaningful changes exist, the cron agent reviews evidence, task progress, rejected actions, duplicate work, tool effectiveness, queue arrival/completion rates and resource trends. It can ask for targeted diagnostics and produce policy/test proposals.
- Automatic soft tuning only within an owner-approved envelope: research concurrency (initially 1–2), checkpoint interval, local tool slice, review cadence, fairness and scheduling priorities. Small versioned changes, one experiment at a time, minimum observation interval/hysteresis, before/after metrics and automatic rollback on regression. No oscillating settings every tick.
- Runtime can always lower admission or pause on pressure. Cron may not raise cgroup limits, remove isolation/SSRF/auth checks, grant tools, transfer secrets, change models/providers, increase cost ceilings or extend beyond the approved total-time envelope.
- Code changes, security-rule changes and new capability requests are raised for discussion with evidence/regression tests before implementation. The previous broad repair authority should be narrowed in the revised watchdog prompt; it must not silently deploy a production patch.
- Routine recovery/tuning is logged without Telegram noise. Repeated unresolved failures get a concise digest; severe incidents get an immediate alert through a separately configured event path, not merely a five-minute poll.

Tuning records: proposer identity, old/new values, reason, supporting metric window, authorized envelope version, application time, evaluation deadline and rollback decision. Configuration updates use compare-and-swap revisions. Researchers/reviewer cannot use the tuning API.

## 9. tcstw connection and isolation plan

Access is currently blocked by SSH authentication. User must establish/approve an existing working host identity or a dedicated restricted identity through a secure channel; never paste private keys/passwords into chat. Do not weaken StrictHostKeyChecking or TLS verification.

Proposed v1 transport after access is established:

- A persistent authenticated SSH stdio RPC link to a fixed remote service entrypoint, with bounded framed messages, reconnect/backoff and application idempotency. No publicly exposed research-admin port.
- Dedicated service identity; pinned/verified host key; forced command and no PTY, agent forwarding, arbitrary shell or unrelated port forwarding. Deployment identity is separate from runtime identity.
- Application role scopes separate feed proposal/status/artifact operations, OAuth relay operations and supervisor configuration operations. Do not give the browser or feed connector runner/deployment privileges.
- OAuth remains owned/refreshed on the existing Hermes host. A narrow local relay claims authenticated OAuth request envelopes from the remote daemon, calls the approved model/tools using existing credential ownership, and returns bounded events. It never transfers auth.json/refresh tokens to tcstw. Pause only the Grok branch if that relay is unavailable.
- The relay enforces public-data-only payloads, fixed model/tool/endpoint constraints, per-request and project budgets, expiry and request idempotency; it must not become an arbitrary authenticated forwarding proxy. Actual source adapter and native tool/citation capability tests remain mandatory.
- CommandCode project key provisioning location is an explicit deployment choice, not a copy-all-environment operation. Trusted provider broker gets it; generated code never does.
- Runner resides on tcstw as a separate restricted service/identity. Only the trusted runner can invoke its configured runtime; code containers cannot see runner sockets, broker processes, other tasks or host credentials. Network disabled for code, controlled public-network fetch elsewhere. Rootless/container availability and enforceable limits require actual negative tests.
- New state/artifacts live on tcstw local disk. Host keeps bounded cache/outbox, and transport failure does not trigger unbounded buffering. SQLite backup uses a consistent backup procedure, not copying only a live database while ignoring WAL.

Residual risks include kernel/runtime escape, provider-side prompt injection and remote cancellation uncertainty; a container or a reviewer is not a universal safety proof.

## 10. Concrete implementation sequence

All new paths below are proposed, not claims that code exists. Every code change gets a failing-before test, implementation, focused test, independent review, then lead-controlled commit. No mutation test targets live port 8791.

### A. Access, build and capability gates

Files: `docs/deployment.md`, `deploy/runtime-rpc-policy.md`, later `harness/Cargo.toml` and `Cargo.lock`.
1. Resolve tcstw identity securely; measure RAM/headroom/cgroups/disk/runtime and verify host identity.
2. Select build storage/architecture; compile a minimal release daemon in the build environment; measure idle RSS and startup, without claiming Rust is automatically smaller.
3. Run narrowly scoped real DeepSeek/GLM high+tool and OAuth Web/X/custom-tool probes; keep provider identity/tool events/citations and redacted evidence.
4. Test positive code artifact roundtrip and negative isolation/resource cases on tcstw before enabling Code Execution.
Gate: read-only source/docs support is not a substitute for these live tests.

### B. Existing feed correctness repairs

Modify `pipeline.py`, `store.py`, `http_util.py`, `server.py`, `filter.py`, `learn.py`, `public/app.js` as identified by R1–R11. Add focused tests in `tests/test_pipeline_health.py`, `test_http_policy.py`, `test_server_security.py`, `test_filter_versions.py` and browser fixtures.
Order: stage counters/cooldown/static containment/secret argv; owner-only writes and acknowledged UI mutations; separate manual override from model verdict; source-version re-evaluation and strict partial-output accounting. No new remote agent rollout yet.
Commands: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`; `node --check public/app.js`. Expected: old suite plus new regressions pass; capture actual counts, not a predeclared success total.

### C. Transaction/outbox boundary

Create `db.py`, `migrations/001_feed.sql`, `migrations/002_outbox.sql`, `research_client.py`, `tests/test_feed_outbox.py`, `scripts/migrate_state.py`; modify `store.py` as a compatibility facade.
Tests: copy-only migration count/content checks, explicit feedback survives model races, outbox replay cannot duplicate a task, remote down does not block reads, full outbox produces visible backpressure, backup/restore and rollback work. Cut over production only with snapshot and controlled writer pause.

### D. Minimal Rust durable scheduler

Create `harness/src/{main,db,scheduler,rpc,resources,config}.rs`, `harness/migrations/001_research.sql`, `harness/tests/{leases,restart,backpressure,rpc_scope}.rs`.
Contract: states include proposed/queued/running/retry_wait/review_wait/paused/ready/failed; persisted step/attempt/lease epoch; next_attempt_at; usage counters; incident cursor; source snapshot identity. One SQLite owner. Stale lease epoch cannot publish.
Tests: crash between request and receipt produces outcome_unknown, duplicate delivery, lease expiry, stale completion rejection, restart-safe cumulative budget, queue page bound, resource pressure stops admission, reviewer capacity is not starved. No false exactly-once billing guarantee.
Commands: `cargo test --locked --manifest-path harness/Cargo.toml`; `cargo clippy --locked --manifest-path harness/Cargo.toml --all-targets -- -D warnings`. Commands are future acceptance commands, not run in this planning turn.

### E. Providers, tools and separate review

Create `harness/src/providers/{mod,commandcode,xai_relay}.rs`, `harness/src/{retry,agent,review}.rs`, `harness/src/tools/{mod,search,fetch,code}.rs`, `harness/src/bin/runner.rs`, `sandbox/Containerfile`.
Lead authors prompts/review rubric before implementation. Implement typed transient/permanent failures, DeepSeek retry-to-GLM checkpoint continuation, optional OAuth peer, shared capability contracts and fixed policy separation.
Tests: independent Imagine-only and Grok-only tasks; no peer data leakage before frozen output; failed Grok does not block Imagine; reviewer prompt injection and forged approval; safe public research is not unnecessarily gated; sandbox CPU/memory/disk/network/mount boundaries actually enforced; recovered failures remain visible to cron.

### F. Filter dispatch and global snapshot

Modify `filter.py`, `pipeline.py`, `research_client.py`; add `tests/test_research_dispatch.py`.
Lead defines proposal schema with source_version/question/assigned-peer preference/priority reason/existing-task refs. Scheduler assigns identity and authorizes actual peer/capabilities. Tests cover rejected-post dispatch, multiple questions, deduplication, snapshot staleness, resubmission after remote outage, age fairness, queue pressure and no auto-dispatch of old backlog.

### G. Minimal UI and controlled first live version

Modify `public/{index.html,app.js,styles.css}`, `server.py`; add `tests/test_research_projection.py` and browser fixture tests.
One dot, reviewed canonical result, retained original reading-column placement. Mobile touch tests, pending/retrying/paused/failed states, stale remote result label, no unsafe HTML and no billable GET/hover. Feed works with research feature disabled.
Start with new public posts only, one research slot and one code slot; do not launch the historical backlog. Record proposal/admission/completion rates and duration distribution. Introduce a small explicit paired-comparison sample only after steady single-peer operation. Amount is configuration, not an invented validated rate.

### H. Operational supervisor and adaptive tuning

Modify `scripts/monitor.py`, `scripts/host_monitor_wrapper.py`; create `harness/src/{metrics,tuning}.rs`, `harness/tests/tuning.rs`, `tests/test_monitor_incidents.py`.
Lead authors revised watchdog mission/authority boundary. Update existing job only after endpoint/monitor readiness; read back exact job target and settings.
Tests: failure-recovery between polls, normal progress periodic assessment, unresolved incident cursor, CAS conflict, unauthorized envelope expansion rejected, hysteresis, rollback, lower capacity on OOM pressure, severe event notification path, routine issues do not spam user.

## 11. First rollout acceptance and unresolved decisions

A deployed feature is complete only with real provider/runtime evidence, restart/disconnection recovery, bounded memory/disk and usable mobile results. Unit tests alone are insufficient. Measure scheduler + relay + code container together, including peak/cgroup memory and disk growth, not only daemon RSS.

Need owner input only for inaccessible prerequisites or privilege changes: a working approved tcstw connection, secure credential provisioning if absent, and the final resource/cost envelope after measuring tcstw. Proposed cadence/concurrency can be adjusted in that envelope; code/security changes still come back for discussion.

No implementation or deployment occurred in this revision turn. Current live filter and watchdog continue unchanged.

## 12. Independent stress-review reconciliation

Completed report: `/home/young/.hermes/cache/delegation/subagent-summary-0-20260921_165949_163449.txt` (read in full). This was source/design review, not deployed Rust or tcstw testing.

Additional explicit acceptance cases:

- Disconnect the feed/cron transport while tcstw is executing: no client may infer that the remote worker died and start a replacement scheduler/task. Only the authoritative scheduler can transition its lease; remote provider outcome may remain unknown.
- Shared Code Execution capability means the same runner contract/image, **not a shared writable workspace**. Separate peer/task workspaces and containers; any runtime reuse requires verified cleanup. Test that peer B cannot read peer A's files, including after a failed cleanup or cancelled run.
- Stale review approval cannot authorize a changed artifact, action or capability version. A permissive reviewer response must still fail the negative SSRF/path/credential isolation tests.
- Include WAL, source/artifact files, container writable layers and abandoned workspaces in disk accounting. Cleanup targets only service-owned expired resources, preserves unresolved evidence, and never performs a broad host/container prune. Existing host services count against available headroom.
- A paired-comparison retry is idempotent within its assigned peer; changing peer is an explicit new task/assignment revision, not a second accidental retry. Test both peer-only completion directions.

Do not adopt speculative statements from the review as measurements: two CPU cores alone cannot establish that the proposed workload will OOM. Prefer enforced sandbox cgroup containment and measured host reserve; do not assume an OOM-score tweak provides isolation. A Unix-domain socket is distinct from TCP loopback, and neither alone authenticates remote clients.

Keep notification severity proportional: an ordinary successfully blocked unsafe URL or model mistake is not automatically a user-facing emergency. Actual boundary compromise, credible sensitive-data exposure or sustained severe attempts trigger containment/notification; routine rejects remain review/cron evidence. Actual retry execution counts toward active work; only retry **waiting** is excluded.
