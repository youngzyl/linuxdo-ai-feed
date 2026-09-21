# PLAN — imagine: a parallel, tool-using "发散" stage

Status: **SUPERSEDED — historical draft, not an implementation instruction.** The approved direction is [Independent Research Harness — Revision 2](../.hermes/plans/2026-09-21_085937Z-independent-research-rust.md). In particular, Grok is an independent peer, not the fallback; the startup concurrency proposal is one research task, not eight. See [deployment status](deployment.md) for what actually runs.
Model target chosen by the user: CommandCode `deepseek/deepseek-v4.1-flash` with reasoning high,
fallback `grok-4.6` reasoning high. Search: local SearXNG. UI: the left-hand dot stays; mobile
reuses the drawer. Failures of *every* AI call must be visible to the cron watchdog.

---

## 1. What it is

A second stage next to the filter. The filter answers "is this worth reading?" — imagine takes a
post as a *starting point* and, with web tools, develops it: associations, consequences, what it
could be used for, what is worth checking next. It never just paraphrases the post.

Fusion analogy the user asked for:

* **filter = lead.** Besides scoring, it can *hand out the job*: a new per-topic output field
  `imagine: true|false` plus optional `focus: "<what to develop>"`. It may dispatch a post that it
  judged **low value**, because "worth reading" and "worth developing" are different questions.
* **imagine = the sidekick pool.** One independent background agent per topic, each with its own
  prompt, tool set and budget. **Concurrency is not artificially limited**: a pool with a tunable
  cap (`imagine.max_workers`, default 8, 1–32) so parallel execution keeps wall-clock time down.

Trigger sources, all idempotent per (topic, prompt_version, model):

| source | when | note |
|---|---|---|
| filter dispatch | the filter marks `imagine: true` | works for rejected posts too |
| on demand | you click the dot on a row | `POST /api/imagine/<id>` |
| prewarm | newest N picked posts each cycle (`imagine.prewarm_n`, default 2) | keeps the dot useful without burning budget |

## 2. The per-topic agent loop

Input: title, body (cap 2000 chars), tags, category, the filter's one-line reason, thread stats.

Tools (bounded — **max 3 calls, 60 s wall clock, total output ≤ 3k chars**):

* `search(q)` → local SearXNG `http://127.0.0.1:9888/search?q=…&format=json`, top 5
  (title / url / snippet). Verified reachable from the service host.
* `fetch(url)` → readable text of a page (reuses the existing curl transport, `r.jina.ai`
  fallback, cap 3000 chars).

Output (tolerant parsing, same recovery code as the filter):

```json
{
  "ideas": [{"title": "<=20 字", "body": "<=120 字"}],
  "links": [{"url": "…", "title": "…"}],
  "trace": "一句话说明它搜了什么、为什么"
}
```

3–6 ideas. Stored with `model`, `prompt_version`, `at`, and the raw tool trace.

### Retry / backoff (explicitly requested)

Attempt ladder `1s → 5s → 20s → 60s` (±20 % jitter), up to 4 attempts per task. Each failed
attempt appends to `logs/failures.jsonl` with `stage="imagine"`, attempt number, next delay and
context (topic id, model, whether the failure was in a tool or in the model call). When all
attempts fail: mark the topic `imagine.status="failed"` (grey dot with a retry affordance), count
it, and after `imagine_consecutive_error_threshold` (default 3) failures raise
`/health → attention.needed` so the watchdog steps in.

## 3. Model

* primary: CommandCode `deepseek/deepseek-v4.1-flash`, requested with `reasoning_effort: "high"`.
  Supported-or-not is probed on the first call: on HTTP 400 the request is re-sent without the
  parameter (same pattern already used for `response_format` json mode). Config:
  `imagine.model`, `imagine.reasoning_effort`.
* fallback: `grok-4.6` with reasoning high. Needs an xAI key/endpoint in the service env — the
  config slot (`imagine.fallback_model`, `imagine.fallback_base_url`) is filled in now, activated
  when a key exists. No pretending it works without one.
