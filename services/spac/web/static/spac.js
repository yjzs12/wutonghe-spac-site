/* 作物生长模型模块页 —— 输入 / 运行 / 结果工作台 */
/* 依赖 common.js（BASE、api、postJSON、renderFigures、renderTable、watchJob ...） */

/* 独立网站模式（BASE 为空 = 没挂在网关子路径下）：本站只有这一个模块，
   收起跨模块导航，避免出现指向不存在的首页/兄弟模块的链接。 */
if (BASE === '') {
  const links = document.querySelector('.nav-links');
  if (links) links.style.display = 'none';
  const brand = document.querySelector('.brand');
  if (brand) brand.innerHTML = '<span class="dot">梧</span>梧桐河灌区 · 作物生长模型';
  document.title = '基于 SPAC 的典型作物生长模型';
}

const logView = createLogView($('#log'));
const taskPill = $('#taskPill');
const elapsedEl = $('#elapsed');

let currentJobId = null;
let watcher = null;
let running = false;
let wideMode = false;
let lastResult = null;

/* ---------------- 状态 ---------------- */
function setPill(kind, text) {
  taskPill.className = 'pill ' + kind;
  taskPill.innerHTML = '';
  if (kind === 'run') {
    const sp = document.createElement('span');
    sp.className = 'spin';
    taskPill.appendChild(sp);
  }
  taskPill.appendChild(document.createTextNode(text));
}

/* ---------------- 年份 ---------------- */
function fillYears(years, period) {
  const chips = $('#yearChips');
  const sel = $('#yearSelect');
  chips.innerHTML = '';
  sel.innerHTML = '';
  if (period) $('#periodHint').textContent = period;

  if (!years.length) {
    chips.innerHTML = '<span class="muted">暂无气象数据，请先导入</span>';
    sel.innerHTML = '<option value="">（无数据，请先导入气象数据）</option>';
    $('#runBtn').disabled = true;
    return;
  }
  $('#runBtn').disabled = false;
  years.forEach(y => {
    const c = el('span', 'chip', String(y));
    c.addEventListener('click', () => {
      $$('#yearChips .chip').forEach(x => x.classList.remove('on'));
      c.classList.add('on');
      sel.value = String(y);
    });
    chips.appendChild(c);

    const o = document.createElement('option');
    o.value = String(y);
    o.textContent = y + ' 年';
    sel.appendChild(o);
  });
  const last = String(years[years.length - 1]);
  sel.value = last;
  $$('#yearChips .chip').forEach(x => x.classList.toggle('on', x.textContent === last));
}

async function loadYears() {
  try {
    const r = await api('/api/years');
    if (!r.ok) { $('#yearChips').innerHTML = `<span class="muted">读取失败：${escapeHtml(r.error)}</span>`; return; }
    fillYears(r.years, r.period);
  } catch (e) {
    $('#yearChips').innerHTML = `<span class="muted">读取失败：${escapeHtml(e.message)}</span>`;
  }
}

/* ---------------- 导入气象数据 ---------------- */
async function importWeather(overrideEra5 = false) {
  const input = $('#weatherFile');
  const box = $('#importResult');
  if (!input.files.length) { toast('请先选择气象 CSV 文件', 'err'); return; }

  const fd = new FormData();
  fd.append('file', input.files[0]);
  fd.append('override_era5', overrideEra5 ? 'true' : 'false');

  box.innerHTML = '<div class="notice">导入中…</div>';
  $('#importWeather').disabled = true;
  try {
    const res = await fetch(BASE + '/api/weather', { method: 'POST', body: fd });
    const data = await res.json();

    if (res.status === 409 && data.need_confirm) {
      box.innerHTML = '';
      const n = el('div', 'notice warn', data.error);
      const btn = el('button', 'btn btn-sm btn-primary', '确认覆盖并导入');
      btn.style.marginTop = '8px';
      btn.addEventListener('click', () => importWeather(true));
      box.append(n, btn);
      return;
    }
    if (!data.ok) {
      box.innerHTML = `<div class="notice err">${escapeHtml(data.error || '导入失败')}</div>`;
      toast('导入失败', 'err');
      return;
    }

    const days = data.missing_days > 0
      ? `<span class="pill warn">缺 ${data.missing_days} 天</span>`
      : '<span class="pill ok">模拟期数据完整</span>';
    box.innerHTML = `<div class="notice ok">
        已导入 <b>${data.year}</b> 年气象数据（模拟期 ${data.n_days}/${data.expected} 天）${days}<br>
        总降雨 ${fmtNum(data.prcp)} mm ｜ 日均太阳辐射 ${fmtNum(data.rs_mean)} MJ/m² ｜
        平均气温 ${fmtNum((data.tmin_mean + data.tmax_mean) / 2)} ℃
        ${data.replaced_era5 ? '<br>（已替换同年的 ERA5 数据）' : ''}
      </div>`;
    toast(`已导入 ${data.year} 年气象数据`, 'ok');
    await loadYears();
    $('#yearSelect').value = String(data.year);
  } catch (e) {
    box.innerHTML = `<div class="notice err">导入失败：${escapeHtml(e.message)}</div>`;
    toast('导入失败', 'err');
  } finally {
    $('#importWeather').disabled = false;
  }
}

