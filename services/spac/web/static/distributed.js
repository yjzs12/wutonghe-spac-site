/* Distributed irrigation workbench — reads precomputed per-grid results only. */
(() => {
  'use strict';
  let geo = null, features = [], paths = [], scale = 1, offsetX = 0, offsetY = 0;
  let year = null, mapVals = {}, focused = null, years = [], requestId = 0, detailId = 0;
  const canvas = $('#distMap'), ctx = canvas.getContext('2d');
  const colors = {rice: '#59bd91', corn: '#d1b962', other: '#71828b'};
  const cropNames = {rice: '水稻', corn: '玉米', other: '其他', unknown: '未分类'};
  const schemeNames = {CF:'常规淹水', SWI:'浅湿灌溉', AWD:'干湿交替', CI:'控制灌溉', RAIN:'雨养'};
  const number = (v, d = 2) => (v == null || !Number.isFinite(Number(v)) ? '—'
    : Number(v).toLocaleString('zh-CN', {maximumFractionDigits: d}));
  const notice = (msg) => { $('#distNotice').textContent = msg; };

  async function request(path) {
    const response = await fetch(BASE + path);
    const data = await response.json();
    if (response.status === 401 && window.__authNeedLogin) window.__authNeedLogin();
    if (!response.ok || data.ok === false) {
      throw new Error(typeof data.detail === 'string' ? data.detail : data.error || `请求失败 (${response.status})`);
    }
    return data;
  }
  const guarded = (fn) => async () => {
    try { await fn(); } catch (e) { notice(e.message); toast(e.message, 'err'); }
  };

  /* ---------------- 年份与汇总 ---------------- */
  async function loadYears() {
    const data = await request('/api/distributed/fixed/years');
    years = data.years || [];
    const select = $('#distYear');
    select.replaceChildren();
    years.forEach(y => select.add(new Option(`${y} 年`, String(y))));
    if (!years.length) {
      notice('结果准备中，请稍后刷新。');
      $('#distStatus').textContent = '无数据';
      return false;
    }
    year = (data.ready_years || [])[0] || years[0];
    select.value = String(year);
    $('#distStatus').textContent = '就绪';
    return true;
  }

  async function loadYear() {
    const token = ++requestId, requestedYear = year;
    const [summary, map] = await Promise.all([
      request(`/api/distributed/fixed/summary?year=${requestedYear}`),
      request(`/api/distributed/fixed/map?year=${requestedYear}`)
    ]);
    if (token !== requestId || year !== requestedYear) return;
    if (summary.snapshot_id !== map.snapshot_id) throw new Error('结果正在更新，请刷新');
    $('#distSumSub').textContent = `${year} 年 · ${summary.complete ? '全部可计算区域' : '仅已完成部分'}`;
    $('#distStatus').textContent = summary.complete ? '已完成' : '计算中';
    $('#distBasis').textContent = '2023—2026年 · ERA5-Land 12格点就近匹配 · OpenLandMap土壤';
    const rows = summary.schemes.map(r => `<tr><td>${cropNames[r.crop]}</td><td>${escapeHtml(r.label || schemeNames[r.scheme] || r.scheme)}</td>
      <td>${number(r.n_grids,0)}</td><td>${number(r.area_ha)}</td><td>${number(r.weighted_irrigation_mm,1)}</td>
      <td>${number(r.net_water_m3,0)}</td><td>${number(r.production_t,1)}</td><td>${number(r.weighted_yield_t_ha,3)}</td></tr>`).join('');
    const coverage = Object.entries(summary.coverage).map(([c,v]) => {
      const area = (summary.schemes.find(r => r.crop === c) || {}).area_ha || 0;
      return `${cropNames[c]}：已算${number(area)} ha（可计算面积的${number(100*area/v.eligible_area_ha,1)}%，种植面积的${number(100*area/v.expected_area_ha,1)}%），缺输入${number(v.expected_area_ha-v.eligible_area_ha)} ha`;
    }).join('；');
    $('#distSummary').innerHTML = `<div class="dist-table-wrap"><table class="dist-result-table"><thead><tr>
      <th>作物</th><th>制度</th><th>格数</th><th>已算面积 ha</th><th>净灌水 mm</th><th>已算净水量 m³</th><th>已算总产 t</th><th>单产 t/ha</th>
      </tr></thead><tbody>${rows}</tbody></table></div><p class="hint">${escapeHtml(coverage)}</p>
      <a class="btn btn-sm" href="${BASE}/api/distributed/fixed/summary.csv?year=${year}">下载汇总</a>`;
    geo = map.geo; mapVals = map.results || {};
    features = geo.features.filter(f =>
      ['rice','corn','other'].includes(f.properties.dominant_crop) && hasResult(f));
    rebuildPaths(); fitMap();
    notice(`${year} 年 · 已完成 ${number(summary.success,0)} / ${number(summary.eligible,0)} 个格网作物记录${summary.failed ? ` · 失败${summary.failed}` : ''}`);
  }

  function project(c) { return [c[0] * Math.cos(47.3 * Math.PI / 180), -c[1]]; }
  function polygons(f) {
    return f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates;
  }
  let bounds;
  function rebuildPaths() {
    const xs = [], ys = [];
    for (const f of features) for (const p of polygons(f)) for (const ring of p) for (const c of ring) {
      const [x, y] = project(c); xs.push(x); ys.push(y);
    }
    bounds = xs.length ? [xs.reduce((a, b) => Math.min(a, b), Infinity), ys.reduce((a, b) => Math.min(a, b), Infinity),
                         xs.reduce((a, b) => Math.max(a, b), -Infinity), ys.reduce((a, b) => Math.max(a, b), -Infinity)] : null;
    paths = features.map(feature => {
      const path = new Path2D();
      polygons(feature).forEach(poly => poly.forEach(ring => {
        ring.forEach((c, i) => { const p = project(c); if (i === 0) path.moveTo(...p); else path.lineTo(...p); });
        path.closePath();
      }));
      return {feature, path};
    });
  }

  function hasResult(f, crop) {
    const crops = mapVals[f.id] || [];
    return crop ? crops.includes(crop) : crops.some(c => c === 'rice' || c === 'corn');
  }

  function visible(f) {
    const mode = $('#distFilter').value;
    return hasResult(f, mode === 'all' ? null : mode);
  }

  function draw() {
    const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
    if (!geo) return;
    ctx.translate(offsetX, offsetY); ctx.scale(scale, scale);
    const displayed = paths.filter(p => visible(p.feature));
    displayed.forEach(({feature: f, path}) => {
      ctx.fillStyle = colors[f.properties.dominant_crop];
      ctx.fill(path, 'evenodd');
      ctx.lineWidth = 0.3 / scale; ctx.strokeStyle = '#0c1e24'; ctx.stroke(path);
      if (f.id === focused) { ctx.strokeStyle = '#c65b00'; ctx.lineWidth = 2.8 / scale; ctx.stroke(path); }
    });
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    const cropsShown = new Set(displayed.map(p => p.feature.properties.dominant_crop));
    const legend = [['rice', '水稻'], ['corn', '玉米'], ['other', '其他']];
    $('#distLegend').innerHTML = legend.filter(([crop]) => cropsShown.has(crop)).map(([crop, n]) =>
      `<span><i class="dist-swatch" style="background:${colors[crop]}"></i>${escapeHtml(n)}</span>`).join('');
    $('#distMapStats').textContent =
      `显示 ${displayed.length.toLocaleString()} 格 · 格网面积 ${number(displayed.reduce((s, p) => s + p.feature.properties.area_ha, 0))} ha`;
  }

  function fitMap() {
    if (!bounds) { draw(); return; }
    const padding = 28, width = canvas.clientWidth;
    if (width <= 2 * padding) return;
    const height = Math.ceil((width - 2 * padding) * (bounds[3] - bounds[1]) / (bounds[2] - bounds[0]) + 2 * padding);
    const heightStyle = `${height}px`;
    if (canvas.parentElement.style.height !== heightStyle) canvas.parentElement.style.height = heightStyle;
    scale = Math.min((width - 2 * padding) / (bounds[2] - bounds[0]),
                     (canvas.clientHeight - 2 * padding) / (bounds[3] - bounds[1]));
    offsetX = (canvas.clientWidth - scale * (bounds[0] + bounds[2])) / 2;
    offsetY = (canvas.clientHeight - scale * (bounds[1] + bounds[3])) / 2;
    draw();
  }

  let press = null;
  canvas.addEventListener('pointerdown', e => { press = {x:e.clientX, y:e.clientY}; });
  canvas.addEventListener('pointercancel', () => { press = null; });
  canvas.addEventListener('click', e => {
    const dragged = press && Math.hypot(e.clientX - press.x, e.clientY - press.y) > 6;
    press = null;
    if (dragged || e.detail > 1) return;
    const x = (e.offsetX - offsetX) / scale, y = (e.offsetY - offsetY) / scale;
    const hit = paths.find(p => visible(p.feature) && ctx.isPointInPath(p.path, x, y, 'evenodd'));
    if (hit) guarded(() => showDetail(hit.feature.id))();
  });
  new ResizeObserver(() => { if (geo) fitMap(); }).observe(canvas.parentElement);

  /* ---------------- 格网详情 ---------------- */
  let clearDailyTable = () => {};
  function renderDailyTable(container, table) {
    clearDailyTable();
    renderTable(container, table);
    const viewport = $('.table-wrap', container);
    if (!viewport) return;
    viewport.classList.add('dist-daily-viewport');
    viewport.tabIndex = 0;
    viewport.setAttribute('aria-label', '逐日数据，可左右滚动');
    const rail = document.createElement('div'), track = document.createElement('div');
    rail.className = 'dist-daily-scroll'; rail.tabIndex = 0;
    rail.setAttribute('aria-label', '逐日数据横向滚动条');
    rail.appendChild(track); container.prepend(rail);
    rail.addEventListener('scroll', () => { viewport.scrollLeft = rail.scrollLeft; });
    viewport.addEventListener('scroll', () => { rail.scrollLeft = viewport.scrollLeft; });
    const resize = new ResizeObserver(() => {
      track.style.width = `${viewport.scrollWidth}px`;
      rail.hidden = viewport.scrollWidth <= viewport.clientWidth;
      rail.scrollLeft = viewport.scrollLeft;
    });
    resize.observe(viewport); resize.observe($('table', viewport));
    clearDailyTable = () => { resize.disconnect(); clearDailyTable = () => {}; };
    let drag = null;
    viewport.addEventListener('pointerdown', e => {
      if (e.pointerType !== 'mouse' || e.button !== 0) return;
      const rect = viewport.getBoundingClientRect();
      if (e.clientX >= rect.left + viewport.clientLeft + viewport.clientWidth ||
          e.clientY >= rect.top + viewport.clientTop + viewport.clientHeight) return;
      drag = {x:e.clientX, left:viewport.scrollLeft};
      viewport.setPointerCapture(e.pointerId);
      viewport.classList.add('is-dragging'); e.preventDefault();
    });
    viewport.addEventListener('pointermove', e => {
      if (drag) viewport.scrollLeft = drag.left + drag.x - e.clientX;
    });
    const stop = () => { drag = null; viewport.classList.remove('is-dragging'); };
    viewport.addEventListener('pointerup', stop);
    viewport.addEventListener('pointercancel', stop);
    viewport.addEventListener('lostpointercapture', stop);
  }

  function profileTable(title, profile) {
    if (!profile || !profile.soil_layers) return '';
    let top = 0;
    const body = profile.soil_layers.map(layer => {
      const bottom = top + layer.depth;
      const row = `<tr><td>${number(top * 100, 0)}–${number(bottom * 100, 0)}</td>
        <td>${number(layer.SAT, 3)}</td><td>${number(layer.FC, 3)}</td>
        <td>${number(layer.PWP, 3)}</td><td>${number(layer.Ks, 1)}</td>
        <td>${number(layer.penetrability, 0)}</td></tr>`;
      top = bottom;
      return row;
    }).join('');
    return `<h3>${title} 土层</h3><div class="dist-profile"><table>
      <thead><tr><th>层界 cm</th><th>SAT</th><th>FC</th><th>PWP</th><th>Ks mm/d</th><th>穿透 %</th></tr></thead>
      <tbody>${body}</tbody></table></div>`;
  }

  function resultRows(crop, record) {
    if (!record) return '<tr><td colspan="7" class="muted">无该作物结果</td></tr>';
    return record.schemes.map(r => `<tr><td>${escapeHtml(r.label)}</td><td>${number(r.irrigation_mm,1)}</td>
      <td>${number(r.net_water_m3,0)}</td><td>${number(r.yield_t_ha,3)}</td><td>${number(r.production_t,2)}</td>
      <td>${number(r.et_mm,1)}</td><td><button class="btn btn-sm" data-crop="${crop}" data-scheme="${r.scheme}">逐日数据</button></td></tr>`).join('');
  }

  async function showDetail(id) {
    clearDailyTable();
    const token = ++detailId, requestedYear = year;
    focused = id; draw();
    $('#distDetailTitle').textContent = id;
    $('#distDetail').textContent = '正在读取…';
    const data = await request(`/api/distributed/fixed/grid/${encodeURIComponent(id)}?year=${year}`);
    if (token !== detailId || requestedYear !== year || focused !== id) return;
    const areas = data.classification.areas_ha || {};
    let html = `<p>水稻 ${number(areas.rice)} ha · 玉米 ${number(areas.corn)} ha · 气象格点 ${escapeHtml(data.point)}</p>
      <a class="btn btn-sm" href="${BASE}/api/distributed/fixed/inputs/${encodeURIComponent(id)}?year=${year}">下载输入</a>
      <a class="btn btn-sm" href="${BASE}/api/distributed/fixed/weather/${encodeURIComponent(id)}?year=${year}">下载气象</a>`;
    for (const crop of ['rice','corn']) {
      if (!(areas[crop] > 0)) continue;
      const settings = data.inputs[crop] && data.inputs[crop].agronomy;
      html += `<h3>${cropNames[crop]} · 各制度对比</h3><div class="dist-table-wrap"><table class="dist-result-table"><thead><tr>
        <th>灌溉制度</th><th>净灌水 mm</th><th>净水量 m³</th><th>单产 t/ha</th><th>总产 t</th><th>ET mm</th><th>查询</th>
        </tr></thead><tbody>${resultRows(crop,data.results[crop])}</tbody></table></div>`;
      if (settings) html += `<details><summary>${cropNames[crop]} · 计算输入</summary><p>种植 ${escapeHtml(settings.planting_date_iso)} · 生育期结束 ${escapeHtml(settings.harvest_date)}</p>
        ${profileTable(cropNames[crop], {soil_layers:settings.soil.layers})}<p class="hint">气象文件 ${escapeHtml(data.inputs[crop].weather_file)} · 灌溉施用效率 90%</p></details>`;
    }
    html += '<div id="distDaily" class="dist-daily" aria-live="polite"></div>';
    $('#distDetail').innerHTML = html;
    $('#distDetail').querySelectorAll('[data-scheme]').forEach(button => button.addEventListener('click',guarded(async () => {
      const crop = button.dataset.crop, scheme = button.dataset.scheme;
      const dailyToken = ++detailId;
      const box = $('#distDaily');
      const current = () => dailyToken === detailId && year === requestedYear && focused === id;
      clearDailyTable();
      button.disabled = true; button.textContent = '读取中…';
      box.setAttribute('aria-busy', 'true');
      box.innerHTML = '<p role="status">正在读取逐日数据…</p>';
      box.scrollIntoView({block:'start'});
      try {
        const r = await request(`/api/distributed/fixed/daily/${encodeURIComponent(id)}?year=${requestedYear}&crop=${crop}&scheme=${scheme}`);
        if (!current()) return;
        box.innerHTML = `<h3>${cropNames[crop]} · ${escapeHtml(schemeNames[scheme] || scheme)} · 逐日结果</h3><a class="btn btn-sm" href="${BASE}/api/distributed/fixed/download/${encodeURIComponent(id)}?year=${requestedYear}&crop=${crop}&scheme=${scheme}">下载逐日 CSV</a><div id="distDailyTable"></div>`;
        renderDailyTable($('#distDailyTable'),r.table);
        box.scrollIntoView({block:'start'});
      } catch (e) {
        if (current()) box.innerHTML = `<p role="alert">读取失败：${escapeHtml(e.message)}。请点击“逐日数据”重试。</p>`;
      } finally {
        if (current()) box.setAttribute('aria-busy', 'false');
        button.disabled = false; button.textContent = '逐日数据';
      }
    })));
  }

  /* ---------------- 事件 ---------------- */
  $('#distYear').addEventListener('change', guarded(async () => {
    clearDailyTable();
    year = Number($('#distYear').value);
    focused = null; ++detailId;
    $('#distDetail').innerHTML = '<div class="empty-state">点击灌区中的一个格网。</div>';
    await loadYear();
    draw();
  }));
  $('#distFilter').addEventListener('change', draw);
  $('#distLocate').addEventListener('click', guarded(async () => {
    const id = $('#distGridSearch').value.trim();
    const f = features.find(f => f.id === id);
    if (!f) throw new Error('该年份没有该格网的可显示结果');
    if (!visible(f)) $('#distFilter').value = 'all';
    await showDetail(id);
  }));

  const initialize = guarded(async () => {
    const me = await fetch(BASE + '/api/auth/me', {cache:'no-store'});
    if (!me.ok) { setTimeout(initialize, 2000); return; }
    if (!await loadYears()) return;
    await loadYear();
    draw();
  });
  initialize();
})();
