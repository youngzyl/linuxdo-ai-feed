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
      "has_body": true,
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

**Compatibility, and the two shapes of this endpoint.** The **default** read (`GET /api/state`)
is unchanged: every row still carries `body_text`, so a page built before the body split keeps
rendering correctly against a newer server (an already-open tab survives a rollout). The current
reader asks for `GET /api/state?view=list`, the **list shape**: each row carries `has_body`
instead of `body_text` and the payload is roughly 7x smaller on the wire (§1.1, §5). The two
shapes differ in exactly one key per row, both are gzipped identically, and the body a reader
actually opens comes one at a time from `/api/topic/<id>`.

* `pending`  — scraped, not filtered yet
* `picked`   — deepseek judged it valuable  (middle column, 精选)
* `rejected` — deepseek judged it low value (stays in the left column, dimmed)

`filter` is `null` when `state == "pending"`.

Column membership (v2, settled) — a topic can hold **two** memberships at once:

* column 1 全部    — every topic that is not `picked` (`pending` + `rejected`)
* column 2 精选    — `picked`
* column 3 收藏    — the bookmark list (see §7); membership is independent of `state`

A bookmarked `rejected` topic renders in 收藏 and in 全部; a bookmarked `picked` topic renders
in both 精选 and 收藏. Slot alignment is unchanged: it is the slot, not the topic, that stays
unique per column, so row *i* still lines up across all three columns.

## 1.1 GET /api/topic/<id>

```json
{
  "id": 2927083,
  "title": "对 computer use 的一些看法",
  "url": "https://linux.do/t/topic/2927083",
  "body_text": "plain text of the OP, may be '' when the detail fetch failed",
  "excerpt": "plain text, <=300 chars, '' if unknown",
  "has_body": true,
  "state": "picked",
  "filter": {"score": 78, "category": "模型发布", "reason": "...", "summary": "..."},
  "created_at": "2026-09-20T09:25:12.000Z",
  "bumped_at": "2026-09-20T09:31:00.000Z",
  "reply_count": 0,
  "views": 30,
  "category": "Develop",
  "tags": ["人工智能", "纯水"]
}
```

* The only route that returns an OP body. The reader calls it when an **explicit** drawer open
  starts (never for a hover/focus preview), caches the answer in memory (bounded, oldest
  evicted first) and keeps the excerpt - or an honest loading line - on screen until the body
  arrives.
* `has_body` is `true` exactly when `body_text` is non-empty (after trimming), i.e. when a
  `/api/topic/<id>` call can return a body; it is the same flag each `/api/state` row carries.
  A list row renders from `has_body` + `excerpt` and never assumes an absent `body_text` means
  "no body".
* `has_body` is **not displayed content**: it only schedules that read. See §7 for what marks a
  topic read.
* **Path id contract.** The id must match ASCII `[0-9]{1,20}` exactly, decided on the raw path
  segment **before** any `int()` conversion or store lookup. Leading zeros are legal and name
  the same topic as the canonical id (`/api/topic/00011` == `/api/topic/11`). An unknown id,
  `0`, a negative or signed id, a spaced / dotted / hex-ish id, a slash, an empty segment, a
  non-ASCII digit (`²`, Arabic-Indic `١١`, fullwidth `１１` - all of which `str.isdigit()` or
  `int()` would otherwise convert) and a path longer than 20 digits are all
  `404 {"error": "topic not found"}`. A 4000+ digit path is a 404, never an `int()` failure and
  never a lookup. The browser-suite stub shares this parser, so the double accepts exactly what
  the server accepts.
* Read-only: no owner token, and it is not in the owner-gate table. CORS follows `/api/state`
  (`Vary: Origin`, exact allow-listed origin only, no credentials). Any method other than
  GET/HEAD/OPTIONS is a 405.
## 2. Other endpoints