/* ---------------- 运行 ---------------- */
async function startRun(year, crop) {
  if (running) return;
  year = year || $('#yearSelect').value;
  crop = crop || (($('input[name=crop]:checked') || {}).value || 'both');
  if (!year) { toast('请先选择模拟年份', 'err'); return; }

  running = true;
  $('#runBtn').disabled = true;
  $('#demoBtn').disabled = true;
  $('#runBtn').textContent = '模拟中…';
  logView.reset();
  clearResults();
  setPill('run', '运行中');
  elapsedEl.textContent = '';

  try {
    const r = await postJSON('/api/run', { year: Number(year), crop });
    if (!r.ok) throw new Error(r.error || '启动失败');
    currentJobId = r.job_id;
    $('#downloadLog').href = BASE + `/api/job/${r.job_id}/log.txt`;
    history.replaceState(null, '', location.pathname + `?job=${r.job_id}`);
    watch(r.job_id);
  } catch (e) {
    logView.append('启动失败：' + e.message + '\n');
    toast('启动失败：' + e.message, 'err');
    finishUI();
  }
}

function watch(jobId) {
  if (watcher) watcher.stop();
  watcher = watchJob(jobId, {
    onLog: t => logView.append(t),
    onState: s => {
      if (s.status === 'running') {
        setPill('run', s.progress || '运行中');
        elapsedEl.textContent = '已用时 ' + fmtDuration(s.elapsed);
      } else if (s.status === 'queued') {
        setPill('warn', '排队中');
      }
    },
    onFinish: s => {
      if (s.status === 'done' && s.result) {
        logView.append(`\n[完成] ${s.result.n_scenarios} 个方案，${s.result.figures.length} 张图件。\n`);
        setPill('ok', '已完成');
        elapsedEl.textContent = '耗时 ' + fmtDuration(s.elapsed);
        toast('模拟完成', 'ok');
        renderResult(jobId, s.result);
      } else {
        logView.append('\n[失败] ' + (s.error || '未知错误') + '\n');
        setPill('err', '失败');
        toast('模拟失败，详见日志', 'err');
      }
      finishUI();
    },
  });
}

function finishUI() {
  running = false;
  $('#runBtn').disabled = false;
  $('#runBtn').textContent = '开始模拟';
  $('#demoBtn').disabled = false;
}

/* ---------------- 结果渲染 ---------------- */
function clearResults() {
  $('#kpiRow').classList.add('hidden');
  $('#kpiRow').innerHTML = '';
  $('#figureBody').innerHTML = '<div class="empty-state">运行中…</div>';
  $('#optimalBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#summaryBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#waterReqBody').innerHTML = '<div class="empty-state">暂无</div>';
  if ($('#waterReqNote')) $('#waterReqNote').innerHTML = '';
  $('#fileBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#toggleWide').disabled = true;
}

