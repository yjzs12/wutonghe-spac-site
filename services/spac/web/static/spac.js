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

const taskPill = $('#taskPill');
const elapsedEl = $('#elapsed');

let currentJobId = null;
let watcher = null;
let running = false;
let wideMode = false;
let lastResult = null;
let agronomyConfig = null;
let weatherBounds = {};

function displayText(value) {
  return String(value == null ? '' : value).replace(/aqua\s*crop/gi, '模型');
}

function inputError(message) {
  const text = String(message || '请检查输入后重试');
  return /aqua\s*crop|traceback|site-packages|\.py\b|error:/i.test(text)
    ? '请检查日期、气象数据和土壤参数后重试' : text;
}

function soilLabel(settings) {
  const preset = agronomyConfig && agronomyConfig.soils[settings.soil_type];
  return displayText(preset ? preset.label : '所选土壤').replace('（当前默认）', '');
}

function preparationFreeNotes(text) {
  return String(text || '').split(/(?<=[。！？])/).filter(sentence => !/泡田|puddl/i.test(sentence)).join('');
}

function tableForDisplay(table) {
  if (!table) return table;
  const indices = table.columns.map((_, i) => i).filter(i =>
    !/泡田|puddl/i.test(table.columns[i]) && !['结论', '限制', '关键期定义', '取水口径', '判定性质', '候选方案', '推荐候选', '现场验证通过', '保产比例(%)'].includes(table.columns[i]));
  return {
    columns: indices.map(i => displayText(table.columns[i])),
    rows: table.rows.map(row => indices.map(i => {
      const value = row[i];
      if (typeof value !== 'string') return value;
      if (!/泡田|puddl/i.test(value)) return displayText(value);
      return table.columns[i] === '灌水口径' ? '田间净量' : displayText(preparationFreeNotes(value));
    })),
  };
}

async function loadAgronomySettings() {
  const data = await api('/api/capabilities');
  if (!data.agronomy) throw new Error('服务未提供种植日期与土壤配置，请更新后端');
  agronomyConfig = data.agronomy;
  const box = $('#agronomySettings');
  box.innerHTML = '';
  ['rice', 'corn'].forEach(crop => {
    const name = crop === 'rice' ? '水稻' : '玉米';
    const section = el('fieldset', 'agronomy-section');
    section.id = crop + 'Agronomy';
    section.innerHTML = `<legend>${name} · 种植与模型参数</legend>
      <div class="field"><label for="${crop}PlantingDate">${name}${crop === 'rice' ? '移栽' : '播种'}日期</label>
        <input id="${crop}PlantingDate" type="date" required>
        <div class="hint">生育期日期随${crop === 'rice' ? '移栽' : '播种'}日期推算。</div></div>
      <details class="calendar-preview"><summary>${name}生育期分布</summary>
        <div id="${crop}CalendarStages" class="calendar-stages" aria-live="polite"></div>
      </details>
      <div class="field"><label for="${crop}SoilType">土壤类型</label>
        <select id="${crop}SoilType"></select></div>
      <details class="soil-editor"><summary>展开并修改土壤参数</summary>
        <div class="hint">含水率单位 m³/m³，须满足 PWP &lt; FC &lt; SAT。水稻土层厚度按0.01m递增，玉米按0.1m递增。</div>
        <div id="${crop}SoilLayers"></div>
        <div class="field"><label for="${crop}SoilCN">径流曲线数 CN</label>
          <input id="${crop}SoilCN" type="number" min="1" max="100" step="0.1" required>
        </div>
        <button class="btn btn-sm" type="button" id="${crop}SoilReset">恢复所选土壤预设</button>
      </details>`;
    section.appendChild(el('div', 'hint', crop === 'rice' ? '当前使用模拟演示默认参数，可自行调整各类参数。' : '作物参数可展开修改，用于模拟试验。'));
    box.appendChild(section);
    const select = $('#' + crop + 'SoilType');
    Object.entries(agronomyConfig.soils).forEach(([key, p]) => {
      const option = el('option'); option.value = key;
      option.textContent = displayText(p.label);
      select.appendChild(option);
    });
    select.value = agronomyConfig.defaults[crop].soil_type;
    select.addEventListener('change', () => renderSoilEditor(crop));
    $('#' + crop + 'SoilReset').addEventListener('click', () => renderSoilEditor(crop));
    const planted = $('#' + crop + 'PlantingDate');
    planted.addEventListener('input', () => updateCropCalendar(crop));
    renderSoilEditor(crop);
    renderParameterEditors(crop);
  });
  syncPlantingYear(true);
  syncCropSections();
}