| Method | Path | Response |
|---|---|---|
| POST | `/api/refresh` | `{"ok":true,"running":bool,"job":{"stage":"fetch\|filter\|idle","started_at":...,"last_result":...}}` — kicks a background cycle, returns immediately |
| GET | `/api/topic/<id>` | one topic incl. `body_text` (see §1.1) - 404 for an unknown or non-numeric id |
| GET | `/health` | see §3 |
| GET | `/metrics` | Prometheus text: `linuxdo_ai_fetch_consecutive_failures`, `linuxdo_ai_topics_total`, `linuxdo_ai_picked_total`, `linuxdo_ai_last_success_timestamp_seconds`, `linuxdo_ai_cycles_total{result="ok\|fail"}` |
| GET | `/api/failures?n=20` | `{"failures":[{"at":...,"stage":"fetch\|filter","attempt":3,"error":"...","context":{...}}]}` newest first — this is the RCA feed for the agent |
| GET | `/api/queue` / POST `/api/queue` | `{"queue":[topic_id,...]}` — 收藏 (bookmarks), persisted server-side; the wire name `queue` is kept for compatibility and the client also caches the list in `localStorage`. Body: `{"add": id}` / `{"remove": id}`. Independent of `/api/feedback` (see §7) |

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

Layout (desktop >= 900px): one header bar + three columns 全部 / 精选 / 收藏.

* Row-aligned slots: a topic occupies the SAME row index in every column. Use CSS grid rows so
  column 1/2/3 cells at row *i* line up horizontally. Its own membership decides column 1 and
  column 2 (see §1); column 3 is the bookmark list, so one topic may fill two slots. Empty slots
  keep the old meaning: a cell is blank when that list has no item for that row.
* `gap` mode (default): empty slots keep their height with a thin dimmed hairline marker → the
  filtering result is visible as aligned holes. `compact` mode: empty slots collapse and surviving
  items pull up (per column, so rows re-flow but semantics stay per column).
* Toggle button switches gap ⇄ compact; both modes animate (FLIP on `transform`/`opacity` only, ~300 ms,
  `prefers-reduced-motion: reduce` → no movement).
* Refresh: newly picked topics (not seen as picked in the previous state) animate once from their
  column-1 slot into the middle column (fade + slide, once, no replay for already-picked ones).
* **Row activation (settled).** Every row in every column - 全部, 精选 and 收藏 - opens the
  preview on that topic and **pins** it: a click/tap, or Enter/Space on the focused row. A row
  activation never writes: 收藏 / 取消收藏 is the drawer button's job alone (`POST /api/queue`),
  in 精选 exactly as everywhere else, and a keep/skip vote stays the drawer's 纳入精选 / 排除.
  Copy is settled: 收藏 / 取消收藏, 纳入精选, 排除, 原文, 备注（可选） / placeholder 补充筛选偏好.
  No conversational copy (教筛选器 / 漏掉了 / 下一轮会读到 / 后台可能正在重启 are all banned).
* Preview: desktop hover ≥250 ms (and keyboard focus) opens a **transient** preview - it closes
  when the pointer leaves, while nothing is pinned. An explicit activation (click, tap,
  Enter/Space, or the `?open=` dev deep link) opens the **pinned** preview instead: it stays
  until the reader clicks elsewhere (any click outside the rows and the panel - blank space, the
  header, and its controls 刷新 / 紧凑 / 管理 / tabs, which dismiss it and still perform their own
  action), clicks 关闭, taps the mobile scrim, or presses `Esc`. While a preview is pinned, a passive hover or
  focus on the same or on another row can neither replace it nor unpin it, a pointer that
  leaves does not close it, and re-entering a row does not drop the pin; an explicit activation
  on another row replaces the pinned topic. The panel shows title, meta line
  (author · created · replies · views · category · tags), `summary`/`reason` when present, the
  OP body (fetched on demand from `/api/topic/<id>` when the drawer opens; `excerpt` meanwhile,
  or 正文未抓取 when there is none) and a link to the original topic
  (`target="_blank" rel="noopener"`). Closing restores the focus to the row the preview came
  from, and that restored focus is never re-read as a fresh keyboard preview.
* Mobile (< 900px): three tabs (全部 / 精选 / 收藏) + single column list; same drawer as bottom sheet;
  touch targets >= 44 px. A topic is listed once per tab: the 收藏 membership is folded into 全部/精选.
* Header: site name with the subtitle 浏览新帖，发现值得读的内容。 inside the brand block, `fetched_at`
  (relative, e.g. `12 分钟前`), counts `全部 N · 精选 N · 收藏 N`, model label, refresh button,
  gap/compact toggle, and a management button that ALWAYS reads 管理 (fixed geometry; the auth state
  is a small indicator/dot plus the title — never a changed label). Warning chip on
  `attention.needed = true`.
