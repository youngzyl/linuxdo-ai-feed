# linuxdo-ai-feed

A three-column reading desk for the **人工智能** tag on linux.do, filtered by an LLM.

**Live reader:** https://youngzyl.github.io/linuxdo-ai-feed/

The feed runs independently on tcstw; GitHub Pages serves only static assets. See
[deployment and access](docs/deployment.md) for owner authentication, the current model,
release evidence and research status. The research harness is **not live** yet.

Every cycle the collector pulls the tag's topic list (`/tag/444-tag/444.json`) read-only,
fetches the opening post of each new topic, and asks the configured filter model one question per
batch: *is this actually worth reading?* The page then shows the result as three
aligned columns — everything (全部), the picks (精选), and your bookmarks (收藏) — so the filtering
itself is visible. A bookmark is a separate signal from a filter preference: adding or removing one
never votes, and a 纳入精选 / 排除 vote never edits the bookmark list.

```
linux.do tag 444 ──► collector (list + OP body, paced, backoff)
                       │
                       ▼
                  state.json  ◄── filter (deepseek-v4.1-flash, batched, score/reason/summary)
                       │
                       ▼
              http server ──► /api/state ──► three-column page (PC + mobile)
                       └──────► /health, /metrics, /api/failures ──► watchdog / RCA
```

## Quickstart

```bash
cd linuxdo-ai
export commandcode_apikey=...        # independent key, or put it in .env (gitignored)
python3 run.py cycle                 # one fetch+filter cycle, prints a JSON summary
python3 run.py serve                 # scheduler + HTTP server on 127.0.0.1:8791
scripts/serve.sh                     # same, supervised (restarts on crash, logs to logs/serve.log)
python3 -m unittest discover -s tests
```

No third-party Python packages: stdlib only, Python ≥ 3.11.
Requires the `curl` binary (see *Transport* below).

Environment variables:

* `commandcode_apikey` (or `COMMANDCODE_API_KEY`) — **required for filtering**. The key
  value never appears in a response, a log line, or `state.json`; `/health` only
  reports `key_present` and the source label.
* `linuxdo_cookie` — optional linux.do session cookie, only if detail fetches start
  getting Cloudflare-challenged on your network.
* `LINUXDO_AI_TRANSPORT` — `auto` (default) / `curl` / `python`.
* `LINUXDO_AI_ALLOW_POOL_KEY=1` — dev-only fallback to `~/.hermes/auth.json`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the three-column page |
| GET | `/api/state` | full payload: topics with `state` (`pending`/`picked`/`rejected`) + filter verdicts |
| POST | `/api/refresh` | kick a cycle now (returns immediately) |
| GET/POST | `/api/queue` | 收藏 (bookmarks), the right column: `{"queue":[id,...]}` / `{"add":id}` / `{"remove":id}` |
| GET/POST | `/api/feedback` | preference votes: `{"id":123,"vote":"keep"|"skip","note":"..."}` or `{"id":123,"vote":"clear"}`. Next filter cycle injects the most recent keep/skip examples into the prompt. |
| GET | `/health` | status, counters, `attention.needed` |
| GET | `/metrics` | Prometheus text |
| GET | `/api/failures?n=20` | failure journal (newest first) — the RCA feed |
| GET | `/fixtures/state.sample.json` | offline fixture for UI work |

`GET /api/state` shape is specified in [CONTRACT.md](CONTRACT.md).

POST queue/feedback/refresh and GET feedback/failures require the owner bearer token.
Without a configured owner token those routes fail closed; anonymous reading still works.
The browser **管理** button stores the token in sessionStorage only. Cross-origin access
uses an exact origin allowlist; CORS does not replace authentication.

## Monitoring and self-healing

Failure handling is layered, and the last layer is an agent:

1. **Per request** — `curl` transport errors, Cloudflare interstitials (403/429) and
   timeouts raise a typed `HttpError`; every failed attempt is journaled to
   `logs/failures.jsonl` with stage, attempt number and the delay before the retry.