function renderResult(jobId, res) {
  lastResult = res;
  const currentRice = res.model_revision === '2026-10-22-rice-v4';
  const standardRice = currentRice || res.model_revision === '2026-10-22-rice-v3';
  const netWater = ['net_with_puddling_v2', 'net_profile_puddling_v3', 'net_puddling_balance_v4'].includes(res.irrigation_basis);

  /* --- KPI 条：把最优灌溉制度摊成指标卡 --- */
  const kpis = [];
  if (res.kpi && res.kpi.length) {
    res.kpi.forEach(k => {
      kpis.push({ k: `${k['作物']} · 最优${netWater ? '净' : ''}灌水量`, v: fmtNum(k['累计灌溉量(mm)'], 1), unit: 'mm', kind: 'accent' });
      kpis.push({ k: `${k['作物']} · 最优方案`, v: k['最优方案'], unit: '' });
      /* 展示相对产量；天气充足时可以出现平台，不预设最佳档位。 */
      kpis.push({ k: `${k['作物']} · 达峰值产量`, v: fmtNum(k['产量达最大百分比(%)'], 1), unit: '%' });
      const yieldLabel = standardRice && k['作物'] === '水稻' ? '产量（14.5%水分）' : '产量';
      kpis.push({ k: `${k['作物']} · ${yieldLabel}`, v: fmtNum(k['产量(t/ha)'], 2), unit: 't/ha' });
      kpis.push({ k: `${k['作物']} · 灌溉 WUE`, v: fmtNum(k['灌溉WUE(kg/m³)'], 3), unit: 'kg/m³' });
    });
  }
  kpis.push({ k: '方案总数', v: res.n_scenarios, unit: '个' });
  kpis.push({ k: '图件', v: (res.figures || []).length, unit: '张' });
  /* 作物需水量：全生育期 ETc 与 Kc（正常灌溉对照口径） */
  (res.water_req && res.water_req.crops || []).forEach(c => {
    kpis.push({ k: `${c.name} · 全生育期需水量`, v: fmtNum(c.total_etc, 1), unit: 'mm' });
    kpis.push({ k: `${c.name} · 全期作物系数 Kc`, v: fmtNum(c.kc, 2), unit: '' });
  });
  renderKpis('#kpiRow', kpis);

  /* --- 图件：一页铺开所有窗口 --- */
  renderFigures($('#figureBody'), jobId, res.figures, { groupBy: true });
  $('#figureSub').textContent = `${res.year} 年 · ${res.crop_names.join('+')} · `
    + `${(res.figures || []).length} 张图（点图全屏，右上角下载原图）`;
  $('#toggleWide').disabled = !(res.figures && res.figures.length);
  applyWide();

  /* --- 最优制度表 --- */
  if (res.optimal && res.optimal.rows.length) {
    renderTable($('#optimalBody'), res.optimal);
  } else {
    $('#optimalBody').innerHTML = '<div class="empty-state">无优化方案结果</div>';
  }

  /* --- 汇总表（高亮最优方案行） --- */
  const hl = [];
  if (res.optimal) {
    const oi = res.optimal.columns.indexOf('作物');
    const op = res.optimal.columns.indexOf('最优方案');
    const si = res.summary.columns.indexOf('作物');
    const sp = res.summary.columns.indexOf('灌溉方案');
    res.optimal.rows.forEach(o => {
      const idx = res.summary.rows.findIndex(r => r[si] === o[oi] && r[sp] === o[op]);
      if (idx >= 0) hl.push(idx);
    });
  }
  renderTable($('#summaryBody'), res.summary, { highlight: hl });
  $('#summarySub').textContent = `${res.year} 年 · ${res.summary.rows.length} 个方案（绿色行为最优）`
    + (netWater ? ' · 主统计为田间净量，毛量另列' : ' · 历史任务口径，不能与新版直接比较')
    + (currentRice ? ` · ${res.model_notes || ''}` : ' · 历史模型版本，泡田与关键期约束口径与当前版本不同');

  /* --- 作物需水量（月表 + 点月份展开逐日明细） --- */
  const wrBox = $('#waterReqBody');
  const wr = res.water_req;
  if (wr && wr.crops && wr.crops.length) {
    wrBox.innerHTML = '';
    wr.crops.forEach(c => {
      const title = el('div', 'fig-group-title',
        `${c.name}（生育期 ${c.period} ｜ 全期 ETc ${fmtNum(c.total_etc, 1)} mm ｜ `
        + `Kc ${fmtNum(c.kc, 2)} ｜ 降雨 ${fmtNum(c.total_prcp, 1)} mm ｜ `
        + `${netWater ? '净灌溉' : '灌溉'} ${fmtNum(c.total_irr, 1)} mm）`);
      wrBox.appendChild(title);
      const box = el('div');
      wrBox.appendChild(box);
      renderWaterReq(box, c);
    });
    $('#waterReqSub').textContent =
      `${wr.year} 年 · ${wr.basis} · 月表默认折叠，点月份行展开该月逐日`;
    renderPhenology($('#waterReqNote'), wr.crops);
  } else {
    wrBox.innerHTML = '<div class="empty-state">暂无</div>';
    if ($('#waterReqNote')) $('#waterReqNote').innerHTML = '';
  }

  /* --- 文件 --- */
  renderFiles($('#fileBody'), jobId, res.files);
}

