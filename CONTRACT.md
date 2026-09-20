# linuxdo-ai-feed — frozen interfaces (v1)

Project root: `/workspace/linuxdo-ai`
Data source: linux.do tag 人工智能 (`/tag/444-tag/444.json`) — read-only scrape.
Filter: CommandCode provider API, model `deepseek/deepseek-v4.1-flash`, key from env `commandcode_apikey`.

Backend binds `127.0.0.1:8791` and serves BOTH the static frontend (`public/`) and the JSON API.

---

## 1. GET /api/state

```json
{
  "generated_at": "2026-09-20T09:41:00Z",
  "source": {
    "tag": "人工智能",
    "tag_url": "https://linux.do/tag/444-tag/444",
    "fetched_at": "2026-09-20T09:40:12Z",
    "total_topics": 312
  },
  "filter": {
    "model": "deepseek/deepseek-v4.1-flash",
    "prompt_version": "v1",
    "last_run_at": "2026-09-20T09:40:40Z",
    "picked": 14,
    "rejected": 96,
    "pending": 0,
    "errors": 0
  },
  "topics": [
    {
      "id": 2927083,
      "title": "对 computer use 的一些看法",
      "url": "https://linux.do/t/topic/2927083",
      "author": "binaryMan",
      "created_at": "2026-09-20T09:25:12.000Z",
      "bumped_at": "2026-09-20T09:31:00.000Z",
      "reply_count": 0,
      "views": 30,
      "like_count": 3,
      "category": "Develop",
      "tags": ["人工智能", "纯水"],
      "excerpt": "plain text, <=300 chars, '' if unknown",
      "body_text": "plain text of the OP, may be '' when detail fetch failed",
      "detail_fetched": true,
      "state": "picked",
      "filter": {
        "score": 78,
        "category": "模型发布",
        "reason": "一句话中文理由（<=30 字）",
        "summary": "中文摘要（<=80 字）",
        "at": "2026-09-20T09:40:40Z"
      }
    }
  ]
}
```

`topics` is sorted newest-first (`created_at` desc, `id` desc as tiebreak).
`state` is the single source of truth for which column renders a topic:

* `pending`  — scraped, not filtered yet
* `picked`   — deepseek judged it valuable  (middle column)
* `rejected` — deepseek judged it low value (stays in the left column, dimmed)

`filter` is `null` when `state == "pending"`.

## 2. Other endpoints

| Method | Path | Response |
|---|---|---|
| POST | `/api/refresh` | `{"ok":true,"running":bool,"job":{"stage":"fetch\|filter\|idle","started_at":...,"last_result":...}}` — kicks a background cycle, returns immediately |
| GET | `/health` | see §3 |
| GET | `/metrics` | Prometheus text: `linuxdo_ai_fetch_consecutive_failures`, `linuxdo_ai_topics_total`, `linuxdo_ai_picked_total`, `linuxdo_ai_last_success_timestamp_seconds`, `linuxdo_ai_cycles_total{result="ok\|fail"}` |
| GET | `/api/failures?n=20` | `{"failures":[{"at":...,"stage":"fetch\|filter","attempt":3,"error":"...","context":{...}}]}` newest first — this is the RCA feed for the agent |
| GET | `/api/queue` / POST `/api/queue` | `{"queue":[topic_id,...]}` — server-side copy of the right column (client also keeps localStorage) |

## 3. GET /health

```json
{
  "status": "ok",
  "attention": {"needed": false, "reason": ""},
  "uptime_s": 1204,
  "last_success_at": "2026-09-20T09:40:12Z",
  "last_attempt_at": "2026-09-20T09:55:12Z",
  "consecutive_failures": 0,
  "next_retry_in_s": 0,
  "fetch": {"ok": true, "page": 1, "topics_seen": 30, "new": 4, "error": null},
  "filter": {"ok": true, "model": "deepseek/deepseek-v4.1-flash", "key_source": "env:commandcode_apikey", "key_present": true, "last_error": null, "batches_ok": 2, "batches_failed": 0},
  "last_error": null
}
```

* `status`: `ok` | `degraded` (last cycle partially failed: detail fetch / filter error, but list ok) | `failing` (repeated fetch failures).
* `attention.needed` becomes `true` when `consecutive_failures >= 3` (config `attention_threshold`) or the filter key is missing/invalid — i.e. a human/agent must do RCA. The watchdog cron reads exactly this field.
* `key_present` is a boolean only; the key value must never appear in any response, log, or file.

