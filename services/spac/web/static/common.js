/* ==========================================================================
   梧桐河灌区模型平台（微服务版）—— 公共前端逻辑
   源文件：assets/web/common.js   同步到各服务：python 工具/同步前端资源.py
   无框架、无构建、无 CDN 依赖：断网也能用。
   ========================================================================== */

/* URL 前缀由页面 <meta name="app-base" content="__BASE__"> 决定：
   挂在网关下是 /crop，独立运行是空串。 */
const BASE = (document.querySelector('meta[name="app-base"]') || {}).content || '';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const qs = (name) => new URLSearchParams(location.search).get(name);

/* ---------------- 请求封装 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(BASE + path, options);
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  const data = await res.json();
  /* 会话过期 / 未登录：把登录框拉回来，而不是让用户对着一串 401 猜为什么 */
  if (res.status === 401 && data && data.need_login) {
    if (window.__authNeedLogin) window.__authNeedLogin();
    throw new Error('请先登录');
  }
  if (!res.ok && !(data && data.error)) throw new Error(`HTTP ${res.status}`);
  return data;
}

async function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* Go 风格名，方便服务代码里直接写 postJson */
const postJson = postJSON;

/* ---------------- 提示 ---------------- */
function toast(message, type = '') {
  /* 登录遮罩期间业务接口必然 401 —— 别再叠一堆红条，用户只需要看到登录框 */
  if (type === 'err' && window.__authPending) return;
  let wrap = $('.toast-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 320);
  }, type === 'err' ? 6500 : 3400);
}