/* ---------------- 物候口径说明：移栽前的 Kc 不是作物系数 ---------------- */
/* 模拟期固定自 04-01 起，但水稻 05-15 才移栽、玉米 04-25 才播种。
   这段 CC=0、不灌溉，ETc 只剩裸土蒸发，Kc 会掉到 0.1 以下 —— 必须就地讲明，
   否则「全期 Kc」会被当成作物系数引用。 */
function renderPhenology(box, crops) {
  if (!box) return;
  const items = (crops || []).filter(c => c.phenology);
  if (!items.length) { box.innerHTML = ''; return; }
  const line = c => {
    const p = c.phenology;
    const start = (c.period || '').split(' ~ ')[0];
    return `<div style="margin-bottom:7px">`
      + `<span style="color:var(--text-2);font-weight:500">${c.name}</span>　`
      + `模拟期自 ${start} 起，${p.planting} ${p.plant_term}、${p.emergence} ${p.emergence_term || '出苗'}。`
      + `此前 <span style="color:var(--text-2);font-weight:500">${p.pre_days} 天</span>无作物`
      + `（冠层覆盖度0；插前净补水${fmtNum(p.pre_irrigation || 0, 1)}mm，水稻含泡田），蒸发统计：`
      + `ET0 ${fmtNum(p.pre_et0, 1)} mm / ETc ${fmtNum(p.pre_etc, 1)} mm / `
      + `<span style="color:var(--text-2);font-weight:500">Kc ${fmtNum(p.pre_kc, 2)}</span>`
      + `，该段 Kc 不代表作物系数。`
      + `仅统计${p.plant_term}后 ${p.crop_days} 天：`
      + `ET0 ${fmtNum(p.crop_et0, 1)} mm / ETc ${fmtNum(p.crop_etc, 1)} mm / `
      + `<span style="color:var(--text-2);font-weight:500">Kc ${fmtNum(p.crop_kc, 2)}</span>。</div>`;
  };
  box.innerHTML =
    `<div style="margin-bottom:4px;color:var(--text-2);font-weight:500">口径说明</div>`
    + items.map(line).join('');
}

/* ---------------- 作物需水量：月表 + 可展开逐日 ---------------- */
/* 月行默认折叠，点击月份行才展开该月的逐日 ET0/ETc/Kc/降雨/灌溉，
   这样既能看日步长数据，又不会一上来就把整个生育期铺满页面。 */