## 4. Fetch rules (backend)

* List: `GET /tag/444-tag/444.json?order=created&ascending=false&page=N` with browser-ish headers
  (`accept: application/json, text/javascript, */*; q=0.01`, `x-requested-with: XMLHttpRequest`,
  `sec-fetch-*`, Chrome UA, `accept-language: zh-CN,zh;q=0.9`). 30 topics/page; follow
  `topic_list.more_topics_url` (relative → resolve against `https://linux.do`).
* Detail (for `body_text`): `GET /t/topic/<id>.json`, paced >= 2 s apart, `post_stream.posts[0].cooked`
  → strip HTML → plain text (cap 4000 chars). On 403/429/CF challenge → fall back to
  `https://r.jina.ai/https://linux.do/t/topic/<id>.json` (strip the `Title:`/`Markdown Content:` wrapper
  around the JSON). Cache permanently per topic id.
* Backoff (one cycle): attempts 5, delays `5, 20, 60, 180, 420` s with ±20 % jitter; each failed attempt
  appends a row to `logs/failures.jsonl`. After the cycle gives up: `consecutive_failures += 1`,
  next cycle delay = `interval * 2^min(consecutive_failures,4)` (cap 30 min). Any success resets it.
* Never write to linux.do (the site forbids AI-generated content) — read-only.

## 5. Frontend requirements (v0 → then 10 design iterations)

Files: `public/index.html`, `public/styles.css`, `public/app.js` (no framework, no build step, no CDN —
must work offline/behind the tunnel). Data via `fetch('/api/state')`.

Layout (desktop >= 900px): one header bar + three columns All / Picked / Queue.

* Row-aligned slots: a topic occupies the SAME row index in every column. Use CSS grid rows so
  column 1/2/3 cells at row *i* line up horizontally. A topic is rendered in exactly one column
  (by its `state` + queue membership); the other two cells are empty slots.
* `gap` mode (default): empty slots keep their height with a thin dimmed hairline marker → the
  filtering result is visible as aligned holes. `compact` mode: empty slots collapse and surviving
  items pull up (per column, so rows re-flow but semantics stay per column).
* Toggle button switches gap ⇄ compact; both modes animate (FLIP on `transform`/`opacity` only, ~300 ms,
  `prefers-reduced-motion: reduce` → no movement).
* Refresh: newly picked topics (not seen as picked in the previous state) animate once from their
  column-1 slot into the middle column (fade + slide, once, no replay for already-picked ones).
* Click a middle-column item → it moves to the right column (Queue, persisted; `localStorage` + POST /api/queue).
* Preview: desktop hover ≥250 ms (and keyboard focus) or mobile tap → detail drawer/panel with title,
  meta line (author · created · replies · views · category · tags), `summary`/`reason` when present,
  `body_text` (or `excerpt`), and a link to the original topic (`target="_blank" rel="noopener"`).
* Mobile (< 900px): three tabs (全部 / 已筛 / 待读) + single column list; same drawer as bottom sheet;
  touch targets >= 44 px.
* Header: site name, `fetched_at` (relative, e.g. `12 分钟前`), counts `全部 N · 通过 N · 待读 N`,
  model label (`deepseek-v4.1-flash`), refresh button, gap/compact toggle. Show a small warning chip
  when GET /health returns `attention.needed = true`.
* Style: Anthropic-research *editorial* feel — serif display headings, sans body, generous whitespace,
  1 px low-contrast rules instead of card shadows, one accent color, small caps labels, footnote-style
  meta (`筛选模型 · … · 时间`). Light + dark via `prefers-color-scheme`. Body 15–16 px / 1.6,
  column gap 24–32 px, max-width 1280 px, accent used sparingly. No emoji in chrome; no tables.
* Accessibility: real buttons, visible focus ring, `aria-live` on the count line, drawer closeable with
  `Esc`, list items keyboard-activatable.

## 6. Fixtures for frontend work

`fixtures/state.sample.json` — a realistic 60-topic payload (some pending, ~25 % picked, varied
categories/scores, one topic with empty `body_text`) so the UI can be developed and screenshotted
without the backend running. `file://` + a `?fixture=1` switch may load it via `fetch('fixtures/state.sample.json')`
in dev only.