const soilFields = [
  ['depth', '厚度（m）', 0.1, 3, 0.1],
  ['SAT', '饱和含水率 SAT', 0.001, 0.999, 0.001],
  ['FC', '田间持水量 FC', 0.001, 0.999, 0.00000001],
  ['PWP', '萎蔫含水率 PWP', 0.001, 0.999, 0.001],
  ['Ks', '饱和导水率（mm/d）', 0.001, 10000, 0.001],
  ['penetrability', '根系穿透率（%）', 0.1, 100, 0.1],
];

function renderSoilEditor(crop, saved = null) {
  const cfg = saved || agronomyConfig.soils[$('#' + crop + 'SoilType').value];
  $('#' + crop + 'SoilCN').value = cfg.cn;
  const box = $('#' + crop + 'SoilLayers');
  box.innerHTML = '';
  cfg.layers.forEach((layer, i) => {
    const group = el('div', 'soil-layer');
    group.appendChild(el('strong', '', `第 ${i + 1} 层`));
    const grid = el('div', 'soil-grid');
    soilFields.forEach(([key, label, min, max, step]) => {
      const field = el('div', 'field');
      const id = `${crop}Soil${i}${key}`;
      const title = el('label', '', label); title.htmlFor = id;
      const input = document.createElement('input');
      Object.assign(input, { id, type: 'number', min: String(key === "depth" && crop === "rice" ? .01 : min), max: String(max), step: String(key === "depth" && crop === "rice" ? .01 : step), required: true, value: layer[key] });
      input.dataset.soilKey = key;
      field.append(title, input); grid.appendChild(field);
    });
    group.appendChild(grid); box.appendChild(group);
  });
}

// Same layout and reset behavior as the soil editor; metadata comes from the solver.
function renderParameterEditors(crop, saved = {}) {
  const section = $('#' + crop + 'Agronomy');
  section.querySelectorAll('.parameter-editor').forEach(node => node.remove());
  const groups = [...agronomyConfig.parameter_groups, {key: 'phenology', label: '物候参数'}];
  renderStageEditor(crop, saved.stage_days || {});
  groups.forEach(group => {
    const phenology = group.key === 'phenology';
    const specs = phenology ? agronomyConfig.phenology_spec[crop]
      : agronomyConfig.growth_spec[crop].filter(spec => group.keys.includes(spec.key));
    const defaults = agronomyConfig[phenology ? 'phenology_defaults' : 'growth_defaults'][crop];
    const values = {...defaults, ...(saved[phenology ? 'phenology' : 'growth'] || {})};
    const details = el('details', 'soil-editor parameter-editor');
    details.appendChild(el('summary', '', `展开并修改${group.label}`));
    details.appendChild(el('div', 'hint', phenology ? '以移栽/播种后经过天数 DAP 表示，成熟日沿用所选生育期。' : '用于模拟试验，可修改并恢复默认；修改值将参与本次模型运算。'));
    const grid = el('div', 'soil-grid');
    specs.forEach(spec => {
      const field = el('div', 'field');
      const id = `${crop}Param${spec.key}`;
      const label = el('label', '', spec.label); label.htmlFor = id;
      const input = document.createElement('input');
      Object.assign(input, {id, type:'number', min:String(spec.min), max:String(spec.max), step:'any', required:true, value:values[spec.key]});
      input.dataset.parameterKey = spec.key;
      input.dataset.parameterType = phenology ? 'phenology' : 'growth';
      if (phenology) input.addEventListener('input', () => updateCropCalendar(crop));
      field.append(label, input);
      if (spec.hint) field.appendChild(el('div', 'hint', spec.hint));
      grid.appendChild(field);
    });
    const reset = el('button', 'btn btn-sm', `恢复${group.label}默认值`); reset.type = 'button';
    reset.addEventListener('click', () => {
      grid.querySelectorAll('input').forEach(input => {input.value=defaults[input.dataset.parameterKey];});
      if (phenology) updateCropCalendar(crop);
    });
    details.append(grid, reset); section.appendChild(details);
  });
}