* Style: Anthropic-research *editorial* feel — serif display headings, sans body, generous whitespace,
  1 px low-contrast rules instead of card shadows, one accent color, small caps labels, footnote-style
  meta (`筛选模型 · … · 时间`). Light + dark via `prefers-color-scheme`. Body 15–16 px / 1.6,
  column gap 24–32 px, max-width 1280 px, accent used sparingly. No emoji in chrome; no tables.
* Accessibility: real buttons, visible focus ring, `aria-live` on the count line, drawer closeable with
  `Esc`, list items keyboard-activatable (Enter/Space opens and pins the preview).

The browser regression for this contract is `tests/browser_preview_pin.py` (real
`Input.dispatchMouseEvent` / `dispatchKeyEvent` / `dispatchTouchEvent` events against the stub
API): hover transience, click pinning, the pending hover timer after a click, re-entering a
pinned row, a passive hover over another row, explicit replacement, all three columns, outside
clicks on blank space and the header, inside interactions, `Esc` and 关闭 without a focus
re-open, keyboard activation and touch taps, and that no activation path writes a bookmark.

## 6. Fixtures for frontend work

`fixtures/state.sample.json` — a realistic 60-topic payload (some pending, ~25 % picked, varied
categories/scores, one topic with empty `body_text`) so the UI can be developed and screenshotted
without the backend running. `file://` + a `?fixture=1` switch may load it via `fetch('fixtures/state.sample.json')`
in dev only.

## 7. 收藏 (bookmarks) and 已读 (browser-local read state)

Two separate signals, deliberately not coupled:

**收藏 — the third column.** Persisted with the existing `queue` storage and `/api/queue` wire
name (no migration, no new shape), but the semantics are bookmarks:

* Adding or removing a bookmark NEVER records a keep/skip preference, and a keep/skip vote NEVER
  adds or removes a bookmark (`learn._apply_explicit_effects` now only writes the manual override).
* Bookmarking a non-picked topic (a rejected one, say) makes it show under 收藏 while it stays in
  its own column: membership is independent of the model verdict, so one topic can hold two slots.
* Every pre-existing queue entry stays in the list as a bookmark — which of them were meant as
  "read later" and which as "keep" cannot be inferred, so nothing gets migrated away.

**已读 — reading position.** Browser-local `localStorage` only
(`linuxdo-ai.read:<apiBase or page origin>`), never a server write, so it survives a reload but is
per browser (another device does not see it) and is not owner-authenticated:

* An explicit preview open (click / tap / keyboard, or the `?open=` dev deep link) marks the
  topic read as soon as the preview **shows** content: a non-empty `excerpt`, or a body already
  in the in-memory body cache. `has_body` alone is **not** content - it only schedules the lazy
  read - so a topic with a body and no excerpt stays unread while that body loads and stays
  unread if the read fails; it becomes read once a non-empty body is actually displayed, during
  that same explicit visit. A hover/focus preview is a prefetch of the row - it neither marks
  read nor fetches a body, whether or not another preview is pinned, and it never turns a pinned
  preview into a transient one. A preview with no body waits for the original link, and clicking
  `原文` marks read even when no body was fetched (a manual action).
* Re-opening the topic the drawer already shows does not mark again, so a manual 未读 toggle sticks
  while the drawer stays open (including the 30 s time refresh); moving to another topic marks
  again. Closing the drawer or switching topic cancels a pending automatic mark, a manual
  已读/未读 toggle cancels it for the rest of that visit, and an answer that arrives for a topic
  the drawer has left (or for a snapshot that has since been re-read) marks nothing and replaces
  nothing.
* Display: read rows drop one step in title weight and colour; unread rows keep the stronger title
  plus one small dot. No per-row 已读 text or badge — the only 已读 strings in the UI are the icon
  control's `aria-label`/`title` (标记为已读 / 标记为未读), which is the reversible, icon-only control.
* Bounded (the most recent 1000 ids) and tolerant of blocked or malformed storage (falls back to
  unread instead of throwing). A `storage` event from another tab of the same deployment is mirrored.