* **Open item:** the service currently has no CommandCode key in this sandbox (the running filter
  uses a DeepSeek-direct stand-in). Imagine inherits the same key resolution
  (`commandcode_apikey` → `.env`), so it switches to the real CommandCode id the moment the key is
  dropped into `.env` — no code change.

## 4. Storage & API (what "带摘要" means)

`/api/state` is fetched on **every** page load / refresh and carries every topic (currently 315).
Putting 68 × 3–6 idea bodies in there would add hundreds of KB for content nobody has opened yet.
So:

* topic gets a light summary only:
  `imagine: {status: "queued"|"running"|"ready"|"failed", idea_count: n, model, at, prompt_version}` —
  exactly what the dot needs to render.
* the bodies are fetched on demand: `GET /api/imagine/<id>` → `{ideas[], links[], trace, model, at}`;
  `POST /api/imagine/<id>` queues that one topic immediately (what clicking the dot does).
* `/health` gains `imagine: {queued, running, ready, failed_total, failed_consecutive, workers, last_error}`.

## 5. UI

* The list row gains **no text**. In the left gutter: a 6–8 px dot, only when there is a state —
  hollow ring (pulsing) = queued/running, filled dot = has ideas, grey dot with a slash = failed
  (the popover offers retry). `title` + `aria-label` for screen readers/hover, nothing visible.
* Desktop: hover ≥250 ms or keyboard focus → small popover (ideas, links, one-line trace,
  footnote `模型 · 时间`); `Esc` closes; click pins it. Living in the left gutter keeps it clear of
  "click row = preview" and of the planned swipe-to-vote.
* Mobile: tapping the dot opens the **existing drawer anchored to the 发散 section**. No second
  overlay pattern; try it, iterate if it feels bad (user's instruction).
* The drawer's 发散 section is the single canonical reading surface: ideas 3–6, links, trace,
  footnote with model + time — same content as the desktop popover.
* Naming: Chinese label in the UI (「发散」), `imagine` in code and config. **No model name in the
  UI chrome** — it only appears in that section's footnote, matching the existing "筛选模型" footnote.
  (Answered: there is no reason to put the name in the UI; the dot needs no label.)

## 6. Monitoring

* Every AI failure (filter **and** imagine) lands in `logs/failures.jsonl` (stage-separated) and in
  `/api/failures`; repeated failures flip `attention.needed`, which the existing 20-minute cron
  watchdog reads. The watchdog's prompt will be extended to cover imagine failures explicitly and to
  summarise trends, so "what failed, why, what fixed it" accumulates instead of being re-derived.
* `/health` exposes the imagine queue so a wedged pool (queued > 0 but nothing running) is visible
  rather than silent.

## 7. Build order

1. `imagine.py` — task loop, tools (searxng/fetch), retry ladder, tolerant JSON, prompt v1.
2. Worker pool (bounded concurrency, background) + `/health.imagine`.
3. Filter prompt gains `imagine` / `focus` (+ tests that a rejected-but-dispatched post is queued).
4. API: `GET/POST /api/imagine/<id>`, `/api/state` summary only.
5. Frontend: dot (3 states) + desktop popover + drawer section.
6. Update the cron watchdog prompt + README + CONTRACT.
7. End-to-end on real posts, screenshots for review.

Unit tests cover: pool concurrency and cap, per-(topic,version) dedup, retry ladder and jitter
bounds, tool budget enforcement, timeout handling, JSON recovery, failure journal + attention
threshold, and `/api/imagine` round-trip.

## 8. Open questions (need a yes/no before step 1)

1. `imagine.max_workers = 8` (1–32) as the default?
2. `imagine.prewarm_n = 2` per cycle (filter-dispatched posts are not subject to this cap)?
3. Section label 「发散」, code `imagine`, no model name in the UI chrome — agreed?
4. `grok-4.6` fallback stays a config-only slot until an xAI key is provided — agreed?