function parameterValues(crop, type) {
  return Object.fromEntries(Array.from($('#' + crop + 'Agronomy').querySelectorAll(`[data-parameter-type="${type}"]`))
    .map(input => [input.dataset.parameterKey, Number(input.value)]));
}

function calendarDay(iso) { return Date.parse(iso + 'T00:00:00Z') / 86400000; }
function calendarISO(day) { return new Date(day * 86400000).toISOString().slice(0, 10); }

function stageValues(crop) {
  return Object.fromEntries(Array.from($('#' + crop + 'CalendarStages').querySelectorAll('[data-stage-key]'))
    .map(input => [input.dataset.stageKey, Number(input.value)]));
}

function renderStageEditor(crop, saved = {}) {
  const box = $('#' + crop + 'CalendarStages');
  const ref = agronomyConfig.calendar.crops[crop];
  const stages = ref.stages.filter(s => s.reference_date >= ref.planting_date);
  box.replaceChildren();
  box.appendChild(el('div', 'hint', '播种/移栽当天为第1天；推算日期自动计算。修改后的阶段参与本次模拟，生育期结束决定收获日期。'));
  const table = document.createElement('table');
  const head = table.createTHead().insertRow();
  ['阶段', crop === 'rice' ? '移栽后天数' : '播种后天数', '推算日期'].forEach(label => head.appendChild(el('th', '', label)));
  const body = table.createTBody();
  stages.forEach((stage, i) => {
    const row = body.insertRow();
    row.insertCell().textContent = stage.name === '收获' ? '生育期结束' : stage.name;
    const input = document.createElement('input');
    const days = calendarDay('2000-' + stage.reference_date) - calendarDay('2000-' + ref.planting_date) + 1;
    Object.assign(input, {type:'number', min:'1', max:'366', step:'1', required:true, value:saved[stage.reference_date] ?? days});
    input.setAttribute('aria-label', `${crop === 'rice' ? '水稻' : '玉米'}${stage.name === '收获' ? '生育期结束' : stage.name}后天数`);
    input.dataset.stageKey = stage.reference_date;
    input.readOnly = i === 0;
    input.addEventListener('input', () => updateCropCalendar(crop));
    row.insertCell().appendChild(input);
    row.insertCell().dataset.stageDate = stage.reference_date;
  });
  const wrap = el('div', 'table-wrap'); wrap.appendChild(table); box.appendChild(wrap);
  const error = el('div', 'hint'); error.id = crop + 'CalendarError'; error.setAttribute('role', 'alert'); error.style.color = '#b42318'; box.appendChild(error);
  const actual = el('div', 'calendar-stages'); actual.id = crop + 'ModelStages'; box.appendChild(actual);
  const reset = el('button', 'btn btn-sm', '恢复生育期分布默认值'); reset.type = 'button';
  reset.addEventListener('click', () => {renderStageEditor(crop); updateCropCalendar(crop);}); box.appendChild(reset);
}

function cropSeasonDays(crop) {
  const ref = agronomyConfig.calendar.crops[crop];
  return calendarDay(`2000-${ref.harvest_date}`) - calendarDay(`2000-${ref.planting_date}`);
}

