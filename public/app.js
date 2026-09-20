/* linux.do AI feed — frontend v0 (vanilla, no build step)
 *
 * Data: GET /api/state  (see CONTRACT.md §1)
 * Dev switch: ?fixture=1 loads fixtures/state.sample.json (served copy lives in
 * public/fixtures/). Dev-only extras: &phase=next loads the "next" fixture,
 * &health=attention fakes the /health attention chip.
 */
'use strict';

(function () {
  const CFG = {
    state: '/api/state',
    refresh: '/api/refresh',
    health: '/health',
    queue: '/api/queue',
    fixtureBase: 'fixtures/state.sample.json',
    fixtureNext: 'fixtures/state.sample.next.json',
    lsQueue: 'linuxdo-ai.queue',
    lsSeen: 'linuxdo-ai.seenPicked',
    lsDensity: 'linuxdo-ai.density',
    hoverMs: 250,
    closeMs: 180,
    animMs: 300
  };

  const params = new URLSearchParams(location.search);
  const DEV = params.get('fixture') === '1';
  const DEV_HEALTH = params.get('health');
  const DEV_SLOW = Number(params.get('slow')) || 0; /* dev: hold the fetch to show skeletons */
  const DEV_FAIL = params.get('fail') === '1';      /* dev: force the /api/state error state */

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
    retry: $('#retry'),
    refresh: $('#refresh'),
    density: $('#density'),
    tabs: $('#tabs'),
    drawer: $('#drawer'),
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
    dLink: $('#d-link'),
    dClose: $('#d-close')
  };

  const S = {
    data: null,
    topics: new Map(),
    seen: new Set(),
    queue: new Set(),
    density: 'gap',
    tab: 'all',
    openId: null,
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

  function readIds(key) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return new Set();
      const arr = JSON.parse(raw);
      return new Set((Array.isArray(arr) ? arr : [])
        .map((n) => Number(n))
        .filter((n) => Number.isFinite(n)));
    } catch (err) {
      return new Set();
    }
  }

  function writeIds(key, set) {
    try {
      localStorage.setItem(key, JSON.stringify(Array.from(set)));
    } catch (err) {
      /* storage disabled — in-memory only */
    }
  }

  const sleep = (ms) => new Promise((r) => window.setTimeout(r, ms));

  function showError(detail) {
    el.errordetail.textContent = detail;
    el.errorstate.hidden = false;
    if (!S.data) { /* nothing rendered yet — the error replaces the board */
      el.board.hidden = true;
      el.board.removeAttribute('aria-busy');
      el.empties.hidden = true;
    }
  }

  function clearError() {
    el.errorstate.hidden = true;
    el.errordetail.textContent = '';
  }

  /* per-column empty explanations; on mobile only the visible list gets one */
  function updateEmpties() {
    const topics = S.data ? S.data.topics : [];
    const picked = topics.filter((t) => t.state === 'picked').length;
    let queued = 0;
    S.queue.forEach((id) => { if (S.topics.has(id)) queued += 1; });
    const show = { c1: false, c2: false, c3: false };

    if (!S.data || el.board.hidden) {
      el.empties.hidden = true;
      return;
    }
    if (isDesktop()) {
      show.c1 = topics.length - picked === 0;
      show.c2 = picked - queued === 0;
      show.c3 = queued === 0;
    } else if (S.tab === 'all') {
      show.c1 = topics.length === 0;
    } else if (S.tab === 'picked') {
      show.c2 = picked === 0;
    } else {
      show.c3 = queued === 0;
    }

    el.emptyC2.textContent = picked > 0 ? '通过筛选的帖子都已在待读。' : '中栏还没有通过筛选的帖子。';
    el.emptyC1.hidden = !show.c1;
    el.emptyC2.hidden = !show.c2;
    el.emptyC3.hidden = !show.c3;
    el.empties.hidden = !(show.c1 || show.c2 || show.c3);
  }

  /* -------------------------------------------------------------- data load */

  function stateUrl(opts) {
    const bust = (opts && opts.bust === false) ? '' : ((opts && opts.bust) || '?t=' + Date.now());
    if (!DEV) return CFG.state + (bust || '?t=' + Date.now());
    const file = (opts && opts.next) ? CFG.fixtureNext : CFG.fixtureBase;
    return file + (bust || '?t=' + Date.now());
  }

  async function fetchState(opts) {
    if (DEV_FAIL) throw new Error('dev：?fail=1 模拟 /api/state 失败');
    if (DEV_SLOW) await sleep(DEV_SLOW);
    const res = await fetch(stateUrl(opts), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  function validate(data) {
    if (!data || !Array.isArray(data.topics)) throw new Error('payload 缺少 topics');
    return data;
  }

  function colOf(t) {
    if (t.state !== 'picked') return 1;
    return S.queue.has(t.id) ? 3 : 2;
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

  function itemLabel(t) {
    const score = (t.filter && typeof t.filter.score === 'number') ? ('，模型评分 ' + t.filter.score) : '';
    return '打开预览：' + t.title + '。' + metaText(t) + score;
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

    const bits = [
      t.author || '匿名',
      time,
      '回复 ' + (t.reply_count || 0),
      '浏览 ' + (t.views || 0)
    ];
    appendBits(text, bits);
    meta.appendChild(text);

    /* classification group is never truncated — the +N fold stays readable */
    const tags = document.createElement('span');
    tags.className = 'item__meta-tags';
    const tagBits = [t.category || '未分类'];
    if (tg.text) tagBits.push(tg.text);
    appendBits(tags, tagBits);
    if (tg.extra > 0) tags.title = '标签 · 共 ' + tg.all.length + ' 个：' + tg.all.join('、');
    meta.appendChild(tags);
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
    node.setAttribute('aria-label', itemLabel(t));

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

  function buildCell(row, col, topic) {
    const cell = document.createElement('div');
    cell.className = 'cell cell--c' + col;
    /* Auto-placement in a 3-column grid keeps row i aligned; do not set
       inline gridColumn — those leak into the mobile 1-col template as
       implicit extra columns and overflow the viewport. */
    if (topic && colOf(topic) === col) {
      cell.appendChild(buildItem(topic));
    } else {
      cell.classList.add('cell--empty');
      cell.setAttribute('aria-hidden', 'true');
      const hair = document.createElement('span');
      hair.className = 'slot__mark';
      cell.appendChild(hair);
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

  function animate(node, dx, dy, fade) {
    if (reduced()) return;
    node.style.transition = 'none';
    node.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
    if (fade) node.style.opacity = '0';
    void node.offsetWidth;
    requestAnimationFrame(() => {
      node.style.transition = 'transform ' + CFG.animMs + 'ms cubic-bezier(.2,.7,.2,1)' +
        (fade ? ', opacity ' + CFG.animMs + 'ms linear' : '');
      node.style.transform = 'translate(0,0)';
      if (fade) node.style.opacity = '1';
      const done = () => {
        node.style.transition = '';
        node.style.transform = '';
        node.style.opacity = '';
      };
      node.addEventListener('transitionend', done, { once: true });
      window.setTimeout(done, CFG.animMs + 140);
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

    const frag = document.createDocumentFragment();
    if (compact) {
      for (let col = 1; col <= 3; col++) {
        const stack = document.createElement('div');
        stack.className = 'col col--c' + col;
        S.rows.forEach((r) => { if (colOf(r.t) === col) stack.appendChild(wrapItem(r.t, col)); });
        frag.appendChild(stack);
      }
    } else {
      S.rows.forEach((r) => {
        for (let col = 1; col <= 3; col++) frag.appendChild(buildCell(r.row, col, r.t));
      });
    }
    el.board.replaceChildren(frag);

    if (prevRects && !reduced()) {
      el.board.querySelectorAll('.item').forEach((node) => {
        const prev = prevRects.get(Number(node.dataset.id));
        const isNew = !!(newIds && newIds.has(Number(node.dataset.id)));
        if (!prev) {
          if (isNew) animate(node, 0, 0, true);
          return;
        }
        const now = node.getBoundingClientRect();
        const dx = prev.left - now.left;
        const dy = prev.top - now.top;
        if (!isNew && Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
        animate(node, dx, dy, isNew);
      });
    }

    S.first = false;
    updateEmpties();
  }

  function updateCounts() {
    const topics = S.data ? S.data.topics : [];
    const picked = topics.filter((t) => t.state === 'picked').length;
    let queued = 0;
    S.queue.forEach((id) => { if (S.topics.has(id)) queued += 1; });
    el.cAll.textContent = String(topics.length);
    el.cTotal.textContent = String(topics.length);
    el.cPicked.textContent = String(picked);
    el.cQueue.textContent = String(queued);
    el.countline.setAttribute('aria-label',
      '全部 ' + topics.length + ' 条，通过 ' + picked + ' 条，待读 ' + queued + ' 条');
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

  async function pull(opts) {
    const prevRects = S.data ? captureRects() : null;
    let data;
    try {
      data = validate(await fetchState(opts));
    } catch (err) {
      const hint = DEV
        ? '无法读取 ' + stateUrl({ next: opts && opts.next }) +
          '（file:// 下浏览器会拦截 fetch，请用本地 http 服务打开；细节见 CONTRACT §6）'
        : '无法读取 /api/state（' + err.message + '）。后台可能正在重启，稍后重试。';
      showError(hint);
      return false;
    }
    clearError();
    applyState(data, prevRects);
    return true;
  }

  /* ---------------------------------------------------------------- drawer  */

  const STATE_LABEL = { picked: '已通过', rejected: '已筛掉', pending: '待筛选' };

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
    } else if (t.excerpt) {
      el.dBody.textContent = t.excerpt;
      el.dBody.hidden = false;
    } else {
      el.dBody.textContent = t.detail_fetched ? '正文为空。' : '正文未抓取（详情请求失败）。';
      el.dBody.hidden = false;
    }

    el.dLink.href = t.url;
    el.dLink.setAttribute('aria-label', '在新标签打开原文：' + t.title);

    if (t.state === 'picked') {
      el.dQueue.hidden = false;
      const queued = S.queue.has(t.id);
      el.dQueue.textContent = queued ? '从待读移出' : '移到待读';
      el.dQueue.setAttribute('aria-pressed', queued ? 'true' : 'false');
    } else {
      el.dQueue.hidden = true;
    }
  }

  function openDrawer(id, opts) {
    const t = S.topics.get(id);
    if (!t) return;
    const wasOpen = S.openId;
    if (wasOpen && wasOpen !== id) {
      const prev = el.board.querySelector('.item[data-id="' + wasOpen + '"]');
      if (prev) {
        prev.classList.remove('is-current');
        prev.removeAttribute('aria-expanded');
      }
    }
    S.openId = id;
    S.pinned = !!(opts && opts.pinned);
    if (opts && opts.trigger) S.lastTrigger = opts.trigger;

    fillDrawer(t);

    const node = el.board.querySelector('.item[data-id="' + id + '"]');
    if (node) {
      node.classList.add('is-current');
      node.setAttribute('aria-expanded', 'true');
    }

    window.clearTimeout(hideTimer);
    if (!el.drawer.hidden) {
      el.drawer.classList.add('is-open');
      return;
    }
    el.drawer.hidden = false;
    if (reduced()) {
      el.drawer.classList.add('is-open');
      return;
    }
    requestAnimationFrame(() => el.drawer.classList.add('is-open'));
  }

  function closeDrawer(restoreFocus) {
    if (el.drawer.hidden) return;
    const id = S.openId;
    const node = id ? el.board.querySelector('.item[data-id="' + id + '"]') : null;
    if (node) {
      node.classList.remove('is-current');
      node.removeAttribute('aria-expanded');
    }
    S.openId = null;
    S.pinned = false;
    el.drawer.classList.remove('is-open');
    const finish = () => { el.drawer.hidden = true; };
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

  /* ----------------------------------------------------------------- queue  */

  function postQueue() {
    if (DEV) return; /* no backend behind the fixture — stay quiet */
    try {
      fetch(CFG.queue, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ queue: Array.from(S.queue) })
      }).catch(() => { /* fire and forget */ });
    } catch (err) { /* fire and forget */ }
  }

  function toggleQueue(id) {
    const t = S.topics.get(id);
    if (!t || t.state !== 'picked') return;
    const prevRects = captureRects();
    if (S.queue.has(id)) S.queue.delete(id);
    else S.queue.add(id);
    writeIds(CFG.lsQueue, S.queue);
    postQueue();
    render(prevRects, null);
    updateCounts();
    if (S.openId === id) fillDrawer(t);
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
    if (!isTouchGesture() && colOf(t) === 2) toggleQueue(t.id);
    else openDrawer(t.id, { pinned: true, trigger: node });
  });

  el.board.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
    const node = ev.target.closest ? ev.target.closest('.item') : null;
    if (!node) return;
    ev.preventDefault();
    const t = S.topics.get(Number(node.dataset.id));
    if (!t) return;
    if (colOf(t) === 2) toggleQueue(t.id);
    else openDrawer(t.id, { pinned: true, trigger: node });
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

  el.dClose.addEventListener('click', () => closeDrawer(true));

  el.dQueue.addEventListener('click', () => {
    if (S.openId) toggleQueue(S.openId);
  });

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
        try {
          await fetch(CFG.refresh, { method: 'POST' }).catch(() => {});
        } catch (err) { /* fire and forget */ }
        await new Promise((r) => window.setTimeout(r, 1200));
        await pull({});
      }
    } finally {
      el.refresh.removeAttribute('aria-busy');
      el.refresh.textContent = '刷新';
    }
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
  });

  if (mDesktop.addEventListener) mDesktop.addEventListener('change', () => { if (S.data) render(captureRects(), null); });

  /* ------------------------------------------------------------------ init  */

  async function loadQueue() {
    S.queue = readIds(CFG.lsQueue);
    if (DEV) return;
    try {
      const res = await fetch(CFG.queue, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      const ids = Array.isArray(data.queue) ? data.queue : [];
      let changed = false;
      ids.forEach((id) => {
        const n = Number(id);
        if (Number.isFinite(n) && !S.queue.has(n)) { S.queue.add(n); changed = true; }
      });
      if (changed) {
        writeIds(CFG.lsQueue, S.queue);
        if (S.data) { render(captureRects(), null); updateCounts(); }
      }
    } catch (err) { /* offline: localStorage copy stands */ }
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
      const res = await fetch(CFG.health, { cache: 'no-store' });
      if (!res.ok) return;
      const h = await res.json();
      const att = h.attention || {};
      if (att.needed) {
        el.warnchip.hidden = false;
        el.warnchip.title = att.reason || '需要人工排查';
      } else {
        el.warnchip.hidden = true;
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
    el.density.setAttribute('aria-pressed', S.density === 'compact' ? 'true' : 'false');
    el.density.title = '空位显示方式：' + (S.density === 'compact' ? '紧凑（空位折叠）' : '间隙（空位保留）');
  }

  async function init() {
    initFromUrl();
    S.seen = readIds(CFG.lsSeen);
    renderSkeleton();
    await loadQueue();
    await pull({ next: DEV && params.get('phase') === 'next' });
    checkHealth();
    tickTimer = window.setInterval(refreshTimes, 30000);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) window.clearInterval(tickTimer);
    else { refreshTimes(); tickTimer = window.setInterval(refreshTimes, 30000); }
  });

  init();
})();