function renderWaterReq(box, c) {
  const mcols = c.monthly.columns;
  const totalRow = c.monthly.rows[c.monthly.rows.length - 1];
  const monthRows = c.monthly.rows.slice(0, -1);
  const byMonth = (c.daily && c.daily.by_month) || {};

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  mcols.forEach(col => {
    const th = document.createElement('th');
    th.textContent = col;
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');

  const putCells = (tr, arr) => {
    arr.forEach(v => {
      const td = document.createElement('td');
      if (v == null) {
        td.textContent = '—';
      } else if (typeof v === 'number') {
        td.textContent = fmtNum(v);
        td.className = 'num';
      } else {
        td.textContent = String(v);
      }
      tr.appendChild(td);
    });
  };

  monthRows.forEach(row => {
    const days = byMonth[String(parseInt(row[0], 10))];
    const tr = document.createElement('tr');
    putCells(tr, row);
    tbody.appendChild(tr);
    if (!days || !days.length) return;

    const caret = document.createElement('span');
    caret.className = 'wr-caret';
    caret.textContent = '▸ ';
    const firstTd = tr.firstChild;
    firstTd.insertBefore(caret, firstTd.firstChild);

    const dayTrs = days.map(dr => {
      const dtr = document.createElement('tr');
      dtr.className = 'wr-day';
      dtr.style.display = 'none';
      putCells(dtr, dr);
      dtr.firstChild.style.paddingLeft = '1.8rem';
      tbody.appendChild(dtr);
      return dtr;
    });

    tr.classList.add('wr-month');
    tr.title = '点击展开该月逐日明细';
    tr.addEventListener('click', () => {
      const open = tr.classList.toggle('wr-open');
      dayTrs.forEach(d => { d.style.display = open ? '' : 'none'; });
      caret.textContent = open ? '▾ ' : '▸ ';
    });
  });

  const ttr = document.createElement('tr');
  ttr.className = 'highlight';
  putCells(ttr, totalRow);
  tbody.appendChild(ttr);

  table.appendChild(tbody);
  box.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  wrap.appendChild(table);
  box.appendChild(wrap);

  ensureWrStyle();
}

/* 展开区样式：一次性注入 <style>，颜色跟随浅色/深色主题变量 */
function ensureWrStyle() {
  if (document.getElementById('wrStyle')) return;
  const st = document.createElement('style');
  st.id = 'wrStyle';
  st.textContent = `
tr.wr-month { cursor: pointer; }
tr.wr-month:hover { background: var(--bg-sunken); }
tr.wr-open { background: color-mix(in srgb, var(--accent-soft) 70%, transparent); }
tr.wr-day td { font-size: .86em; color: var(--text-2); background: var(--bg-sunken); }
.wr-caret { color: var(--accent); font-weight: 600; }
`;
  document.head.appendChild(st);
}

function applyWide() {
  const box = $('#figureBody .figures');
  if (!box) return;
  box.classList.toggle('wide', wideMode);
  $$('#figureBody .figure').forEach(f => f.classList.toggle('wide', wideMode));
  $('#toggleWide').textContent = wideMode ? '双列并排' : '单列铺宽';
}

/* ---------------- 恢复历史任务 ---------------- */
async function restoreJob(jobId) {
  currentJobId = jobId;
  $('#downloadLog').href = BASE + `/api/job/${jobId}/log.txt`;
  let s;
  try {
    s = await api(`/api/job/${jobId}`);
  } catch (e) {
    /* 免费实例重启 / 重新部署后，job 元数据与结果文件一起没了。
       原来这里会抛未捕获异常，页面停在"运行中…"，看着像卡死。 */
    clearResults();
    $('#figureBody').innerHTML =
      '<div class="empty-state">这次运行的结果已不存在（实例休眠重启或重新部署会清空），请重新点「开始模拟」。</div>';
    setPill('err', '结果已失效');
    toast('这次运行的结果已不存在（实例休眠重启或重新部署会清空），请重新跑一次模拟。', 'err');
    return;
  }
  logView.reset();
  if (s.log) logView.append(s.log);
  if (s.status === 'running' || s.status === 'queued') {
    running = true;
    $('#runBtn').disabled = true;
    $('#runBtn').textContent = '模拟中…';
    setPill('run', s.progress || '运行中');
    watch(jobId);
  } else if (s.status === 'done' && s.result) {
    setPill('ok', '已完成');
    elapsedEl.textContent = '耗时 ' + fmtDuration(s.elapsed);
    renderResult(jobId, s.result);
  } else if (s.status === 'failed') {
    setPill('err', '失败');
  }
}

/* ---------------- 事件 ---------------- */
$('#refreshYears').addEventListener('click', loadYears);
$('#importWeather').addEventListener('click', () => importWeather(false));
$('#runBtn').addEventListener('click', () => startRun());
$('#demoBtn').addEventListener('click', () => {
  const years = $$('#yearSelect option').map(o => o.value).filter(Boolean);
  const y = years.includes('2024') ? '2024' : years[years.length - 1];
  if (!y) { toast('没有可用年份，请先导入气象数据', 'err'); return; }
  $('#yearSelect').value = y;
  const rb = $('input[name=crop][value=both]');
  if (rb) rb.checked = true;
  startRun(y, 'both');
});
$('#toggleWide').addEventListener('click', () => {
  wideMode = !wideMode;
  applyWide();
});

loadYears().then(() => {
  const jid = qs('job');
  if (jid) restoreJob(jid);
});