function updateCropCalendar(crop) {
  const planted = $('#' + crop + 'PlantingDate');
  const box = $('#' + crop + 'CalendarStages');
  const year = $('#yearSelect').value;
  if (!year) return;
  const start = calendarDay(`${year}-${agronomyConfig.sim_start}`) + (crop === 'rice' ? 6 : 0);
  const end = calendarDay(`${year}-${agronomyConfig.sim_end}`);
  planted.min = calendarISO(start);
  planted.max = calendarISO(end - agronomyConfig.min_season_days);
  const errorBox = $('#' + crop + 'CalendarError');
  if (!errorBox) return;
  errorBox.textContent = '';
  if (!planted.value || !planted.checkValidity()) {
    errorBox.textContent = `请选择${planted.min}至${planted.max}之间的日期。`;
    return errorBox.textContent;
  }
  const ref = agronomyConfig.calendar.crops[crop];
  const stages = stageValues(crop), days = Object.values(stages);
  const lastWeather = weatherBounds[year]?.last;
  const errors = [];
  Object.entries(stages).forEach(([key, dap]) => {
    const valid = Number.isInteger(dap) && dap >= 1 && dap <= 366;
    const iso = valid ? calendarISO(calendarDay(planted.value) + dap - 1) : '';
    box.querySelector(`[data-stage-date="${key}"]`).textContent = iso || '请输入有效整数';
    if (iso && lastWeather && iso > lastWeather) errors.push(`推算日期${iso}晚于气象数据最后一天${lastWeather}，请缩短播种后天数或补充气象数据。`);
  });
  if (days.some((d, i) => !Number.isInteger(d) || d < 1 || d > 366 || (i && d <= days[i-1]))) errors.push('各阶段天数须为1至366的整数，并依次递增。');
  const harvest = calendarISO(calendarDay(planted.value) + days[days.length-1] - 1);
  if (days[days.length-1] - 1 < agronomyConfig.min_season_days) errors.push(`整个生育期至少${agronomyConfig.min_season_days}天。`);
  if (harvest > calendarISO(end)) errors.push(`生育期结束不得晚于模拟范围末日${calendarISO(end)}。`);
  const stagesInput = parameterValues(crop, 'phenology');
  if (Object.keys(stagesInput).length) {
    const actual = $('#' + crop + 'ModelStages');
    renderTable(actual, {columns:['模型阶段','DAP','推算日期'], rows:agronomyConfig.phenology_spec[crop].map(spec =>
      [spec.label, stagesInput[spec.key], calendarISO(calendarDay(planted.value) + stagesInput[spec.key])])});
    Object.values(stagesInput).forEach(dap => {
      if (!Number.isFinite(dap)) return;
      const iso = calendarISO(calendarDay(planted.value) + dap);
      if (lastWeather && iso > lastWeather) errors.push(`模型物候推算日期${iso}晚于气象数据最后一天${lastWeather}。`);
      if (dap >= days[days.length-1] - 1) errors.push('模型物候期天数须短于整个生育期。');
    });
  }
  errorBox.textContent = errors[0] || '';
  return errorBox.textContent;
}

function syncPlantingYear(reset = false) {
  if (!agronomyConfig) return;
  const year = $('#yearSelect').value;
  if (!year) return;
  ['rice', 'corn'].forEach(crop => {
    const input = $('#' + crop + 'PlantingDate');
    const md = !reset && input.value ? input.value.slice(5) : agronomyConfig.defaults[crop].planting_date;
    input.value = `${year}-${md}`;
    updateCropCalendar(crop);
  });
}

function syncCropSections() {
  const crop = ($('input[name=crop]:checked') || {}).value || 'both';
  ['rice', 'corn'].forEach(c => {
    const section = $('#' + c + 'Agronomy');
    if (!section) return;
    section.hidden = crop !== 'both' && crop !== c;
    $('#' + c + 'ManagementPanel').hidden = section.hidden;
    section.disabled = running || section.hidden;
  });
}