**Transport.** Every text response (`/api/state` in both shapes, `/health`, `/api/queue`,
`/api/failures`, `/api/topic/<id>`, `/metrics` and the static text assets) is gzipped when the
request's `Accept-Encoding` permits gzip and the body is at least 1024 bytes; smaller bodies
stay identity-encoded. `Content-Length` always describes the bytes on the wire, `Content-Encoding`
is set only then (a response that already carries one is never compressed twice), and `Vary`
keeps `Origin` and adds `Accept-Encoding`. The same payload that was 2.72 MB raw goes out at
~345 KB. Negotiation policy for the ambiguous inputs: `gzip` and `x-gzip` are the same codec,
matched case-insensitively; no header, an empty header or only other codecs mean identity; a
positive wildcard (`*`) permits gzip when no explicit gzip token is present; duplicate gzip
tokens resolve to the **conservative minimum** quality, so a `q=0` refusal cannot be bypassed by
reordering or a later `gzip;q=1`; `q=0` beats a positive wildcard while an explicit positive
token beats `*;q=0`; and an unparsable or out-of-range `q` (`abc`, `nan`, `inf`, `2`, `-1`)
refuses that token.

**Network UX.** One bounded deadline (`AbortController`, **default 30 s**, overridable in dev
with `?readtimeout=`) covers the `/api/state` read **and** the transfer of its body: an answer
whose headers arrive quickly and whose body then stalls is reported as a deadline, not as a
malformed payload, and the copy says the read did not finish in time
(`读取超时（30 秒内未能读完响应）`) rather than claiming the server never answered. The same bound
covers the lazy `/api/topic/<id>` read, whose timer and pending marker are released in every
outcome so a later visit really retries.

The default is 30 s because of a measurement, not a guess. The incident HAR carried
`content-length: 2718951` and captured **275712 body bytes over 11.791 s** of receive time,
i.e. about **23383 B/s** on that connection; the compressed list shape (~347 KB) therefore needs
roughly **14.8 s** to arrive there, which the previous 12 s bound cut off mid-transfer. 30 s is the same
single deadline for the list and for the detail read, and the `?readtimeout=` dev knob keeps
deterministic short deadlines for tests. This is a companion to the payload reduction, not a
substitute for it: the deadline only decides when a read that is genuinely stuck is abandoned.
The HAR also shows `/health` (7672 ms TTFB) and `/api/queue` (7066 ms) delays that **begin
before** the `/api/state` request, so nothing here attributes those to this payload.
Overlapping refresh / retry / 30 s-tick reads are
deduplicated into a single in-flight read. A failed read reports what actually happened — HTTP
status, deadline, or connection error — and an existing successful snapshot stays rendered with a
stale notice instead of being blanked (no backend-restart guessing). No retry storms, no write
retries, and no persistent copy of secrets or full state.

**Collapsed technical facts.** A failed read also fills a collapsed 技术详情 block inside the error
panel, with four safe lines and nothing more: the category (`timeout` / `http <status>` / `network`
/ `payload`), the fixed trusted endpoint path (`/api/state`), the client-side failure time
(ISO 8601, `Z`) and the last successful read time (`—` when there has not been one). It never
contains a full URL or query string, headers, tokens or response bodies, and it is never sent
anywhere — there is no telemetry endpoint. The user's browser clock is the only clock involved.

## U07 — density toggle keeps the reading position

`紧凑 / 间隙` (the masthead density control) reflows the same board between the packed stacks and
the chronological grid, and it must keep the reader where they were:

- The reading position is an in-memory `{id, column, viewport top}` triple, never a DOM reference
  (a bookmark redraw or a density re-render replaces the DOM and must not detach it). It is
  recorded on an explicit open (click / Enter / Space) from the originating column, or by the
  passive debounced viewport sample of the last genuinely browsed column.
- While the masthead is on screen the sample freezes: returning to the header for the density
  control does not overwrite the remembered article with the first article at the page top.
- The toggle resolves that triple BEFORE re-rendering, re-renders without FLIP item motion and
  compensates synchronously; a settled deliberate scroll deeper in the list may advance it first.
- It is never persisted, never restored across a refresh, never a request destination, and never
  mutates the read set, bookmarks, votes or preference storage.
- Desktop only: mobile list geometry does not change with density, so nothing is compensated and
  no jump is added.

Legacy-capability fallback (click-only): the passive preview resumes on a genuine mouse movement only
in engines that type their pointer input — `PointerEvent`, a `pointermove` whose `pointerType` is
`"mouse"`. Where that typing is unavailable there is no reliable way to tell a mouse from a finger, so
the guard a density reflow leaves stands, and the preview resumes on the next explicit activation
(click / tap / Enter / Space) rather than on movement. No touch heuristics, cooldowns or storage are
involved, and explicit activation behaves identically to the modern path.