2. **Per cycle** — the list fetch retries 5 times with `5s → 20s → 60s → 180s → 420s`
   (±20 % jitter). Topic-detail fetches are paced (≥2.2 s); a challenge gets one
   delayed retry, then `r.jina.ai` as a read-only proxy, and a still-failed body is
   simply retried on the next cycle.
3. **Across cycles** — a failed cycle increments `consecutive_failures` and pushes the
   next attempt out to `interval × 2^min(n,4)` (cap 30 min). Any success resets it.
   Fetch and filter failure streaks are independent: a successful fetch cannot clear
   repeated filter failures. After `attention_threshold` (3) consecutive failures `/health` flips
   `attention.needed` to `true`, and so does a missing/invalid key or repeated filter
   errors.
4. **Agent on call** — `scripts/monitor.py` is a *deterministic* probe (stable output while
   healthy, so a cron worker stays asleep) printing
   `source=… service=… attention=… consecutive_failures=… stale=… last_error=<signature>`.
   The deployed wrapper reads the HTTPS URL in `deploy/active-target.json`; a failed
   remote probe never falls back to local files. Only unconfigured local/dev mode can
   fall back to `data/state.json` + `logs/`. A cron job watches it; when the output changes, the agent wakes up, does the
   RCA (reads `/api/failures`, `logs/failures.jsonl`, `logs/server.log`, reproduces the
   failing request with the collector's own headers), fixes the cause, restarts the service
   and verifies a full green cycle — instead of you noticing a silently dead collector days later.

   Hermes cron `monitor` paths must live in `~/.hermes/scripts/`, so register the wrapper once:

   ```bash
   cp <workspace>/linuxdo-ai/scripts/host_monitor_wrapper.py ~/.hermes/scripts/linuxdo_ai_monitor.py
   ```

   then create a cron job with `monitor=linuxdo_ai_monitor.py` and the RCA instructions from
   this section's step 4 as its prompt.

`python3 run.py status` prints the same health JSON the watchdog reads and exits
non-zero when `attention.needed` is true.

## Transport: why curl

linux.do sits behind Cloudflare, which fingerprints the TLS handshake: `urllib` and
`httpx` are answered with `403 Forbidden` no matter how browser-like the headers are,
while curl's handshake passes (verified — same headers, three clients, one 200 and two
403s). `http_util` therefore shells out to curl by default and keeps a stdlib fallback
for hosts without it. TLS verification is never disabled.

Credential-bearing requests use the stdlib transport, keep secrets out of curl argv,
and refuse all redirects. TLS certificate/hostname verification remains enabled.

Rate limiting is scoped per origin (scheme/host/port), not shared across providers: the first 403/429/503 puts the
client into a cooldown (30s → 60s → 180s → 300s, honouring `Retry-After`), a cooldown
longer than 75s aborts the current phase instead of sleeping through it, and the body
fetch prefers the tag's RSS feed (one request for 30 opening posts) over 30 paced detail
calls. A successful response resets only that origin's ladder.

## Configuration

Everything lives in `config.json` (merged over the defaults in `config.py`):
poll interval, page count, retry schedules and jitter, detail pacing/budget, filter
batch size, model id, prompt version, attention thresholds.

## Design notes

* **Slot-aligned columns.** A topic keeps the same row across all three columns; its
  `state` decides which column renders it and the other cells stay as empty slots, so
  "what got filtered out" is visible as aligned holes rather than an invisible list
  operation. A toggle switches the empty slots between *reserved* and *collapsed*.
* **Filtering is not a black box** — every pick carries the model's score, category,
  one-line reason and summary.
* **Read-only by design.** The collector never posts, replies, likes or logs in. The
  community forbids AI-generated content on the site; this tool only reads it.

## Legal / etiquette

Personal reading aid. It reads public topic listings and opening posts at a polite
rate (2 pages per cycle, ~15-minute interval, paced detail fetches) and links back to
the original threads instead of republishing their content.