function agronomyRunOptions(crop) {
  if (!agronomyConfig) throw new Error('种植与土壤配置尚未加载成功，请刷新页面');
  const selected = crop === 'both' ? ['rice', 'corn'] : [crop];
  const payload = {rice_management: {
    control_depth_m: Number($('#riceControlDepth').value), stop_days: Number($('#riceStopDays').value),
    regreen_days: Number($('#riceRegreenDays').value), tillering_peak_dap: Number($('#riceTilleringPeak').value),
    drying_start_dap: Number($('#riceDryingStart').value), drying_days: Number($('#riceDryingDays').value), cold_events: []
  }};
  selected.forEach(c => {
    const calendarError = updateCropCalendar(c);
    if (calendarError) {$('#' + c + 'CalendarStages').closest('details').open = true; throw new Error(calendarError);}
    const section = $('#' + c + 'Agronomy');
    const invalid = Array.from(section.querySelectorAll('input')).find(input => !input.checkValidity());
    if (invalid) {
      const details = invalid.closest('details'); if (details) details.open = true;
      invalid.reportValidity();
      throw new Error('请核实种植日期、土壤及作物参数');
    }
    const layers = Array.from(section.querySelectorAll('.soil-layer')).map(group =>
      Object.fromEntries(Array.from(group.querySelectorAll('input')).map(input => [input.dataset.soilKey, Number(input.value)])));
    if (layers.some(l => !(l.PWP < l.FC && l.FC < l.SAT))) throw new Error('土壤含水率须满足 PWP < FC < SAT');
    const depth = layers.reduce((sum, layer) => sum + layer.depth, 0);
    const minDepth = Number(parameterValues(c, 'growth').Zmax) + 0.1;
    if (depth < minDepth - 1e-8 || depth > 3 + 1e-8) throw new Error(`土壤总深度须在${minDepth}至3m之间，以覆盖根区与底边界`);
    payload[c + '_agronomy'] = {
      planting_date: $('#' + c + 'PlantingDate').value,
      soil_type: $('#' + c + 'SoilType').value,
      soil_layers: layers,
      soil_cn: Number($('#' + c + 'SoilCN').value),
      growth: parameterValues(c, 'growth'),
      phenology: parameterValues(c, 'phenology'),
      stage_days: stageValues(c),
    };
  });
  return payload;
}

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
      syncPlantingYear();
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
  syncPlantingYear();
}