/* ---------------- 文本与数字 ---------------- */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(bytes) {
  if (bytes == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + u[i];
}

function fmtNum(v, digits) {
  if (v == null || v === '') return '—';
  if (typeof v !== 'number') return String(v);
  if (digits != null) return v.toFixed(digits);
  if (Number.isInteger(v)) return String(v);
  const a = Math.abs(v);
  const d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 4;
  return v.toFixed(d);
}

function fmtDuration(sec) {
  if (!sec) return '0s';
  if (sec < 60) return sec.toFixed(0) + 's';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  if (m < 60) return `${m}min ${s}s`;
  return `${Math.floor(m / 60)}h ${m % 60}min`;
}

/* ---------------- 表格 ---------------- */
function isNumericCol(rows, idx) {
  let hit = 0, tot = 0;
  for (const r of rows) {
    const v = r[idx];
    if (v === null || v === '') continue;
    tot++;
    if (typeof v === 'number') hit++;
  }
  return tot > 0 && hit / tot > 0.8;
}

function renderTable(container, data, opts = {}) {
  if (typeof container === 'string') container = $(container);
  if (!data || !data.columns || !data.rows || !data.rows.length) {
    container.innerHTML = '<div class="empty-state">暂无数据</div>';
    return;
  }
  const { highlight = [] } = opts;
  const numeric = data.columns.map((_, i) => isNumericCol(data.rows, i));

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  data.columns.forEach((c, i) => {
    const th = document.createElement('th');
    th.textContent = c;
    if (numeric[i]) th.className = 'num';
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  data.rows.forEach((row, ri) => {
    const tr = document.createElement('tr');
    if (highlight.includes(ri)) tr.className = 'highlight';
    row.forEach((v, ci) => {
      const td = document.createElement('td');
      td.textContent = numeric[ci] ? fmtNum(v) : (v == null ? '—' : String(v));
      if (numeric[ci]) td.className = 'num';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  container.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  wrap.appendChild(table);
  container.appendChild(wrap);
}

/* ---------------- 文件 URL ---------------- */
function fileUrl(jobId, rel, download = false) {
  const u = `${BASE}/files/${jobId}/` + rel.split('/').map(encodeURIComponent).join('/');
  return download ? u + '?download=1' : u;
}

/* 表格类产物浏览器无法内联打开（xlsx 的 MIME 一律被交给下载器），
   交给服务端转 HTML：?preview=1。图片 / PDF / TXT 浏览器原生支持，不加参数。 */
const TABLE_EXT = ['xlsx', 'xlsm', 'csv', 'tsv'];
function previewUrl(jobId, rel) {
  const u = fileUrl(jobId, rel);
  const ext = (rel.split('.').pop() || '').toLowerCase();
  return TABLE_EXT.includes(ext) ? u + '?preview=1' : u;
}

function renderFiles(container, jobId, files) {
  if (typeof container === 'string') container = $(container);
  if (!files || !files.length) {
    container.innerHTML = '<div class="empty-state">暂无文件</div>';
    return;
  }
  const list = document.createElement('div');
  list.className = 'filelist';
  files.forEach(f => {
    const name = f.name || f.rel;
    const rel = f.rel || name;
    const item = document.createElement('div');
    item.className = 'fileitem';

    const a = document.createElement('a');
    a.className = 'nm';
    a.textContent = name;
    a.href = fileUrl(jobId, rel, true);
    a.title = '点击下载';
    item.appendChild(a);

    if (f.size) {
      const sz = document.createElement('span');
      sz.className = 'sz';
      sz.textContent = fmtSize(f.size);
      item.appendChild(sz);
    }
    const open = document.createElement('a');
    open.className = 'btn btn-sm';
    open.textContent = '预览';
    open.href = previewUrl(jobId, rel);
    open.target = '_blank';
    open.rel = 'noopener';
    item.appendChild(open);

    list.appendChild(item);
  });
  container.innerHTML = '';
  container.appendChild(list);
}

/* ==========================================================================
   图件网格 —— 「一页看多图」的核心
   每张图 = 一个独立窗口：标题栏（标题 / 分组 / 放大 / 下载）+ 画布
   可选按 group 分组显示小标题；点击画布全屏查看。
   ========================================================================== */
/* 图片 / 文件加载失败时，探一次真实 HTTP 状态，把「为什么」直接写在页面上。
   免费层（Render 等）重启会清空 work/ 里的运行结果、并重置登录会话；两种情况
   的表现都是「缩略图有缓存还能看，点开大图 / 点预览却什么都不显示」，
   不给状态码根本没法判断是没登录还是结果没了。 */
async function failReason(url) {
  let st = 0;
  try {
    const r = await fetch(url, { cache: 'no-store' });
    st = r.status;
  } catch (e) { st = 0; }
  if (st === 401 || st === 403)
    return '登录状态已失效（服务重启会重置会话）—— 请刷新页面重新登录，再重跑一次模拟。';
  if (st === 404)
    return '结果文件已不存在（免费实例休眠/重启/重新部署会清空运行结果）—— 请重新点「开始模拟」。';
  if (st === 200)
    return '文件能取到但浏览器未能解码，可点右上角 ⤓ 下载原图查看。';
  return `加载失败（HTTP ${st || '网络错误'}），可点右上角 ⤓ 下载原图。`;
}

function renderFigures(container, jobId, figures, opts = {}) {
  if (typeof container === 'string') container = $(container);
  const { groupBy = true, wideFirst = false } = opts;

  if (!figures || !figures.length) {
    container.innerHTML = '<div class="empty-state">暂无图件</div>';
    return null;
  }

  const box = document.createElement('div');
  box.className = 'figures';

  const makeFigure = (fig, wide) => {
    const d = document.createElement('div');
    d.className = 'figure' + (wide ? ' wide' : '');

    // --- 标题栏 ---
    const head = document.createElement('div');
    head.className = 'fig-head';

    const t = document.createElement('span');
    t.className = 't';
    t.textContent = fig.title;
    t.title = fig.title;
    head.appendChild(t);

    if (fig.group) {
      const g = document.createElement('span');
      g.className = 'grp';
      g.textContent = fig.group;
      head.appendChild(g);
    }

    const acts = document.createElement('span');
    acts.className = 'acts';

    const zoom = document.createElement('button');
    zoom.className = 'ico-btn';
    zoom.textContent = '⛶';
    zoom.title = '全屏查看';
    acts.appendChild(zoom);

    const dl = document.createElement('a');
    dl.className = 'ico-btn';
    dl.textContent = '⤓';
    dl.title = '下载原图 PNG';
    dl.href = fileUrl(jobId, fig.rel, true);
    acts.appendChild(dl);

    head.appendChild(acts);
    d.appendChild(head);

    // --- 画布 ---
    const body = document.createElement('div');
    body.className = 'fig-body';

    const ph = document.createElement('span');
    ph.className = 'loading';
    ph.textContent = '加载中…';
    body.appendChild(ph);

    /* 注意：这里刻意【不用】 loading="lazy"，也【不用】 display:none 隐藏。
       懒加载靠「元素进入视口」触发请求，而 display:none 的元素没有布局盒子、
       不在视口内 —— 请求永远不发、load 永远不触发、图片永远不显示（点开全屏
       才看得到，因为那条路径直接赋 src）。该死锁在旧版 Chromium 内核
       （部分国产/内置浏览器、手机浏览器）上稳定复现。
       本模块图件总量仅数张、单张 100–250 KB，直接加载更稳。 */
    const img = document.createElement('img');
    img.alt = fig.title;
    img.decoding = 'async';
    img.className = 'fig-img';
    const reveal = () => { img.classList.add('ready'); if (ph.parentNode) ph.remove(); };
    img.addEventListener('load', reveal);
    img.addEventListener('error', () => {
      ph.textContent = '图片加载失败，正在核查原因…';
      failReason(img.src).then(msg => { ph.textContent = msg; });
    });
    img.src = fileUrl(jobId, fig.rel);
    // 兜底：图片直接从缓存/已完成状态命中时 load 可能早已错过，主动补判一次。
    // （CSS 已让 img 默认可见，这里只是把「加载中…」占位收掉，双保险。）
    requestAnimationFrame(() => { if (img.complete && img.naturalWidth > 0) reveal(); });
    body.appendChild(img);

    const open = () => lightbox(img.src, fig.title, jobId, fig.rel);
    body.addEventListener('click', open);
    zoom.addEventListener('click', e => { e.stopPropagation(); open(); });
    d.appendChild(body);

    return d;
  };

  if (groupBy) {
    const groups = new Map();
    figures.forEach(f => {
      const g = f.group || '结果';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(f);
    });
    groups.forEach((items, name) => {
      const title = document.createElement('div');
      title.className = 'fig-group-title';
      title.textContent = `${name}（${items.length} 张）`;
      box.appendChild(title);
      items.forEach(f => box.appendChild(makeFigure(f, wideFirst && items.length === 1)));
    });
  } else {
    figures.forEach(f => box.appendChild(makeFigure(f, false)));
  }

  container.innerHTML = '';
  container.appendChild(box);
  return box;
}

function lightbox(src, alt, jobId, rel) {
  const mask = document.createElement('div');
  mask.className = 'lightbox';

  const img = document.createElement('img');
  img.src = src;
  img.alt = alt || '';
  mask.appendChild(img);

  /* 大图打不开时不能只留一块黑底（用户只会说「点开没图」），
     把真实 HTTP 状态翻成人话贴在上面。 */
  img.addEventListener('error', () => {
    img.remove();
    const tip = document.createElement('div');
    tip.className = 'lb-err';
    tip.textContent = '原图加载失败，正在核查原因…';
    mask.appendChild(tip);
    failReason(src).then(t => { tip.textContent = t; });
  });

  if (alt) {
    const cap = document.createElement('div');
    cap.className = 'cap';
    cap.textContent = alt;
    mask.appendChild(cap);
  }

  const close = () => { mask.remove(); document.removeEventListener('keydown', esc); };
  const esc = e => { if (e.key === 'Escape') close(); };
  mask.addEventListener('click', close);
  document.addEventListener('keydown', esc);
  document.body.appendChild(mask);
}

/* ---------------- 日志区 ---------------- */
function createLogView(el) {
  if (typeof el === 'string') el = $(el);
  el.innerHTML = '<span class="empty">等待任务开始…</span>';
  let hasContent = false;
  return {
    append(text) {
      if (!text) return;
      if (!hasContent) { el.innerHTML = ''; hasContent = true; }
      el.appendChild(document.createTextNode(text));
      el.scrollTop = el.scrollHeight;
    },
    reset(msg) {
      el.innerHTML = '';
      hasContent = false;
      if (msg) this.append(msg);
    },
    clear() { this.reset(); },
    isEmpty() { return !hasContent; },
  };
}

/* ---------------- 任务轮询 ---------------- */
function statusPill(status) {
  const MAP = {
    queued:  ['warn', '排队中'],
    running: ['run',  '运行中'],
    done:    ['ok',   '已完成'],
    failed:  ['err',  '失败'],
  };
  const [cls, text] = MAP[status] || ['', status];
  const span = document.createElement('span');
  span.className = 'pill ' + cls;
  if (status === 'running' || status === 'queued') {
    const sp = document.createElement('span');
    sp.className = 'spin';
    span.appendChild(sp);
  }
  span.appendChild(document.createTextNode(text));
  return span;
}

function watchJob(jobId, { onLog, onState, onFinish, interval = 900 } = {}) {
  let offset = 0;
  let stopped = false;
  let errors = 0;

  async function tick() {
    if (stopped) return;
    try {
      const s = await api(`/api/job/${jobId}?offset=${offset}`);
      errors = 0;
      if (s.log) { offset = s.log_offset; if (onLog) onLog(s.log); }
      if (onState) onState(s);
      if (s.status === 'done' || s.status === 'failed') {
        stopped = true;
        if (onFinish) onFinish(s);
        return;
      }
    } catch (e) {
      errors++;
      if (errors > 8) {
        stopped = true;
        if (onFinish) onFinish({ status: 'failed', error: String(e) });
        return;
      }
    }
    setTimeout(tick, interval);
  }
  tick();
  return { stop() { stopped = true; } };
}

/* ---------------- 小工具 ---------------- */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function kpiCard(k, v, unit, kind = '') {
  const d = el('div', 'kpi ' + kind);
  d.appendChild(el('div', 'k', k));
  const val = el('div', 'v', v == null ? '—' : String(v));
  if (unit) {
    const s = document.createElement('small');
    s.textContent = unit;
    val.appendChild(s);
  }
  d.appendChild(val);
  return d;
}

function renderKpis(container, items) {
  if (typeof container === 'string') container = $(container);
  container.innerHTML = '';
  if (!items || !items.length) return;
  items.forEach(it => container.appendChild(kpiCard(it.k, it.v, it.unit, it.kind || '')));
  container.classList.remove('hidden');
}
