/* linux.do AI feed — frontend v0 (vanilla, no build step)
 *
 * Data: GET /api/state  (see CONTRACT.md §1)
 * API origin: public/runtime-config.js (`apiBase`), a trusted build-time value. Empty
 * means same origin (the backend serves this file itself); the GitHub Pages build injects
 * the deployed API origin. Never read from the query string.
 * Owner writes (queue/vote/refresh) need an owner token, kept in sessionStorage only and
 * sent as `Authorization: Bearer`. Without it the page is read-only: browsing, the drawer
 * and the original links keep working, the write controls are disabled with a reason.
 * Dev switch: ?fixture=1 loads fixtures/state.sample.json (served copy lives in
 * public/fixtures/). Dev-only extras: &phase=next loads the "next" fixture,
 * &health=attention fakes the /health attention chip. Fixture mode never talks to the API.
 */
'use strict';

(function () {
  const CFG = {
    state: '/api/state',
    refresh: '/api/refresh',
    health: '/health',
    queue: '/api/queue',
    feedback: '/api/feedback',
    fixtureBase: 'fixtures/state.sample.json',
    fixtureNext: 'fixtures/state.sample.next.json',
    lsQueue: 'linuxdo-ai.queue',
    lsSeen: 'linuxdo-ai.seenPicked',
    lsDensity: 'linuxdo-ai.density',
    lsRead: 'linuxdo-ai.read',
    ssOwner: 'linuxdo-ai.ownerToken',
    readCap: 1000,        /* bounded browser-local read set (oldest ids are dropped) */
    readDeadlineMs: 12000,/* one bounded deadline per /api/state read */
    hoverMs: 250,
    closeMs: 180,
    animMs: 300,     /* FLIP duration (brief: 280-340ms) */
    staggerMs: 14,   /* per-batch delay: a whole-board FLIP must not start at once */
    maxGroups: 12,   /* ⇒ at most 12 batches, ≤154ms extra, never a JS-driven frame loop */
    fadeMs: 160      /* prefers-reduced-motion: opacity only, no movement */
  };

  /* ---------------------------------------------------------------- api origin */
  /* `apiBase` is trusted build-time configuration (public/runtime-config.js). It is never
     read from the URL, localStorage or the DOM, so a crafted link cannot redirect the page
     or its owner token to another host. Only an absolute https:// base (or a localhost
     http:// base for dev) without query/fragment is accepted; anything else falls back to
     same-origin. */
  function apiBaseFrom(value) {
    const raw = (typeof value === 'string') ? value.trim() : '';
    if (!raw) return '';
    let u;
    try { u = new URL(raw); } catch (err) { return ''; }
    const localDev = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname);
    if (u.protocol !== 'https:' && !(u.protocol === 'http:' && localDev)) return '';
    if (u.search || u.hash) return '';
    return raw.replace(/\/+$/, '');
  }
  const RUNTIME = (typeof window !== 'undefined' && window.LINUXDO_AI_RUNTIME) || {};
  const API_BASE = apiBaseFrom(RUNTIME.apiBase);
  const api = (path) => API_BASE + path;

  const params = new URLSearchParams(location.search);
  /* dev-only knob for the browser harness: a shorter read deadline. Clamped, never
     read from the DOM/localStorage, and it only shortens the client's own timeout. */
  const READ_DEADLINE = (function () {
    const asked = Number(params.get('readtimeout'));
    if (!Number.isFinite(asked) || asked <= 0) return CFG.readDeadlineMs;
    return Math.min(Math.max(asked, 400), 60000);
  })();

  /* the browser test reads these; no token is ever exposed */
  window.LINUXDO_AI_DEBUG = { apiBase: API_BASE, sameOrigin: API_BASE === '', readDeadlineMs: READ_DEADLINE };

  const DEV = params.get('fixture') === '1';
  const DEV_HEALTH = params.get('health');
  const DEV_SLOW = Number(params.get('slow')) || 0; /* dev: hold the fetch to show skeletons */
  const DEV_FAIL = params.get('fail') === '1';      /* dev: force the /api/state error state */
  const DEV_AUTO = Number(params.get('auto')) || 0; /* dev: auto-press 刷新 N times (animation demo) */
  const OPEN_ID = Number(params.get('open')) || 0;  /* dev: open the preview for this topic id */

  const $ = (sel, root) => (root || document).querySelector(sel);

  const el = {
    board: $('#board'),
    countline: $('#countline'),
    cAll: $('#c-all'),
    cPicked: $('#c-picked'),
    cTotal: $('#c-total'),
    cQueue: $('#c-queue'),
    fetched: $('#fetched'),
    model: $('#model'),
    devbadge: $('#devbadge'),
    warnchip: $('#warnchip'),
    empties: $('#empties'),
    emptyC1: $('#empty-c1'),
    emptyC2: $('#empty-c2'),
    emptyC3: $('#empty-c3'),
    errorstate: $('#errorstate'),
    errordetail: $('#errordetail'),
    errortech: $('#errortech'),
    errorfacts: $('#errorfacts'),
    retry: $('#retry'),
    refresh: $('#refresh'),
    density: $('#density'),
    tabs: $('#tabs'),
    drawer: $('#drawer'),
    scrim: $('#scrim'),
    dState: $('#d-state'),
    dTitle: $('#d-title'),
    dMeta: $('#d-meta'),
    dFilter: $('#d-filter'),
    dScore: $('#d-score'),
    dCat: $('#d-cat'),
    dReason: $('#d-reason'),
    dSummary: $('#d-summary'),
    dBody: $('#d-body'),
    dQueue: $('#d-queue'),
    dKeep: $('#d-keep'),
    dSkip: $('#d-skip'),
    dRead: $('#d-read'),
    dNote: $('#d-note'),
    dTaste: $('#d-taste'),
    dLink: $('#d-link'),
    dClose: $('#d-close'),
    owner: $('#owner'),
    authchip: $('#authchip'),
    opstatus: $('#opstatus')
  };

  const S = {
    data: null,
    topics: new Map(),
    seen: new Set(),
    queue: new Set(),
    votes: new Map(), /* id -> 'keep' | 'skip' */
    read: new Set(),  /* ids read in this browser (localStorage, namespaced by apiBase) */
    density: 'gap',
    tab: 'all',
    occupied: new Set(), /* 'row:col' slots filled by the previous render */
    flipLog: [],         /* dev-only: batch summary of each FLIP run */
    openId: null,
    lastOkAt: null,   /* client ISO time of the last successful state read */
    pinned: false,
    first: true,
    lastTrigger: null
  };

  let hoverTimer = 0;
  let closeTimer = 0;
  let hideTimer = 0;
  let tickTimer = 0;
  let lastInput = null; /* 'mouse' | 'touch' | 'keyboard' — decides click semantics */

  const mDesktop = window.matchMedia('(min-width: 900px)');
  const mHover = window.matchMedia('(hover: hover)');
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const isDesktop = () => mDesktop.matches;
  /* Never decide from matchMedia('(hover)') alone: headless and exotic
     pointers report hover:none on a real mouse. Follow the gesture instead. */
  const isTouchGesture = () => lastInput === 'touch' || (lastInput === null && !mHover.matches);

  /* ------------------------------------------------------------------ utils */

  function relTime(iso) {
    const ms = Date.parse(iso);
    if (!ms) return '时间未知';
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return '刚刚';
    const m = Math.round(s / 60);
    if (m < 60) return m + ' 分钟前';
    const h = Math.floor(m / 60);
    if (h < 24) return h + ' 小时前';
    const d = Math.floor(h / 24);
    if (d < 30) return d + ' 天前';
    return new Date(ms).toISOString().slice(0, 10);
  }

  function absTime(iso) {
    const ms = Date.parse(iso);
    if (!ms) return '';
    const dt = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    return dt.getFullYear() + '-' + p(dt.getMonth() + 1) + '-' + p(dt.getDate()) + ' ' +
      p(dt.getHours()) + ':' + p(dt.getMinutes());
  }

  function shortModel(m) {
    if (!m) return '未知';
    return String(m).split('/').pop();
  }

  function readIds(key, cap) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return new Set();
      const arr = JSON.parse(raw);
      let nums = (Array.isArray(arr) ? arr : [])
        .map((n) => Number(n))
        .filter((n) => Number.isFinite(n));
      if (cap && nums.length > cap) {
        nums = nums.slice(nums.length - cap);  /* keep the most recent entries */
        writeIds(key, new Set(nums), cap);
      }
      const seen = [];
      nums.forEach((n) => { if (seen.indexOf(n) < 0) seen.push(n); });
      return new Set(seen);
    } catch (err) {
      /* malformed JSON or storage blocked — start from empty, never throw */
      return new Set();
    }
  }

  function writeIds(key, set, cap) {
    try {
      let nums = Array.from(set).map((n) => Number(n)).filter((n) => Number.isFinite(n));
      if (cap && nums.length > cap) nums = nums.slice(nums.length - cap);
      localStorage.setItem(key, JSON.stringify(nums));
    } catch (err) {
      /* storage disabled — in-memory only */
    }
  }

  /* --------------------------------------------------------------- read state */
  /* Read state is browser-local on purpose: it is not owner-authenticated, needs no
     server write, survives a reload, and two deployments must not share a reading
     position - hence the namespace, derived from the trusted apiBase (or the page
     origin in a same-origin build). It is the one deliberately small-scope choice in
     this contract: another device does not see it. */
  function readKey() {
    return CFG.lsRead + ':' + (API_BASE || location.origin);
  }

  function loadRead() {
    return readIds(readKey(), CFG.readCap);
  }

  function markRead(id, value) {
    const n = Number(id);
    if (!Number.isFinite(n)) return;
    const want = (value === undefined) ? !S.read.has(n) : !!value;
    if (want === S.read.has(n)) { syncReadNodes(n); return; }
    if (want) S.read.add(n);
    else S.read.delete(n);
    writeIds(readKey(), S.read, CFG.readCap);
    syncReadNodes(n);
    /* keep the drawer's icon control in step; this never re-marks the topic */
    if (S.openId !== null && Number(S.openId) === n) syncReadControl(S.topics.get(n));
  }

  /* the icon-only control: pressed state + accessible name only (no visible text) */
  function syncReadControl(t) {
    if (!el.dRead || !t) return;
    const read = S.read.has(t.id);
    el.dRead.setAttribute('aria-pressed', read ? 'true' : 'false');
    el.dRead.setAttribute('aria-label', read ? '标记为未读' : '标记为已读');
    el.dRead.title = read ? '标记为未读' : '标记为已读';
  }

  /* every instance of a topic (a topic can hold a 精选 and a 收藏 slot at once) */
  function syncReadNodes(only) {
    el.board.querySelectorAll('.item').forEach((node) => {
      const id = Number(node.dataset.id);
      if (only !== undefined && only !== null && id !== Number(only)) return;
      const read = S.read.has(id);
      node.classList.toggle('is-read', read);
      node.setAttribute('aria-label', itemLabel(S.topics.get(id), read));
    });
  }

  const sleep = (ms) => new Promise((r) => window.setTimeout(r, ms));

  /* the error panel is filled by showError() next to the read-error helpers */
  function clearError() {
    el.errorstate.hidden = true;
    el.errordetail.textContent = '';
    if (el.errorfacts) el.errorfacts.textContent = '';
    if (el.errortech) {
      el.errortech.hidden = true;
      el.errortech.open = false;
    }
  }

  /* per-column empty explanations; on mobile only the visible list gets one.
     Membership: 1 全部 = every topic that is not 精选, 2 精选 = picked, 3 收藏 = bookmarked. */
  function updateEmpties() {
    const topics = S.data ? S.data.topics : [];
    const picked = topics.filter((t) => t.state === 'picked').length;
    let bookmarked = 0;
    S.queue.forEach((id) => { if (S.topics.has(id)) bookmarked += 1; });
    const show = { c1: false, c2: false, c3: false };

    if (!S.data || el.board.hidden) {
      el.empties.hidden = true;
      return;
    }
    if (isDesktop()) {
      show.c1 = topics.length - picked === 0;
      show.c2 = picked === 0;
      show.c3 = bookmarked === 0;
    } else if (S.tab === 'all') {
      show.c1 = topics.length === 0;
    } else if (S.tab === 'picked') {
      show.c2 = picked === 0;
    } else {
      show.c3 = bookmarked === 0;
    }

    el.emptyC1.textContent = topics.length ? '所有帖子都已进入精选。' : '还没有抓到帖子。';
    el.emptyC1.hidden = !show.c1;
    el.emptyC2.hidden = !show.c2;
    el.emptyC3.hidden = !show.c3;
    el.empties.hidden = !(show.c1 || show.c2 || show.c3);
  }

  /* -------------------------------------------------------------- data load */

  function stateUrl(opts) {
    const bust = (opts && opts.bust === false) ? '' : ((opts && opts.bust) || '?t=' + Date.now());
    if (!DEV) return api(CFG.state) + (bust || '?t=' + Date.now());
    const file = (opts && opts.next) ? CFG.fixtureNext : CFG.fixtureBase;
    return file + (bust || '?t=' + Date.now());
  }

  /* One bounded read per pull: a single deadline so a hung connection cannot leave the
     board loading forever, and a classified error so the notice tells the truth about
     what happened (HTTP status vs deadline vs connection) instead of guessing why. */
  async function fetchState(opts) {
    if (DEV_FAIL) throw new Error('dev：?fail=1 模拟 /api/state 失败');
    if (DEV_SLOW) await sleep(DEV_SLOW);
    const controller = (typeof AbortController === 'function') ? new AbortController() : null;
    const timer = window.setTimeout(() => { if (controller) controller.abort(); }, READ_DEADLINE);
    try {
      const res = await fetch(stateUrl(opts), {
        cache: 'no-store',
        signal: controller ? controller.signal : undefined
      });
      if (!res.ok) {
        const err = new Error('HTTP ' + res.status);
        err.kind = 'http';
        err.status = res.status;
        throw err;
      }
      try {
        return await res.json();
      } catch (err) {
        const bad = new Error('payload');
        bad.kind = 'payload';
        throw bad;
      }
    } catch (err) {
      if (err && err.kind) throw err;
      if (controller && controller.signal.aborted) {
        const t = new Error('timeout');
        t.kind = 'timeout';
        throw t;
      }
      const net = new Error((err && err.message) ? err.message : '网络错误');
      net.kind = 'network';
      throw net;
    } finally {
      window.clearTimeout(timer);
    }
  }

  /* the four categories a reader can act on: deadline, server status, connection, payload */
  function readErrorCategory(err) {
    const kind = (err && err.kind) || 'network';
    if (kind === 'timeout') return 'timeout';
    if (kind === 'http') return 'http ' + ((err.status != null) ? err.status : '?');
    if (kind === 'payload') return 'payload';
    return 'network';
  }

  function describeReadError(err) {
    const kind = err && err.kind;
    if (kind === 'timeout') return '读取超时（服务器 ' + Math.round(READ_DEADLINE / 1000) + ' 秒内没有响应）';
    if (kind === 'http') return '服务器返回 HTTP ' + err.status;
    if (kind === 'payload') return '服务器返回的内容不是 JSON';
    return '连不上服务器（' + ((err && err.message) ? err.message : '网络错误') + '）';
  }

  function nowIso() {
    try {
      return new Date().toISOString();
    } catch (err) {
      return '';
    }
  }

  /* Collapsed technical facts, built only from safe values: the category, the fixed
     trusted endpoint path (CFG.state - never a URL with a query or credentials), the
     client-side failure time and the last successful read time. No headers, no tokens,
     no private bodies, and nothing is sent anywhere (no telemetry endpoint). */
  function renderErrorFacts(err) {
    if (!el.errorfacts || !el.errortech) return;
    el.errorfacts.textContent = [
      '分类 ' + readErrorCategory(err),
      '端点 ' + CFG.state,
      '客户端时间 ' + (nowIso() || '—'),
      '上次成功读取 ' + (S.lastOkAt || '—')
    ].join('\n');
    el.errortech.hidden = false;
    el.errortech.open = false;     /* collapsed: no extra visible clutter */
  }

  function showError(detail, err) {
    el.errordetail.textContent = detail;
    renderErrorFacts(err);
    el.errorstate.hidden = false;
    if (!S.data) { /* nothing rendered yet — the error replaces the board */
      el.board.hidden = true;
      el.board.removeAttribute('aria-busy');
      el.empties.hidden = true;
    }
  }

  function validate(data) {
    if (!data || !Array.isArray(data.topics)) throw new Error('payload 缺少 topics');
    return data;
  }

  /* Column membership (settled contract): a topic's own membership - 1 全部 for anything
     not 精选, 2 精选 for picked - never disappears because it is bookmarked, and 3 收藏
     is independent of the valuable flag (a rejected topic can sit there). One row can
     therefore hold two filled cells for the same topic; that is intended, not a bug.
     The intentional same-slot blank mode (the empty-slot marker) and the compact toggle
     keep their old meaning: a cell is blank only when this list has no item for that row. */
  function cellHas(t, col) {
    if (col === 3) return S.queue.has(t.id);
    if (col === 2) return t.state === 'picked';
    return t.state !== 'picked';
  }

  function nodeCol(node) {
    const cell = node && node.closest ? node.closest('.cell') : null;
    if (!cell) return 0;
    const m = /cell--c(\d)/.exec(cell.className);
    return m ? Number(m[1]) : 0;
  }

  function pickedIds(data) {
    return data.topics.filter((t) => t.state === 'picked').map((t) => t.id);
  }

  /* --------------------------------------------------------------- rendering */

  function metaText(t) {
    const bits = [
      t.author || '匿名',
      relTime(t.created_at),
      '回复 ' + (t.reply_count || 0),
      '浏览 ' + (t.views || 0),
      t.category || '未分类'
    ];
    if (Array.isArray(t.tags) && t.tags.length) bits.push(t.tags.join('/'));
    return bits.join(' · ');
  }

  function itemLabel(t, read) {
    const topic = t || {};
    const score = (topic.filter && typeof topic.filter.score === 'number') ? ('，模型评分 ' + topic.filter.score) : '';
    const base = topic.title
      ? '打开预览：' + topic.title + '。' + metaText(topic) + score
      : '打开预览';
    return base + (read ? '。已读' : '');
  }

  /* tags: at most two rendered, the rest folded into +N with the full list in
     the element's title so the row always stays a single meta line */
  function tagSummary(t) {
    const tags = Array.isArray(t.tags) ? t.tags.filter(Boolean) : [];
    const shown = tags.slice(0, 2).join('/');
    const extra = Math.max(0, tags.length - 2);
    return { all: tags, extra: extra, text: extra > 0 ? shown + ' +' + extra : shown };
  }

  function buildScoreChip(t) {
    const chip = document.createElement('span');
    chip.className = 'chip-score';
    chip.textContent = '评分 ' + t.filter.score;
    chip.title = '模型评分 · ' + (t.filter.category || '未分类');
    return chip;
  }

  function buildLine(cls, text) {
    const p = document.createElement('p');
    p.className = cls;
    p.textContent = text;
    return p;
  }

  /* the OP body only earns a row line when it is short; long bodies stay in the
     drawer so the picked row keeps its scannable hierarchy */
  const SHORT_EXCERPT_MAX = 140;
  function shortExcerpt(t) {
    const text = String(t.body_text || t.excerpt || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > SHORT_EXCERPT_MAX) return '';
    return text;
  }

  function buildMeta(t, opts) {
    const meta = document.createElement('p');
    meta.className = 'item__meta';

    const wantsChip = !opts || opts.chip !== false;
    if (wantsChip && t.filter && typeof t.filter.score === 'number') {
      meta.appendChild(buildScoreChip(t));
    }

    const text = document.createElement('span');
    text.className = 'item__meta-text';
    const tg = tagSummary(t);

    const time = document.createElement('span');
    time.className = 'item__time';
    time.textContent = relTime(t.created_at);
    time.title = absTime(t.created_at);

    const bits = [t.author || '匿名', time, '回复 ' + (t.reply_count || 0)];
    /* mobile keeps the meta to one line: views move to the drawer */
    if (isDesktop()) bits.push('浏览 ' + (t.views || 0));
    appendBits(text, bits);
    meta.appendChild(text);

    /* classification group is never truncated — the +N fold stays readable.
       Mobile drops it (and views) so author · time · replies always fit. */
    if (isDesktop()) {
      const tags = document.createElement('span');
      tags.className = 'item__meta-tags';
      const tagBits = [t.category || '未分类'];
      if (tg.text) tagBits.push(tg.text);
      appendBits(tags, tagBits);
      if (tg.extra > 0) tags.title = '标签 · 共 ' + tg.all.length + ' 个：' + tg.all.join('、');
      meta.appendChild(tags);
    }
    return meta;
  }

  function appendBits(host, bits) {
    bits.forEach((bit, i) => {
      if (i) {
        const dot = document.createElement('span');
        dot.className = 'dot';
        dot.textContent = ' · ';
        host.appendChild(dot);
      }
      if (bit === null) return; /* nothing to render (defensive) */
      if (typeof bit === 'string') {
        host.appendChild(document.createTextNode(bit));
        return;
      }
      host.appendChild(bit);
    });
  }

  function buildItem(t) {
    const node = document.createElement('article');
    node.className = 'item item--' + t.state;
    node.dataset.id = String(t.id);
    node.tabIndex = 0;
    node.setAttribute('role', 'button');
    const read = S.read.has(t.id);
    node.setAttribute('aria-label', itemLabel(t, read));
    /* read state is carried by the title weight/colour and a quiet unread dot only -
       never by a per-row 已读 badge */
    if (read) node.classList.add('is-read');

    const title = document.createElement('h3');
    title.className = 'item__title';
    title.textContent = t.title;
    node.appendChild(title);

    /* the picked column leads with what the filter found valuable */
    const picked = t.state === 'picked' && !!t.filter;
    if (picked) {
      const value = document.createElement('p');
      value.className = 'item__value';
      if (typeof t.filter.score === 'number') value.appendChild(buildScoreChip(t));
      if (t.filter.category) {
        const cat = document.createElement('span');
        cat.className = 'item__cat';
        cat.textContent = '判定 · ' + t.filter.category;
        value.appendChild(cat);
      }
      node.appendChild(value);

      if (t.filter.reason) node.appendChild(buildLine('item__reason', t.filter.reason));
      if (t.filter.summary) node.appendChild(buildLine('item__note', t.filter.summary));
      const short = shortExcerpt(t);
      if (short) node.appendChild(buildLine('item__excerpt', short));
    }

    node.appendChild(buildMeta(t, { chip: !picked }));

    if (t.id === S.openId) node.classList.add('is-current');
    return node;
  }

  function wrapItem(t, col) {
    const cell = document.createElement('div');
    cell.className = 'cell' + (col ? ' cell--c' + col : '');
    cell.appendChild(buildItem(t));
    return cell;
  }

  function buildCell(row, col, topic, appearing) {
    const cell = document.createElement('div');
    cell.className = 'cell cell--c' + col;
    /* Auto-placement in a 3-column grid keeps row i aligned; do not set
       inline gridColumn — those leak into the mobile 1-col template as
       implicit extra columns and overflow the viewport. */
    if (topic && cellHas(topic, col)) {
      cell.appendChild(buildItem(topic));
    } else {
      cell.classList.add('cell--empty');
      if (appearing) cell.classList.add('cell--appearing');
      cell.setAttribute('aria-hidden', 'true');
      const mark = document.createElement('span');
      mark.className = 'slot__mark';
      cell.appendChild(mark);
    }
    return cell;
  }

  function captureRects() {
    const map = new Map();
    el.board.querySelectorAll('.item').forEach((node) => {
      const r = node.getBoundingClientRect();
      map.set(Number(node.dataset.id), r);
    });
    return map;
  }

  /* FLIP primitive — transform/opacity only, delayed by `delay` ms.
     prefers-reduced-motion: no movement at all, an entering item only fades. */
  function animate(node, dx, dy, fade, delay) {
    const wait = delay || 0;
    if (reduced()) {
      if (!fade) return;
      node.style.transition = 'none';
      node.style.opacity = '0';
      void node.offsetWidth;
      requestAnimationFrame(() => {
        node.style.transition = 'opacity ' + CFG.fadeMs + 'ms linear';
        node.style.opacity = '1';
        const done = () => { node.style.transition = ''; node.style.opacity = ''; };
        node.addEventListener('transitionend', done, { once: true });
        window.setTimeout(done, CFG.fadeMs + 120);
      });
      return;
    }

    node.classList.add('is-flipping');
    node.style.transition = 'none';
    node.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
    if (fade) node.style.opacity = '0';
    void node.offsetWidth;
    requestAnimationFrame(() => {
      node.style.transition = 'transform ' + CFG.animMs + 'ms cubic-bezier(.2,.7,.2,1) ' + wait + 'ms' +
        (fade ? ', opacity ' + CFG.animMs + 'ms linear ' + wait + 'ms' : '');
      node.style.transform = 'translate(0,0)';
      if (fade) node.style.opacity = '1';
      const done = () => {
        node.style.transition = '';
        node.style.transform = '';
        node.style.opacity = '';
        node.classList.remove('is-flipping');
      };
      node.addEventListener('transitionend', done, { once: true });
      window.setTimeout(done, CFG.animMs + wait + 140);
    });
  }

  /* loading state: skeleton rows in all three columns, sized like real rows so
     the list does not jump when data lands (no spinner) */
  const SKELETON_ROWS = 14;
  function renderSkeleton() {
    el.board.hidden = false;
    el.board.dataset.tab = S.tab;
    el.board.classList.remove('is-compact');
    el.board.setAttribute('aria-busy', 'true');
    el.empties.hidden = true;

    const frag = document.createDocumentFragment();
    for (let row = 1; row <= SKELETON_ROWS; row++) {
      for (let col = 1; col <= 3; col++) {
        const cell = document.createElement('div');
        cell.className = 'cell cell--c' + col;
        const pickedRow = col === 2 && row % 6 === 2; /* ≈ the real picked density */
        if (col === 1 || pickedRow) {
          const sk = document.createElement('div');
          sk.className = 'skeleton' + (col === 2 ? ' skeleton--picked' : '');
          sk.setAttribute('aria-hidden', 'true');
          const bars = col === 2
            ? ['sk--title', 'sk--value', 'sk--reason', 'sk--note', 'sk--meta']
            : ['sk--title', 'sk--meta'];
          bars.forEach((bar, i) => {
            const b = document.createElement('span');
            b.className = 'sk ' + bar + (i === 0 && row % 3 === 0 ? ' sk--title-short' : '');
            sk.appendChild(b);
          });
          cell.appendChild(sk);
        } else {
          cell.classList.add('cell--empty');
          cell.setAttribute('aria-hidden', 'true');
          const mark = document.createElement('span');
          mark.className = 'slot__mark';
          cell.appendChild(mark);
        }
        frag.appendChild(cell);
      }
    }
    el.board.replaceChildren(frag);
  }

  function render(prevRects, newIds) {
    S.rows = (S.data ? S.data.topics : []).map((t, i) => ({ t: t, row: i + 1 }));
    el.board.dataset.tab = S.tab;
    /* gap: one shared grid, every topic sits at its own row index and the other
       two cells of that row stay as empty slots. compact: three independent
       stacks, so surviving items pull up per column. */
    const compact = isDesktop() && S.density === 'compact';
    el.board.classList.toggle('is-compact', compact);

    /* slots that were filled last render and are empty now fade in with their
       row (opacity only) instead of popping */
    const prevOccupied = S.occupied || new Set();
    const occupied = new Set();
    S.rows.forEach((r) => {
      [1, 2, 3].forEach((c) => { if (cellHas(r.t, c)) occupied.add(r.row + ':' + c); });
    });
    const appearing = new Set();
    prevOccupied.forEach((key) => { if (!occupied.has(key)) appearing.add(key); });
    S.occupied = occupied;

    const frag = document.createDocumentFragment();
    if (compact) {
      for (let col = 1; col <= 3; col++) {
        const stack = document.createElement('div');
        stack.className = 'col col--c' + col;
        S.rows.forEach((r) => { if (cellHas(r.t, col)) stack.appendChild(wrapItem(r.t, col)); });
        frag.appendChild(stack);
      }
    } else {
      S.rows.forEach((r) => {
        for (let col = 1; col <= 3; col++) {
          frag.appendChild(buildCell(r.row, col, r.t, appearing.has(r.row + ':' + col)));
        }
      });
    }
    el.board.replaceChildren(frag);

    flipItems(prevRects, newIds);

    S.first = false;
    updateEmpties();
  }

  /* FLIP: one batched geometry read, then transform/opacity-only writes, spread
     over at most CFG.maxGroups batches so a whole-board move cannot stutter */
  function flipItems(prevRects, newIds) {
    if (!prevRects) return;
    const moves = [];
    el.board.querySelectorAll('.item').forEach((node) => {
      const id = Number(node.dataset.id);
      const prev = prevRects.get(id);
      const isNew = !!(newIds && newIds.has(id));
      if (!prev) {
        if (isNew) moves.push({ node: node, dx: 0, dy: 0, fade: true });
        return;
      }
      const now = node.getBoundingClientRect();
      const dx = prev.left - now.left;
      const dy = prev.top - now.top;
      if (!isNew && Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      moves.push({ node: node, dx: dx, dy: dy, fade: isNew });
    });

    if (!moves.length) {
      if (DEV) { S.flipLog.push('moves=0 new=0'); el.board.dataset.flips = S.flipLog.join(' | '); }
      return;
    }
    const groups = Math.max(1, Math.min(CFG.maxGroups, moves.length));
    let maxDelay = 0;
    moves.forEach((m, i) => {
      const batch = Math.floor((i * groups) / moves.length);
      maxDelay = Math.max(maxDelay, batch * CFG.staggerMs);
      animate(m.node, m.dx, m.dy, m.fade, batch * CFG.staggerMs);
    });

    /* dev-only diagnostic: headless runs cannot observe a 300ms transition, so
       the batch summary is parked on the board (visible in a DOM dump) */
    if (DEV) {
      const first = moves[0];
      const entry = 'moves=' + moves.length + ' new=' + moves.filter((m) => m.fade).length +
        ' batches=' + groups + ' maxDelay=' + maxDelay + 'ms' +
        (reduced() ? ' mode=reduced-opacity-only' : ' mode=transform') +
        ' first=' + Math.round(first.dx) + ',' + Math.round(first.dy);
      S.flipLog.push(entry);
      el.board.dataset.flips = S.flipLog.join(' | ');
    }
  }

  function updateCounts() {
    const topics = S.data ? S.data.topics : [];
    const picked = topics.filter((t) => t.state === 'picked').length;
    let bookmarked = 0;
    S.queue.forEach((id) => { if (S.topics.has(id)) bookmarked += 1; });
    el.cAll.textContent = String(topics.length);
    el.cTotal.textContent = String(topics.length);
    el.cPicked.textContent = String(picked);
    el.cQueue.textContent = String(bookmarked);
    el.countline.setAttribute('aria-label',
      '全部 ' + topics.length + ' 条，精选 ' + picked + ' 条，收藏 ' + bookmarked + ' 条');
  }

  function updateHeader() {
    if (!S.data) return;
    const f = S.data.filter || {};
    el.model.textContent = shortModel(f.model);
    el.fetched.textContent = '抓取于 ' + relTime((S.data.source || {}).fetched_at || S.data.generated_at);
    el.fetched.title = 'fetched_at ' + (((S.data.source || {}).fetched_at) || '') +
      ' · generated_at ' + (S.data.generated_at || '');
  }

  function refreshTimes() {
    updateHeader();
    el.board.querySelectorAll('.item').forEach((node) => {
      const t = S.topics.get(Number(node.dataset.id));
      if (!t) return;
      const time = $('.item__time', node);
      if (time) {
        time.textContent = relTime(t.created_at);
        time.title = absTime(t.created_at);
      }
    });
    if (S.openId) fillDrawer(S.topics.get(S.openId));
  }

  /* ------------------------------------------------------------ state pulls */

  function applyState(data, prevRects) {
    S.data = data;
    S.topics.clear();
    data.topics.forEach((t) => S.topics.set(t.id, t));

    el.board.hidden = false;
    el.board.removeAttribute('aria-busy');

    const nowPicked = pickedIds(data);
    const newIds = new Set();
    if (!S.first) nowPicked.forEach((id) => { if (!S.seen.has(id)) newIds.add(id); });
    nowPicked.forEach((id) => S.seen.add(id));
    writeIds(CFG.lsSeen, S.seen);

    render(prevRects, newIds);
    updateCounts();
    updateHeader();
    if (S.openId && !S.topics.has(S.openId)) closeDrawer(false);
  }

  /* deduplicated: a refresh, a retry and the 30s tick that overlap share one read */
  let pullPromise = null;

  function pull(opts) {
    if (pullPromise) return pullPromise;
    pullPromise = doPull(opts).then((ok) => ok, () => false).finally(() => { pullPromise = null; });
    return pullPromise;
  }

  async function doPull(opts) {
    const prevRects = S.data ? captureRects() : null;
    let data;
    try {
      data = validate(await fetchState(opts));
    } catch (err) {
      /* an existing successful snapshot stays rendered, with a stale/error notice */
      const hint = DEV
        ? '无法读取 ' + stateUrl({ next: opts && opts.next }) +
          '（file:// 下浏览器会拦截 fetch，请用本地 http 服务打开；细节见 CONTRACT §6）'
        : describeReadError(err) + '。' + (S.data ? '仍显示上一次成功的数据。' : '') + '点「重试」再读一次。';
      showError(hint, err);
      return false;
    }
    clearError();
    applyState(data, prevRects);
    S.lastOkAt = nowIso();     /* feeds the collapsed facts of a later failure */
    return true;
  }

  /* ---------------------------------------------------------------- drawer  */

  const STATE_LABEL = { picked: '精选', rejected: '未入选', pending: '待筛选' };

  function fillDrawer(t) {
    if (!t) return;
    el.dState.textContent = '预览 · ' + (STATE_LABEL[t.state] || t.state);
    el.dTitle.textContent = t.title;

    const bits = [
      t.author || '匿名',
      relTime(t.created_at),
      '回复 ' + (t.reply_count || 0),
      '浏览 ' + (t.views || 0),
      t.category || '未分类'
    ];
    if (Array.isArray(t.tags) && t.tags.length) bits.push(t.tags.join('/'));
    el.dMeta.textContent = bits.join(' · ');
    el.dMeta.title = 'created_at ' + (t.created_at || '') + ' · bumped_at ' + (t.bumped_at || '');

    if (t.filter) {
      el.dFilter.hidden = false;
      el.dScore.textContent = (t.filter.score != null) ? String(t.filter.score) : '—';
      el.dCat.textContent = t.filter.category || '—';
      el.dReason.textContent = t.filter.reason || '';
      el.dReason.hidden = !t.filter.reason;
      el.dSummary.textContent = t.filter.summary || '';
      el.dSummary.hidden = !t.filter.summary;
    } else {
      el.dFilter.hidden = true;
    }

    if (t.body_text) {
      el.dBody.textContent = t.body_text;
      el.dBody.hidden = false;
      el.dBody.classList.remove('is-empty');
    } else if (t.excerpt) {
      el.dBody.textContent = t.excerpt;
      el.dBody.hidden = false;
      el.dBody.classList.remove('is-empty');
    } else {
      /* true of most live topics: the detail fetch never landed */
      el.dBody.textContent = '正文未抓取。可以点原文链接查看。';
      el.dBody.hidden = false;
      el.dBody.classList.add('is-empty');
    }

    el.dLink.href = t.url;
    el.dLink.setAttribute('aria-label', '在新标签打开原文：' + t.title);

    /* 收藏 is available for every topic - a rejected one can be bookmarked too */
    el.dQueue.hidden = false;
    const queued = S.queue.has(t.id);
    el.dQueue.textContent = queued ? '取消收藏' : '收藏';
    el.dQueue.setAttribute('aria-pressed', queued ? 'true' : 'false');

    const vote = S.votes.get(t.id);
    el.dKeep.setAttribute('aria-pressed', vote === 'keep' ? 'true' : 'false');
    el.dSkip.setAttribute('aria-pressed', vote === 'skip' ? 'true' : 'false');
    /* fixed labels: the active preference is the pressed state plus the one-line note */
    el.dKeep.textContent = '纳入精选';
    el.dSkip.textContent = '排除';
    if (vote === 'keep') {
      el.dTaste.hidden = false;
      el.dTaste.textContent = '已纳入精选';
    } else if (vote === 'skip') {
      el.dTaste.hidden = false;
      el.dTaste.textContent = '已排除';
    } else {
      el.dTaste.hidden = true;
    }

    /* the read control reflects (never changes) the browser-local state: opening the
       drawer again or the 30s time refresh must not undo a manual 未读 toggle */
    syncReadControl(t);
    if (el.dNote && document.activeElement !== el.dNote) el.dNote.value = '';
  }

  /* scrim only exists under 900px, and only while the sheet is open */
  function syncScrim() {
    el.scrim.hidden = !(!el.drawer.hidden && !isDesktop());
  }

  /* a topic can hold two slots at once (精选 + 收藏): every instance follows the drawer */
  function itemNodes(id) {
    return Array.from(el.board.querySelectorAll('.item[data-id="' + id + '"]'));
  }

  function unmarkCurrent(id) {
    itemNodes(id).forEach((n) => {
      n.classList.remove('is-current');
      n.removeAttribute('aria-expanded');
    });
  }

  /* The settled read trigger. An explicit open - click, tap, keyboard activation or the
     ?open= dev deep link - marks the topic read once the preview really shows content.
     A hover/focus preview is only a prefetch and never marks anything, and a preview
     with no body at all waits for the original link. Re-opening the topic the drawer
     already shows does NOT mark again: that would undo a manual 未读 toggle. Only moving
     to another topic (an explicit navigation change) marks again. */
  function drawerHasContent(t) {
    return !!String((t && t.body_text) || '').trim() || !!String((t && t.excerpt) || '').trim();
  }

  function markReadOnOpen(t, opts) {
    if (!opts || !opts.explicit) return;
    if (!drawerHasContent(t)) return;
    markRead(t.id, true);
  }

  function openDrawer(id, opts) {
    const t = S.topics.get(id);
    if (!t) return;
    const wasOpen = S.openId;
    const wasCommitted = wasOpen === id && S.pinned;  /* same topic, already explicitly open */
    if (wasOpen && wasOpen !== id) unmarkCurrent(wasOpen);
    S.openId = id;
    S.pinned = !!(opts && opts.pinned);
    if (opts && opts.trigger) S.lastTrigger = opts.trigger;

    fillDrawer(t);
    if (!wasCommitted) markReadOnOpen(t, opts);

    itemNodes(id).forEach((node) => {
      node.classList.add('is-current');
      node.setAttribute('aria-expanded', 'true');
    });

    window.clearTimeout(hideTimer);
    if (!el.drawer.hidden) {
      el.drawer.classList.add('is-open');
      syncScrim();
      return;
    }
    el.drawer.hidden = false;
    syncScrim();
    if (reduced()) {
      el.drawer.classList.add('is-open');
      return;
    }
    requestAnimationFrame(() => el.drawer.classList.add('is-open'));
  }

  function closeDrawer(restoreFocus) {
    if (el.drawer.hidden) return;
    const id = S.openId;
    if (id) unmarkCurrent(id);
    S.openId = null;
    S.pinned = false;
    el.drawer.classList.remove('is-open');
    const finish = () => { el.drawer.hidden = true; syncScrim(); };
    syncScrim();
    if (reduced()) finish();
    else {
      window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(finish, CFG.animMs + 40);
    }
    if (restoreFocus && S.lastTrigger && document.contains(S.lastTrigger)) {
      try { S.lastTrigger.focus({ preventScroll: true }); } catch (err) { /* noop */ }
    }
  }

  function scheduleClose() {
    window.clearTimeout(closeTimer);
    closeTimer = window.setTimeout(() => {
      if (S.pinned) return;
      if (el.drawer.contains(document.activeElement)) return;
      closeDrawer(false);
    }, CFG.closeMs);
  }

  /* --------------------------------------------------------------- owner auth  */
  /* The owner token lives in sessionStorage only: never in the URL, localStorage, the
     source or a log line. Without it the page is read-only - browsing, the drawer and the
     original links keep working, the write controls are disabled with an explicit reason. */
  let OWNER = false;

  function ownerToken() {
    try { return sessionStorage.getItem(CFG.ssOwner) || ''; } catch (err) { return ''; }
  }

  function setOwnerToken(value) {
    const token = (value || '').trim();
    try {
      if (token) sessionStorage.setItem(CFG.ssOwner, token);
      else sessionStorage.removeItem(CFG.ssOwner);
    } catch (err) { /* storage blocked: stay read-only */ }
    OWNER = !!token;
    return OWNER;
  }

  function canWrite() { return DEV || OWNER; }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    const token = ownerToken();
    if (token) h.Authorization = 'Bearer ' + token;
    return h;
  }

  function setOpStatus(message, ok) {
    if (!el.opstatus) return;
    el.opstatus.hidden = !message;
    el.opstatus.textContent = message || '';
    el.opstatus.title = message || '';
    el.opstatus.classList.toggle('chip--warn', !ok && !!message);
    el.opstatus.classList.toggle('chip--ok', !!ok && !!message);
  }

  const WRITE_CONTROLS = () => [
    [el.refresh, '刷新'],
    [el.dQueue, '收藏'],
    [el.dKeep, '筛选'],
    [el.dSkip, '筛选']
  ];

  function refreshAuthUi() {
    OWNER = !!ownerToken();
    const writable = canWrite();
    if (el.authchip) {
      /* the small indicator carries the auth state - the 管理 button never changes label */
      el.authchip.hidden = false;
      el.authchip.textContent = DEV ? 'DEV · 只读' : (OWNER ? '已连接 · 可写' : '只读');
      el.authchip.title = OWNER
        ? 'owner token 保存在本标签页的 sessionStorage，关掉标签页即失效'
        : '只读：写入（收藏/筛选/刷新）已禁用。点「管理」粘贴 owner token 后开启。';
      el.authchip.classList.toggle('chip--ok', OWNER && !DEV);
    }
    if (el.owner) {
      el.owner.textContent = '管理';
      el.owner.dataset.owner = OWNER ? 'on' : 'off';
      el.owner.title = OWNER
        ? '已连接（token 只在本标签页）；点这里可更换或清除'
        : '未连接 · 点这里粘贴 owner token（只保存在本标签页）';
    }
    WRITE_CONTROLS().forEach((pair) => {
      const btn = pair[0];
      if (!btn) return;
      btn.disabled = !writable;
      btn.title = writable ? '' : '只读模式：' + pair[1] + '需要 owner token，点「管理」填入';
    });
  }

  function opErrorText(status, data) {
    if (status === 401) return '写入被拒（401）：token 无效或已失效，请重新点「管理」';
    if (status === 403) return '请求被拒（403）：来源不在后端允许列表';
    if (status === 404) return '写入失败（404）：目标不存在';
    if (status === 413) return '写入失败（413）：请求体过大';
    if (status === 503) return '写入已关闭（503）：后端还没有配置 owner token';
    return '写入失败：HTTP ' + status + ((data && data.error) ? ' · ' + data.error : '');
  }

  /* Every write goes through here. It waits for the server's acknowledgement, surfaces the
     failure instead of pretending success, and re-enables the control in `finally`. */
  async function mutate(path, body, btn) {
    if (DEV) return { ok: true, dev: true, data: {} };   /* fixture mode: local only */
    if (!OWNER) {
      setOpStatus('只读模式：点「管理」填入 owner token 才能写入', false);
      return { ok: false, reason: 'read-only' };
    }
    if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', '1'); }
    try {
      const res = await fetch(api(path), {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body || {})
      });
      let data = null;
      try { data = await res.json(); } catch (err) { data = null; }
      if (!res.ok) {
        setOpStatus(opErrorText(res.status, data), false);
        return { ok: false, status: res.status, data: data };
      }
      setOpStatus('', true);
      return { ok: true, status: res.status, data: data || {} };
    } catch (err) {
      setOpStatus('网络错误：' + ((err && err.message) ? err.message : '写入请求失败'), false);
      return { ok: false, reason: 'network' };
    } finally {
      if (btn) { btn.removeAttribute('aria-busy'); btn.disabled = !canWrite(); }
    }
  }

  /* ----------------------------------------------------------------- queue  */

  /* The server's queue is authoritative: replace the local copy with the queue from the
     mutation response (or from GET /api/queue) instead of unioning a stale cache. */
  function applyQueueSnapshot(data) {
    if (data && Array.isArray(data.queue)) {
      S.queue = new Set(data.queue.map(Number).filter(Number.isFinite));
      writeIds(CFG.lsQueue, S.queue);
    }
    render(captureRects(), null);
    updateCounts();
    if (S.openId) {
      const t = S.topics.get(S.openId);
      if (t) fillDrawer(t);
    }
  }

  async function toggleQueue(id) {
    const t = S.topics.get(id);
    if (!t) return;
    if (DEV) {          /* fixture mode: a local interaction, no backend behind it */
      const prevRects = captureRects();
      if (S.queue.has(id)) S.queue.delete(id);
      else S.queue.add(id);
      writeIds(CFG.lsQueue, S.queue);
      render(prevRects, null);
      updateCounts();
      if (S.openId === id) fillDrawer(t);
      return;
    }
    if (!OWNER) {
      setOpStatus('只读模式：收藏需要 owner token，点「管理」填入', false);
      return;
    }
    const add = !S.queue.has(id);
    const res = await mutate(CFG.queue, add ? { add: id } : { remove: id }, S.openId === id ? el.dQueue : null);
    if (!res.ok) return;               /* the column only moves after the server agrees */
    applyQueueSnapshot(res.data);
  }

  async function applyVote(id, vote) {
    const t = S.topics.get(id);
    if (!t) return;
    if (DEV) {          /* fixture mode: exercise the interaction locally */
      const prevRects = captureRects();
      S.votes.set(id, vote);
      if (vote === 'keep') {
        t.state = 'picked';
        t.rescued = true;
      } else {
        t.state = 'rejected';
        t.skipped = true;
      }
      /* the vote moves 全部 <-> 精选 only; 收藏 is a separate signal and is untouched */
      render(prevRects, null);
      updateCounts();
      redrawOpen();
      return;
    }
    if (!OWNER) {
      setOpStatus('只读模式：筛选需要 owner token，点「管理」填入', false);
      return;
    }
    const note = (el.dNote && el.dNote.value || '').trim();
    const btn = vote === 'keep' ? el.dKeep : el.dSkip;
    /* one request: the server stores the override and saves before answering */
    const res = await mutate(CFG.feedback, { id: id, vote: vote, note: note }, btn);
    if (!res.ok) return;               /* nothing changes locally on failure */
    const payload = res.data || {};
    if (payload.topic) {
      t.state = payload.topic.state;
      if (payload.topic.rescued) t.rescued = true;
      if (payload.topic.skipped) t.skipped = true;
    }
    S.votes.set(id, vote);
    /* the vote moves the topic between 全部 and 精选 only. The response's queue is
       deliberately not applied: a vote can never add or remove a 收藏. */
    render(captureRects(), null);
    updateCounts();
    redrawOpen();
  }

  function redrawOpen() {
    if (!S.openId) return;
    const cur = S.topics.get(S.openId);
    if (cur) fillDrawer(cur);
  }

  /* ---------------------------------------------------------------- events */

  el.board.addEventListener('mouseover', (ev) => {
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    const from = ev.relatedTarget && ev.relatedTarget.closest ? ev.relatedTarget.closest('.item') : null;
    if (from === node) return;
    window.clearTimeout(closeTimer);
    window.clearTimeout(hoverTimer);
    hoverTimer = window.setTimeout(() => openDrawer(Number(node.dataset.id), { trigger: node }), CFG.hoverMs);
  });

  el.board.addEventListener('mouseout', (ev) => {
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    const to = ev.relatedTarget && ev.relatedTarget.closest ? ev.relatedTarget.closest('.item') : null;
    if (to === node) return;
    window.clearTimeout(hoverTimer);
    scheduleClose();
  });

  el.board.addEventListener('focusin', (ev) => {
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    /* keyboard focus only — a mouse click focuses the item too and would
       otherwise fight with click-to-queue */
    if (lastInput !== 'keyboard' && !node.matches(':focus-visible')) return;
    window.clearTimeout(closeTimer);
    window.clearTimeout(hoverTimer);
    openDrawer(Number(node.dataset.id), { trigger: node });
  });

  document.addEventListener('focusin', (ev) => {
    if (!S.openId || S.pinned) return;
    if (ev.target.closest && (ev.target.closest('.item') || ev.target.closest('.drawer'))) return;
    closeDrawer(false);
  });

  el.board.addEventListener('pointerdown', (ev) => {
    lastInput = (ev.pointerType === 'touch') ? 'touch' : 'mouse';
  }, true);

  el.board.addEventListener('click', (ev) => {
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    const t = S.topics.get(Number(node.dataset.id));
    if (!t) return;
    if (!isTouchGesture() && nodeCol(node) === 2) toggleQueue(t.id);
    else openDrawer(t.id, { pinned: true, explicit: true, trigger: node });
  });

  el.board.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    ev.preventDefault();
    const t = S.topics.get(Number(node.dataset.id));
    if (!t) return;
    if (nodeCol(node) === 2) toggleQueue(t.id);
    else openDrawer(t.id, { pinned: true, explicit: true, trigger: node });
  });

  document.addEventListener('keydown', (ev) => {
    lastInput = 'keyboard';
    if (ev.key === 'Escape' && S.openId) {
      ev.preventDefault();
      closeDrawer(true);
    }
  });

  el.drawer.addEventListener('mouseenter', () => window.clearTimeout(closeTimer));
  el.drawer.addEventListener('mouseleave', scheduleClose);

  /* mobile: tapping the dimmed area closes the sheet */
  el.scrim.addEventListener('click', () => closeDrawer(true));

  el.dClose.addEventListener('click', () => closeDrawer(true));

  el.dQueue.addEventListener('click', () => {
    if (S.openId) toggleQueue(S.openId);
  });
  if (el.dKeep) el.dKeep.addEventListener('click', () => { if (S.openId) applyVote(S.openId, 'keep'); });
  if (el.dSkip) el.dSkip.addEventListener('click', () => { if (S.openId) applyVote(S.openId, 'skip'); });

  /* icon-only, reversible, browser-local (never a server write) */
  if (el.dRead) el.dRead.addEventListener('click', () => { if (S.openId) markRead(S.openId); });

  /* opening the original link counts as reading even when no body was fetched */
  el.dLink.addEventListener('click', () => { if (S.openId) markRead(S.openId, true); });

  el.density.addEventListener('click', () => {
    const prevRects = captureRects();
    S.density = S.density === 'compact' ? 'gap' : 'compact';
    el.density.setAttribute('aria-pressed', S.density === 'compact' ? 'true' : 'false');
    el.density.title = '空位显示方式：' + (S.density === 'compact' ? '紧凑（空位折叠）' : '间隙（空位保留）');
    try { localStorage.setItem(CFG.lsDensity, S.density); } catch (err) { /* noop */ }
    render(prevRects, null);
  });

  el.refresh.addEventListener('click', async () => {
    if (el.refresh.getAttribute('aria-busy') === '1') return;
    el.refresh.setAttribute('aria-busy', '1');
    el.refresh.textContent = '刷新中';
    try {
      if (DEV) {
        /* dev: pretend the backend returned a newer state */
        const ok = await pull({ next: true });
        if (!ok) await pull({});
      } else {
        if (!OWNER) {
          setOpStatus('只读模式：刷新会触发服务端抓取，需要 owner token（点「管理」）', false);
          return;
        }
        /* explicit acknowledgement: a refused refresh stays visible instead of looking fine */
        const res = await mutate(CFG.refresh, {}, el.refresh);
        if (!res.ok) return;
        await sleep(1200);
        await pull({});
      }
    } finally {
      el.refresh.removeAttribute('aria-busy');
      el.refresh.textContent = '刷新';
      el.refresh.disabled = !canWrite();
    }
  });

  el.owner.addEventListener('click', async () => {
    if (DEV) {
      setOpStatus('fixture 模式：交互只在本地生效，不需要 token', true);
      return;
    }
    const entered = window.prompt(
      OWNER ? 'owner token（留空清除，只保存在本标签页）' : 'owner token（只保存在本标签页）',
      ''
    );
    if (entered === null) return;               /* cancelled: keep whatever was there */
    setOwnerToken(entered);
    refreshAuthUi();
    if (!OWNER) {
      setOpStatus('已清除 token：回到只读模式', true);
      return;
    }
    /* verify against the server instead of claiming it works */
    try {
      const res = await fetch(api(CFG.feedback), { cache: 'no-store', headers: authHeaders() });
      if (res.ok) setOpStatus('已连接：可以收藏 / 筛选 / 刷新', true);
      else setOpStatus(opErrorText(res.status, null) + '（token 已保留，可改后再试）', false);
    } catch (err) {
      setOpStatus('token 已保存，但校验请求失败：' + ((err && err.message) ? err.message : '网络错误'), false);
    }
    await loadVotes();
  });

  el.retry.addEventListener('click', async () => {
    if (el.retry.getAttribute('aria-busy') === '1') return;
    el.retry.setAttribute('aria-busy', '1');
    el.retry.textContent = '重试中';
    clearError();
    /* with no data yet the board is hidden: show skeletons again while retrying */
    if (!S.data) renderSkeleton();
    try {
      await pull({});
    } finally {
      el.retry.removeAttribute('aria-busy');
      el.retry.textContent = '重试';
    }
  });

  el.tabs.addEventListener('click', (ev) => {
    const btn = ev.target.closest ? ev.target.closest('.tab') : null;
    if (!btn) return;
    selectTab(btn.dataset.tab);
  });

  el.tabs.addEventListener('keydown', (ev) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    const tabs = Array.from(el.tabs.querySelectorAll('.tab'));
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    ev.preventDefault();
    const next = tabs[(i + (ev.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    next.focus();
    selectTab(next.dataset.tab);
  });

  function selectTab(tab) {
    if (!tab || tab === S.tab) return;
    const prevRects = captureRects();
    S.tab = tab;
    el.tabs.querySelectorAll('.tab').forEach((b) => {
      b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
    });
    render(prevRects, null);
  }

  window.addEventListener('resize', () => {
    /* grid placement switches between explicit rows (desktop) and flow (mobile) */
    if (S.data) render(captureRects(), null);
    syncScrim();
  });

  if (mDesktop.addEventListener) mDesktop.addEventListener('change', () => {
    if (S.data) render(captureRects(), null);
    syncScrim();
  });

  /* mobile: swipe left/right between the three lists; taps stay on the tabs */
  let swipeX = 0;
  let swipeY = 0;
  let swipeT = 0;

  el.board.addEventListener('touchstart', (ev) => {
    if (isDesktop() || ev.touches.length !== 1) return;
    swipeX = ev.touches[0].clientX;
    swipeY = ev.touches[0].clientY;
    swipeT = Date.now();
  }, { passive: true });

  el.board.addEventListener('touchend', (ev) => {
    if (isDesktop() || !swipeT) return;
    const touch = ev.changedTouches[0];
    const dx = touch.clientX - swipeX;
    const dy = touch.clientY - swipeY;
    const dt = Date.now() - swipeT;
    swipeT = 0;
    if (dt > 700 || Math.abs(dx) < 48 || Math.abs(dy) > 40) return;
    const order = ['all', 'picked', 'queue'];
    const i = order.indexOf(S.tab);
    const next = order[i + (dx < 0 ? 1 : -1)];
    if (next) selectTab(next);
  }, { passive: true });

  /* ------------------------------------------------------------------ init  */

  async function loadVotes() {
    if (DEV || !OWNER) return;   /* owner notes are not public: no anonymous request */
    try {
      const res = await fetch(api(CFG.feedback), { cache: 'no-store', headers: authHeaders() });
      if (!res.ok) {
        if (res.status === 401) setOpStatus('owner token 无效（401）：写入被拒，请重新点「管理」', false);
        else if (res.status === 503) setOpStatus('后端未配置 owner token（503）：写入整体关闭', false);
        return;
      }
      const data = await res.json();
      const rows = Array.isArray(data.feedback) ? data.feedback : [];
      rows.forEach((row) => {
        const n = Number(row.id);
        if (Number.isFinite(n) && (row.vote === 'keep' || row.vote === 'skip')) S.votes.set(n, row.vote);
      });
    } catch (err) { /* offline: votes stay empty until the next successful fetch */ }
  }

  async function loadQueue() {
    if (DEV) {                       /* fixture mode: the local copy is the whole story */
      S.queue = readIds(CFG.lsQueue);
      return;
    }
    try {
      const res = await fetch(api(CFG.queue), { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const ids = Array.isArray(data.queue) ? data.queue.map(Number).filter(Number.isFinite) : [];
      /* the server's list replaces the local copy: no union with a stale cache */
      S.queue = new Set(ids);
      writeIds(CFG.lsQueue, S.queue);
      if (S.data) { render(captureRects(), null); updateCounts(); }
    } catch (err) {
      S.queue = readIds(CFG.lsQueue);   /* offline: last known copy, clearly provisional */
      setOpStatus('收藏列表来自本地缓存（没连上后端）', false);
    }
  }

  async function checkHealth() {
    if (DEV) {
      if (DEV_HEALTH === 'attention') {
        el.warnchip.hidden = false;
        el.warnchip.title = 'dev：模拟 /health attention.needed = true';
      }
      return;
    }
    try {
      const res = await fetch(api(CFG.health), { cache: 'no-store' });
      if (!res.ok) return;
      const h = await res.json();
      const att = h.attention || {};
      if (att.needed) {
        el.warnchip.hidden = false;
        el.warnchip.title = att.reason || '需要人工排查';
      } else {
        el.warnchip.hidden = true;
      }
      if (h.auth && h.auth.token_configured === false && el.authchip) {
        /* the backend has no token at all: writes are closed for everyone, not just here */
        el.authchip.title = el.authchip.title + '（后端未配置 owner token：写入整体关闭）';
      }
    } catch (err) { /* health is optional for rendering */ }
  }

  function initFromUrl() {
    if (DEV) {
      el.devbadge.hidden = false;
      el.devbadge.title = 'dev 模式：数据来自 ' + (params.get('phase') === 'next' ? CFG.fixtureNext : CFG.fixtureBase) +
        '，刷新按钮会加载 ' + CFG.fixtureNext + '（若存在）';
    }
    try {
      const d = localStorage.getItem(CFG.lsDensity);
      if (d === 'compact' || d === 'gap') S.density = d;
    } catch (err) { /* noop */ }
    /* ?tab=picked|queue — deep link into one list (same code path as tap/swipe) */
    const wanted = params.get('tab');
    if (wanted === 'all' || wanted === 'picked' || wanted === 'queue') {
      S.tab = wanted;
      el.tabs.querySelectorAll('.tab').forEach((b) => {
        b.setAttribute('aria-selected', b.dataset.tab === S.tab ? 'true' : 'false');
      });
    }
    el.density.setAttribute('aria-pressed', S.density === 'compact' ? 'true' : 'false');
    el.density.title = '空位显示方式：' + (S.density === 'compact' ? '紧凑（空位折叠）' : '间隙（空位保留）');
  }

  async function init() {
    initFromUrl();
    refreshAuthUi();
    S.seen = readIds(CFG.lsSeen);
    S.read = loadRead();
    renderSkeleton();
    S.occupied = new Set();
    await loadQueue();
    await loadVotes();
    await pull({ next: DEV && params.get('phase') === 'next' });
    openFromUrl();
    autoDemo();
    checkHealth();
    tickTimer = window.setInterval(refreshTimes, 30000);
  }

  /* ?open=<id> — dev helper: open the preview panel without a hover */
  function openFromUrl() {
    if (!OPEN_ID) return;
    const node = el.board.querySelector('.item[data-id="' + OPEN_ID + '"]');
    if (!node) return;
    /* an explicit open: the deep link is a navigation, so it marks read when the
       preview really carries content */
    openDrawer(OPEN_ID, { pinned: true, explicit: true, trigger: node });
  }

  /* ?auto=<n> — dev helper: press 刷新 n times so refresh-driven animation
     (newly picked FLIP) can be observed or screenshotted headlessly */
  async function autoDemo() {
    if (!DEV_AUTO) return;
    for (let i = 0; i < DEV_AUTO; i++) {
      await sleep(1200);
      el.refresh.click();
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) window.clearInterval(tickTimer);
    else { refreshTimes(); tickTimer = window.setInterval(refreshTimes, 30000); }
  });

  /* another tab of the same deployment marked something read: mirror the change */
  window.addEventListener('storage', (ev) => {
    if (!ev || ev.key !== readKey()) return;
    S.read = loadRead();
    syncReadNodes();
  });

  init();
})();