async function loadYears() {
  try {
    const r = await api('/api/years');
    if (!r.ok) { $('#yearChips').innerHTML = `<span class="muted">读取失败：${escapeHtml(r.error)}</span>`; return; }
    weatherBounds = r.bounds || {};
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
    syncPlantingYear();
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
  let options;
  try { options = agronomyRunOptions(crop); }
  catch (e) { toast(e.message, 'err'); return; }

  running = true;
  syncCropSections();
  $('#runBtn').disabled = true;
  $('#demoBtn').disabled = true;
  $('#runBtn').textContent = '模拟中…';
  clearResults();
  setPill('run', '运行中');
  elapsedEl.textContent = '';

  try {
    const r = await postJSON('/api/run', { year: Number(year), crop, ...options });
    if (!r.ok) throw new Error(r.error || '启动失败');
    currentJobId = r.job_id;
    history.replaceState(null, '', location.pathname + `?job=${r.job_id}`);
    watch(r.job_id);
  } catch (e) {
    setPill('err', '启动失败');
    $('#figureBody').textContent = '启动失败：' + inputError(e.message);
    toast('启动失败：' + inputError(e.message), 'err');
    finishUI();
  }
}

function watch(jobId) {
  if (watcher) watcher.stop();
  watcher = watchJob(jobId, {
    onState: s => {
      if (s.status === 'running') {
        setPill('run', '运行中');
        elapsedEl.textContent = '已用时 ' + fmtDuration(s.elapsed);
      } else if (s.status === 'queued') {
        setPill('warn', '排队中');
      }
    },
    onFinish: s => {
      if (s.status === 'done' && s.result) {
        setPill('ok', '已完成');
        elapsedEl.textContent = '耗时 ' + fmtDuration(s.elapsed);
        toast('模拟完成', 'ok');
        renderResult(jobId, s.result);
      } else {
        setPill('err', '失败');
        $('#figureBody').textContent = '模拟失败，请检查输入后重试。';
        toast('模拟失败，请检查输入后重试', 'err');
      }
      finishUI();
    },
  });
}

function finishUI() {
  running = false;
  syncCropSections();
  $('#runBtn').disabled = false;
  $('#runBtn').textContent = '开始模拟';
  $('#demoBtn').disabled = false;
}

/* ---------------- 结果渲染 ---------------- */
function clearResults() {
  $('#agronomyResult').classList.add('hidden');
  $('#evidenceBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#kpiRow').classList.add('hidden');
  $('#kpiRow').innerHTML = '';
  $('#figureBody').innerHTML = '<div class="empty-state">运行中…</div>';
  $('#optimalBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#summaryBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#waterReqBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#fileBody').innerHTML = '<div class="empty-state">暂无</div>';
  $('#toggleWide').disabled = true;
}

function renderResult(jobId, res) {
  lastResult = res;
  const simplified = res.model_revision === '2026-10-26-crop-simplified-v7';
  const riceV6 = simplified || res.model_revision === '2026-10-23-rice-audit-v6';
  const directModel = simplified || ['2026-10-23-rice-audit-v6', '2026-10-23-calendar-v9', '2026-10-23-agronomy-v8', '2026-10-23-corn-direct-v7'].includes(res.model_revision);
  const moistureModel = directModel || res.model_revision === '2026-10-23-corn-moisture-v6';
  const currentModel = moistureModel || res.model_revision === '2026-10-22-corn-water-v5';
  const currentRice = currentModel || res.model_revision === '2026-10-22-rice-v4';
  const standardRice = currentRice || res.model_revision === '2026-10-22-rice-v3';
  const netWater = ['net_with_puddling_v2', 'net_profile_puddling_v3', 'net_puddling_balance_v4'].includes(res.irrigation_basis);
  if (currentModel) {
    $('#yearSelect').value = String(res.year);
    $$('#yearChips .chip').forEach(chip => chip.classList.toggle('on', chip.textContent === String(res.year)));
    const cropValue = (res.crops || []).length === 2 ? 'both' : (res.crops || [])[0];
    if (cropValue) $(`input[name=crop][value=${cropValue}]`).checked = true;
    syncPlantingYear(true);
    if (agronomyConfig) Object.entries(res.agronomy_settings || {}).forEach(([crop, settings]) => {
      $('#' + crop + 'PlantingDate').value = settings.planting_date_iso;
      $('#' + crop + 'PlantingDate').dataset.previous = settings.planting_date_iso;
      updateCropCalendar(crop);
      $('#' + crop + 'SoilType').value = settings.soil_type;
      renderSoilEditor(crop, settings.soil);
      const legacy = res.rice_parameter_version !== agronomyConfig.parameter_version;
      renderParameterEditors(crop, legacy ? {...settings, growth: {...agronomyConfig.legacy_growth_defaults[crop], ...(settings.growth || {})}} : settings);
      updateCropCalendar(crop);
    });
    syncCropSections();
  }
  if (riceV6) {
    const mg = res.rice_management || {};
    for (const [id, key] of [['riceControlDepth','control_depth_m'], ['riceStopDays','stop_days'], ['riceRegreenDays','regreen_days'], ['riceTilleringPeak','tillering_peak_dap'], ['riceDryingStart','drying_start_dap'], ['riceDryingDays','drying_days']]) {
      if (mg[key] != null) $('#' + id).value = String(mg[key]);
    }
  }
  const agronomyResult = $('#agronomyResult');
  agronomyResult.textContent = Object.entries(res.agronomy_settings || {}).map(([crop, settings]) =>
    `${crop === 'rice' ? '水稻移栽' : '玉米播种'}：${settings.planting_date_iso}；生育期结束：${settings.harvest_date}；${soilLabel(settings)}`).join(' ｜ ');
  agronomyResult.classList.toggle('hidden', !res.agronomy_settings);

  /* --- KPI 条：把最优灌溉制度摊成指标卡 --- */
  const kpis = [];
  if (moistureModel && res.corn_optimization) {
    const opt = res.corn_optimization;
    kpis.push({ k: '玉米 · 墒情控制最优阈值', v: `（${opt.selected_smt.join('，')}）`, unit: '' });
  }
  if (res.kpi && res.kpi.length) {
    res.kpi.forEach(k => {
      kpis.push({ k: `${k['作物']} · ${riceV6 && !simplified && k['作物'] === '水稻' ? '候选' : directModel ? '最优' : '历史候选'}${netWater ? '净' : ''}灌水`, v: fmtNum(k['累计灌溉量(mm)'], 1), unit: 'mm', kind: 'accent' });
      kpis.push({ k: `${k['作物']} · ${riceV6 && !simplified && k['作物'] === '水稻' ? '节水候选' : directModel ? '最佳灌溉方式' : '历史候选'}`, v: k['最优方案'], unit: '' });
      if (currentModel && !directModel) {
        kpis.push({ k: `${k['作物']} · 最高产方案`, v: k['最高产方案'], unit: '' });
        kpis.push({ k: `${k['作物']} · 较峰值减产`, v: fmtNum(k['较峰值减产(%)'], 2), unit: '%' });
        kpis.push({ k: `${k['作物']} · 候选水源取水估算`, v: fmtNum(k['水源取水量估算(mm)'], 1), unit: 'mm' });
      }
      /* 展示相对产量；天气充足时可以出现平台，不预设最佳档位。 */
      if (!directModel) kpis.push({ k: `${k['作物']} · 达峰值产量`, v: fmtNum(k['产量达最大百分比(%)'], 1), unit: '%' });
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
  $('#figureSub').textContent = `${res.year} 年 · ${(res.figures || []).length} 张图`;
  $('#toggleWide').disabled = !(res.figures && res.figures.length);
  applyWide();

  /* --- 最优制度表 --- */
  if (res.optimal && res.optimal.rows.length) {
    const indices = res.optimal.columns.map((c, i) => i).filter(i => !(!directModel && currentModel && res.optimal.columns[i] === '最优方案'));
    const view = { columns: indices.map(i => !directModel && res.optimal.columns[i] === '最优方案' ? '历史候选方案' : res.optimal.columns[i]), rows: res.optimal.rows.map(row => indices.map(i => row[i])) };
    renderTable($('#optimalBody'), tableForDisplay(view));
  } else {
    $('#optimalBody').innerHTML = `<div class="empty-state">${directModel ? '暂无最优方案。' : '无优化方案结果'}</div>`;
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
  renderTable($('#summaryBody'), tableForDisplay(res.summary), { highlight: hl });
  $('#optimalTitle').textContent = riceV6 && !simplified ? '灌溉方案筛选' : directModel ? '最佳灌溉方式' : '历史候选结果';
  $('#optimalCriterion').textContent = simplified ? '最高产，同产量取灌水最少' : riceV6 ? '历史筛选结果' : directModel ? '玉米最优方案' : '历史结果';
  $('#summarySub').textContent = `${res.year} 年 · ${res.summary.rows.length} 个方案 · 绿色行为${riceV6 && !simplified ? '历史候选' : directModel ? '最优方案' : '历史候选'}`;
  const evidenceBox = $('#evidenceBody');
  evidenceBox.innerHTML = '';
  if (currentModel && res.evidence && res.evidence.rows.length) {
    const table = el('div'); evidenceBox.appendChild(table); renderTable(table, tableForDisplay(res.evidence));
  } else {
    evidenceBox.appendChild(el('div', 'empty-state', currentModel ? '本次未运行玉米。' : '请重新模拟以生成本表。'));
  }

  /* --- 作物需水量（月表 + 点月份展开逐日明细） --- */
  const wrBox = $('#waterReqBody');
  const wr = res.water_req;
  if (wr && wr.crops && wr.crops.length) {
    wrBox.innerHTML = '';
    wr.crops.forEach(c => {
      const title = el('div', 'fig-group-title',
        `${c.name}${c.scheme ? ' · ' + c.scheme : ''}（生育期 ${c.period} ｜ 全期 ETc ${fmtNum(c.total_etc, 1)} mm ｜ `
        + `Kc ${fmtNum(c.kc, 2)} ｜ 降雨 ${fmtNum(c.total_prcp, 1)} mm ｜ `
        + `${netWater ? '净灌溉' : '灌溉'} ${fmtNum(c.total_irr, 1)} mm）`);
      wrBox.appendChild(title);
      const box = el('div');
      wrBox.appendChild(box);
      renderWaterReq(box, c);
    });
    $('#waterReqSub').textContent =
      `${wr.year} 年 · 点击月份查看逐日数据`;
  } else {
    wrBox.innerHTML = '<div class="empty-state">暂无</div>';
  }

  /* --- 文件 --- */
  renderFiles($('#fileBody'), jobId, (res.files || []).filter(file => !/泡田|puddl/i.test(`${file.rel || ''} ${file.name || ''}`)));
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
  let s;
  try {
    s = await api(`/api/job/${jobId}`);
  } catch (e) {
    /* 免费实例重启 / 重新部署后，job 元数据与结果文件一起没了。
       原来这里会抛未捕获异常，页面停在"运行中…"，看着像卡死。 */
    clearResults();
    $('#figureBody').innerHTML =
      '<div class="empty-state">结果已失效，请重新模拟。</div>';
    setPill('err', '结果已失效');
    toast('结果已失效，请重新模拟', 'err');
    return;
  }
  if (s.status === 'running' || s.status === 'queued') {
    running = true;
    syncCropSections();
    $('#runBtn').disabled = true;
    $('#runBtn').textContent = '模拟中…';
    setPill(s.status === 'queued' ? 'warn' : 'run', s.status === 'queued' ? '排队中' : '运行中');
    watch(jobId);
  } else if (s.status === 'done' && s.result) {
    setPill('ok', '已完成');
    elapsedEl.textContent = '耗时 ' + fmtDuration(s.elapsed);
    renderResult(jobId, s.result);
  } else if (s.status === 'failed') {
    setPill('err', '失败');
    $('#figureBody').textContent = '模拟失败，请检查输入后重试。';
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
  syncPlantingYear(true);
  if (agronomyConfig) ['rice', 'corn'].forEach(crop => {
    $('#' + crop + 'SoilType').value = agronomyConfig.defaults[crop].soil_type;
    renderSoilEditor(crop);
    renderParameterEditors(crop);
  });
  const rb = $('input[name=crop][value=both]');
  if (rb) rb.checked = true;
  syncCropSections();
  startRun(y, 'both');
});
$('#toggleWide').addEventListener('click', () => {
  wideMode = !wideMode;
  applyWide();
});

$('#yearSelect').addEventListener('change', () => syncPlantingYear());
$$('input[name=crop]').forEach(input => input.addEventListener('change', syncCropSections));
Promise.all([loadYears(), loadAgronomySettings()]).then(() => {
  const jid = qs('job');
  if (jid) restoreJob(jid);
}).catch(e => toast('配置加载失败：' + e.message, 'err'));
