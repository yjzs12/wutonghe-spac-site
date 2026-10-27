/* Distributed workbench. Offline canvas geometry; all model data comes from APIs. */
(() => {
  'use strict';
  let geo = null, features = [], selected = new Set(), focused = null;
  let result = null, resultJob = null, activeJob = null, watchGeneration = 0, loadingGeneration = 0, detailGeneration = 0;
  let sources = null, paths = [], scale = 1, offsetX = 0, offsetY = 0, busy = false;
  const canvas = $('#distMap'), ctx = canvas.getContext('2d');
  const colors = {missing:'#273c46',rice:'#59bd91',corn:'#d1b962',other:'#71828b'};
  const cropNames = {rice:'水稻',corn:'玉米',other:'其他',unknown:'未分类',all:'合计'};
  const esc = escapeHtml;
  const number = (v, d=2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d});
  const notice = message => { $('#distNotice').textContent = message; };

  async function request(path, options={}) {
    const response = await fetch(BASE+path, options);
    const data = await response.json();
    if (response.status === 401 && window.__authNeedLogin) window.__authNeedLogin();
    if (!response.ok || data.ok === false) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : data.error || `请求失败 (${response.status})`);
      error.needConfirm=!!data.need_confirm;
      throw error;
    }
    return data;
  }
  const post = (path, data) => request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function formUpload(path, file, fields={}) {
    if (!file) throw new Error('请先选择文件');
    const data = new FormData(); data.append('file',file);
    Object.entries(fields).forEach(([k,v]) => data.append(k,v));
    return request(path,{method:'POST',body:data});
  }
  const guarded = fn => async () => { try { await fn(); } catch(e) { setBusy(false);notice(e.message);toast(e.message,'err'); } };
  function fillSelect(node, items, blank=null) {
    const old = node.value; node.replaceChildren();
    if (blank) node.add(new Option(blank,''));
    items.forEach(item => node.add(new Option(item.name||item.title||String(item.id),String(item.id))));
    if ([...node.options].some(o => o.value === old)) node.value=old;
  }
  async function loadSources(preferred={}) {
    sources = await request('/api/distributed/sources');
    fillSelect($('#distSoilSource'),sources.soil_sources);
    fillSelect($('#distCropSource'),sources.datasets.filter(x => x.kind==='crop' && x.status==='ready'),'请选择种植结构成果');
    fillSelect($('#distCropJob'),sources.crop_jobs,'选择可关联任务');
    fillSelect($('#distYear'),sources.years.map(year => ({id:year,name:String(year)})));
    if (!$('#distCropSource').value && sources.datasets.some(x => x.id==='builtin2025')) $('#distCropSource').value='builtin2025';
    if (preferred.soil) $('#distSoilSource').value=preferred.soil;
    if (preferred.crop) $('#distCropSource').value=preferred.crop;
    if (preferred.year && sources.years.includes(Number(preferred.year))) $('#distYear').value=String(preferred.year);
    else if (!geo && sources.years.includes(2025)) $('#distYear').value='2025';
    if(preferred.crop&&!preferred.year){
      const crop=sources.datasets.find(x=>x.id===preferred.crop);
      if(crop&&sources.years.includes(crop.year))$('#distYear').value=String(crop.year);
    }else if(preferred.year&&!preferred.crop&&!$('#distCrossYear').checked){
      const matches=sources.datasets.filter(x=>x.kind==='crop'&&x.status==='ready'&&x.year===Number(preferred.year));
      $('#distCropSource').value=(matches.find(x=>x.id===`builtin${preferred.year}`)||matches[0]||{id:''}).id;
    }
    await Promise.all([loadGrid(),loadHistory()]);
  }
  const sourceQuery = () => new URLSearchParams({soil_source:$('#distSoilSource').value,crop_source:$('#distCropSource').value}).toString();
  async function loadGrid() {
    const generation=++loadingGeneration;
    ++detailGeneration;
    if(focused)$('#distDetail').textContent='正在读取格网输入…';
    notice('正在载入作物格网…');
    const data=await request('/api/distributed/grid?'+sourceQuery());
    if (generation!==loadingGeneration) return;
    geo=data; features=geo.features;
    selected=new Set([...selected].filter(id => features.some(f => f.id===id)));
    rebuildPaths(); if(scale===1) fitMap(); else draw();
    updateCount();
    const crop=sources.datasets.find(x=>x.id===$('#distCropSource').value);
    notice(crop?`${crop.year} 年种植结构 · ${features.length.toLocaleString()} 格，点击格网选择并查看输入。`:'请选择或导入对应年份的种植结构。');
    if(focused) await showDetail(focused);
  }
  function project(coordinate) { return [coordinate[0]*Math.cos(47.3*Math.PI/180),-coordinate[1]]; }
  function polygons(feature) { return feature.geometry.type==='Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates; }
  let bounds;
  function rebuildPaths() {
    let xs=[],ys=[];
    for(const f of features) for(const p of polygons(f)) for(const ring of p) for(const c of ring) {
      const [x,y]=project(c);xs.push(x);ys.push(y);
    }
    // Large boundaries exceed JS argument limits; reduce all coordinates for true bounds.
    bounds=[xs.reduce((a,b)=>Math.min(a,b),Infinity),ys.reduce((a,b)=>Math.min(a,b),Infinity),xs.reduce((a,b)=>Math.max(a,b),-Infinity),ys.reduce((a,b)=>Math.max(a,b),-Infinity)];
    paths=features.map(feature => {
      const path=new Path2D();
      polygons(feature).forEach(poly => poly.forEach(ring => {ring.forEach((c,i) => {const p=project(c);if(i===0)path.moveTo(...p);else path.lineTo(...p);});path.closePath();}));
      return {feature,path};
    });
  }
  function fitMap() {
    if(!bounds) return;
    scale=Math.min((canvas.clientWidth-42)/(bounds[2]-bounds[0]),(canvas.clientHeight-42)/(bounds[3]-bounds[1]));
    offsetX=(canvas.clientWidth-scale*(bounds[0]+bounds[2]))/2;
    offsetY=(canvas.clientHeight-scale*(bounds[1]+bounds[3]))/2;
    draw();
  }
  function visible(f) {return $('#distFilter').value==='all'||f.properties.dominant_crop===$('#distFilter').value;}
  function resultRows(id) {return result ? result.rows.filter(r => r.grid_id===id&&r.status==='done') : [];}
  function draw() {
    const ratio=window.devicePixelRatio||1, w=canvas.clientWidth,h=canvas.clientHeight;
    canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio);
    ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);
    if(!geo)return;
    ctx.translate(offsetX,offsetY);ctx.scale(scale,scale);
    paths.forEach(({feature:f,path}) => {
      if(!visible(f))return;
      const color=colors[f.properties.dominant_crop]||colors.missing;
      ctx.fillStyle=color;ctx.fill(path,'evenodd');ctx.lineWidth=.3/scale;ctx.strokeStyle='#0c1e24';ctx.stroke(path);
      if(selected.has(f.id)){ctx.strokeStyle='#f8eee0';ctx.lineWidth=1.7/scale;ctx.stroke(path);}
      if(f.id===focused){ctx.strokeStyle='#ffd47a';ctx.lineWidth=2.8/scale;ctx.stroke(path);}
    });
    ctx.setTransform(1,0,0,1,0,0);
    const legend=[['水稻',colors.rice],['玉米',colors.corn],['其他',colors.other],['未分类',colors.missing]];
    $('#distLegend').innerHTML=legend.map(([name,color])=>`<span><i class="dist-swatch" style="background:${color}"></i>${esc(name)}</span>`).join('');
    $('#distMapStats').textContent=`${geo.features.length.toLocaleString()} 格 · 灌区面积 ${number(features.reduce((s,f)=>s+f.properties.area_ha,0))} ha · 已选择 ${selected.size} 格`;
  }
  function updateCount(){ $('#distSelectionCount').textContent=`已选择 ${selected.size} 格，每次最多 ${sources?sources.max_cells:64} 格`;draw(); }
  function toggleSelect(id) {if(selected.has(id))selected.delete(id);else if(selected.size<(sources?sources.max_cells:64))selected.add(id);else toast('每次最多选择64格，请分批运行','err');updateCount();}
  let drag=null;
  canvas.addEventListener('pointerdown',e=>{canvas.setPointerCapture(e.pointerId);drag={x:e.offsetX,y:e.offsetY,ox:offsetX,oy:offsetY,moved:false};});
  canvas.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.offsetX-drag.x,dy=e.offsetY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4)drag.moved=true;if(drag.moved){offsetX=drag.ox+dx;offsetY=drag.oy+dy;draw();}});
  canvas.addEventListener('pointerup',e=>{if(!drag)return;const didDrag=drag.moved;drag=null;if(didDrag)return;const x=(e.offsetX-offsetX)/scale,y=(e.offsetY-offsetY)/scale;const hit=paths.find(p=>visible(p.feature)&&ctx.isPointInPath(p.path,x,y,'evenodd'));if(hit){toggleSelect(hit.feature.id);guarded(()=>showDetail(hit.feature.id))();}});
  canvas.addEventListener('pointercancel',()=>{drag=null;});
  canvas.addEventListener('wheel',e=>{e.preventDefault();const zoom=e.deltaY<0?1.22:1/1.22;offsetX=e.offsetX-(e.offsetX-offsetX)*zoom;offsetY=e.offsetY-(e.offsetY-offsetY)*zoom;scale*=zoom;draw();},{passive:false});
  new ResizeObserver(()=>{if(geo)fitMap();}).observe(canvas.parentElement);

  async function showDetail(id) {
    const generation=++detailGeneration;
    focused=id;draw();$('#distDetailTitle').textContent=id;
    $('#distDetail').textContent='正在读取格网输入…';
    const data=await request('/api/distributed/grid/'+encodeURIComponent(id)+'?'+sourceQuery());
    if(focused!==id||generation!==detailGeneration)return;
    const soil=data.soil, classification=data.classification;
    const cropAreas=classification?Object.entries(classification.areas_ha).map(([c,a])=>`${cropNames[c]||c} ${number(a)} ha`).join(' / '):'尚无分类成果';
    let html=`<div class="dist-detail-grid"><div><h3>种植输入</h3><p>格网面积 ${number(data.feature.properties.area_ha)} ha</p><p>${esc(cropAreas)}</p><p class="dist-note">${classification?`${data.classification_year} 年种植结构 · 未分类 ${number(classification.unclassified_ha)} ha`:'请选择种植结构成果'}</p>${soil.status==='missing'?'<p>该格网暂无土壤输入</p>':''}</div><div>`;
    Object.entries(soil.crops||{}).forEach(([crop,profile])=>{
      let top=0;
      html+=`<h3>${cropNames[crop]} · 模型土层</h3><div class="dist-profile"><table><thead><tr><th>层界 cm</th><th>SAT</th><th>FC</th><th>PWP</th><th>Ks mm/d</th><th>穿透 %</th></tr></thead><tbody>`;
      profile.soil_layers.forEach(layer=>{const bottom=top+layer.depth;html+=`<tr><td>${number(top*100,1)}–${number(bottom*100,1)}</td><td>${number(layer.SAT,3)}</td><td>${number(layer.FC,3)}</td><td>${number(layer.PWP,3)}</td><td>${number(layer.Ks)}</td><td>${number(layer.penetrability)}</td></tr>`;top=bottom;});
      html+='</tbody></table></div>';
    });
    html+='</div></div>';
    const rows=resultRows(id);
    rows.forEach(row=>{const dailyUrl=BASE+'/files/'+encodeURIComponent(resultJob)+'/simulations/'+encodeURIComponent(row.simulation_id)+'/optimal_daily.csv?download=1';html+=`<hr><h3>${cropNames[row.crop]} · ${esc(row.scheme)}</h3><p>净灌水 ${number(row.irrigation_mm)} mm · 产量 ${number(row.yield_t_ha)} t/ha · 该作物净水量 ${number(row.net_water_m3)} m³ · <a href="${esc(dailyUrl)}">下载逐日过程 CSV</a></p><label>生长过程 <select class="dist-series" data-simulation="${esc(row.simulation_id)}"><option value="冠层覆盖度(%)">冠层覆盖度 %</option><option value="干物质量(t/ha)">干物质量 t/ha</option><option value="籽粒产量(t/ha)">籽粒产量 t/ha</option><option value="每日总净灌溉量(mm)">逐日净灌水 mm</option></select></label><canvas class="dist-chart" data-simulation="${esc(row.simulation_id)}" aria-label="所选格网作物生长曲线"></canvas>`;});
    $('#distDetail').innerHTML=html;
    $$('.dist-series').forEach(select=>{const chart=select.parentElement.nextElementSibling;const paint=()=>drawSeries(chart,result.time_series[select.dataset.simulation],select.value);select.addEventListener('change',paint);paint();});
  }
  function drawSeries(chart, table, column) {
    if(!table)return;const idx=table.columns.indexOf(column);if(idx<0)return;
    const values=table.rows.map(r=>r[idx]),ratio=devicePixelRatio||1,w=chart.clientWidth,h=chart.clientHeight;
    chart.width=w*ratio;chart.height=h*ratio;const c=chart.getContext('2d');c.scale(ratio,ratio);
    const max=Math.max(1,...values.filter(v=>typeof v==='number'));const left=44,right=w-18,bottom=h-28,top=18;
    c.strokeStyle='#607984';c.fillStyle='#809ba4';c.font='11px sans-serif';
    for(let i=0;i<=4;i++){const y=bottom-(bottom-top)*i/4;c.beginPath();c.moveTo(left,y);c.lineTo(right,y);c.globalAlpha=.3;c.stroke();c.globalAlpha=1;c.fillText(number(max*i/4,1),2,y+3);}
    c.strokeStyle='#50bda1';c.lineWidth=2;c.beginPath();values.forEach((v,i)=>{const x=left+(right-left)*i/Math.max(1,values.length-1),y=bottom-(bottom-top)*Number(v||0)/max;if(i===0)c.moveTo(x,y);else c.lineTo(x,y);});c.stroke();
    const dateIdx=table.columns.indexOf('模拟日期');if(dateIdx>=0){c.fillText(String(table.rows[0][dateIdx]).slice(0,10),left,h-6);c.fillText(String(table.rows.at(-1)[dateIdx]).slice(0,10),Math.max(left,right-70),h-6);}
  }
  function renderResult(jobId,data) {
    result=data;resultJob=jobId;const coverage=data.coverage,overall=data.summary.find(s=>s.crop==='all');
    notice(`模拟完成：${coverage.successful} 个格网作物，失败 ${coverage.failed} 个，跳过 ${coverage.skipped} 个。`);
    $('#distSummary').className='';
    $('#distSummary').innerHTML=`<div class="dist-metrics"><div class="dist-metric">成功模拟作物面积<strong>${number(overall.simulated_area_ha)} <small>ha</small></strong></div><div class="dist-metric">田间净灌水量<strong>${number(overall.net_water_m3,0)} <small>m³</small></strong></div><div class="dist-metric">水源取水估算<strong>${number(overall.source_water_m3,0)} <small>m³</small></strong></div></div><p class="dist-note">选中 ${number(coverage.selected_area_ha)} ha · 未模拟作物 ${number(coverage.unmodeled_supported_area_ha)} ha · 其他 ${number(coverage.other_area_ha)} ha · 未分类 ${number(coverage.unclassified_area_ha)} ha。</p><div class="dist-table-wrap"><table class="dist-result-table"><thead><tr><th>作物</th><th>模拟面积 ha</th><th>加权净灌水 mm</th><th>净水量 m³</th><th>总产量 t</th></tr></thead><tbody>${data.summary.filter(s=>s.crop!=='all').map(s=>`<tr><td>${cropNames[s.crop]}</td><td>${number(s.simulated_area_ha)}</td><td>${number(s.weighted_irrigation_mm)}</td><td>${number(s.net_water_m3,0)}</td><td>${number(s.production_t)}</td></tr>`).join('')}</tbody></table></div><p class="dist-note">点击已模拟格网查看生长曲线和逐日结果。</p><details><summary>逐格运行状态</summary><div class="dist-table-wrap"><table class="dist-result-table"><thead><tr><th>格网</th><th>作物</th><th>状态</th><th>说明</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${esc(r.grid_id)}</td><td>${cropNames[r.crop]||esc(r.crop)}</td><td>${({done:'完成',failed:'失败',skipped:'跳过'})[r.status]}</td><td>${esc(r.reason||r.scheme||'')}</td></tr>`).join('')}</tbody></table></div></details>`;
    $('#distDownloads').replaceChildren();
    const labels={'grid_results.csv':'逐格结果 CSV','grid_results.geojson':'结果地图 GeoJSON','grid_inputs.geojson':'格网输入 GeoJSON','district_summary.csv':'用水配置汇总 CSV','coverage_report.json':'覆盖与缺测报告','run_inputs.json':'运行输入与来源'};
    data.files.filter(f=>labels[f.rel]).forEach(file=>{const a=document.createElement('a');a.className='btn btn-sm';a.textContent=labels[file.rel];a.href=BASE+'/files/'+encodeURIComponent(jobId)+'/'+file.rel+'?download=1';$('#distDownloads').append(a);});
    draw();if(focused)guarded(()=>showDetail(focused))();
  }
  function setBusy(value){busy=value;['distRun','distImportClass','distImportSoil','distImportWeather','distLinkJob','distYear','distSoilSource','distCropSource','distCrossYear','distRefresh'].forEach(id=>$('#'+id).disabled=value);}
  async function watch(jobId, onDone) {
    const generation=++watchGeneration;let offset=0;activeJob=jobId;setBusy(true);$('#distLog').textContent='';
    while(generation===watchGeneration){
      const data=await request('/api/job/'+encodeURIComponent(jobId)+'?offset='+offset);offset=data.log_offset;
      $('#distProgress').textContent=data.progress||data.status;$('#distStatus').textContent=({queued:'排队中',running:'运行中',done:'已完成',failed:'失败'})[data.status]||data.status;
      $('#distLog').textContent=($('#distLog').textContent+(data.log||'')).slice(-30000);
      if(data.status==='done'){activeJob=null;setBusy(false);if(onDone)await onDone(data.result);await loadHistory();return;}
      if(data.status==='failed'){activeJob=null;setBusy(false);throw new Error(data.error||'任务失败，请查看日志');}
      await new Promise(resolve=>setTimeout(resolve,1400));
    }
  }
  async function loadHistory(){const data=await request('/api/jobs?limit=30');const jobs=(Array.isArray(data)?data:data.jobs||[]).filter(j=>j.params&&j.params.type==='distributed');$('#distHistory').replaceChildren();jobs.slice(0,10).forEach(job=>{const button=document.createElement('button');button.className='btn btn-sm dist-history-btn';button.textContent=`${job.title} · ${job.status}`;button.addEventListener('click',guarded(()=>restoreJob(job.id)));$('#distHistory').append(button);});if(!jobs.length)$('#distHistory').textContent='暂无分布式模拟任务';}
  async function restoreJob(id){if(busy&&activeJob!==id)throw new Error('当前任务仍在运行，请完成后查看其他任务');const data=await request('/api/job/'+encodeURIComponent(id));if(data.params&&data.params.type!=='distributed')throw new Error('该任务不是分布式模拟');if(data.params){selected=new Set(data.params.grid_ids||[]);$('#distCrossYear').checked=!!data.params.allow_cross_year;await loadSources({soil:data.params.soil_source,crop:data.params.crop_source,year:data.params.year});}if(data.status==='done'&&data.result){if(data.result.type!=='distributed')throw new Error('结果类型不匹配');renderResult(id,data.result);$('#distStatus').textContent='已完成';$('#distProgress').textContent='已恢复历史结果';}else if(data.status==='failed')throw new Error(data.error||'历史任务失败');else await watch(id,r=>renderResult(id,r));}

  $('#distRefresh').addEventListener('click',guarded(()=>loadSources()));
  function clearResult(message) {
    result=null;resultJob=null;$('#distSummary').className='empty-state';
    $('#distSummary').textContent=message;$('#distDownloads').replaceChildren();
    $('#distProgress').textContent='尚未运行';
  }
  ['distSoilSource','distCropSource'].forEach(id=>$('#'+id).addEventListener('change',guarded(async()=>{
    if(id==='distCropSource'){
      const crop=sources.datasets.find(x=>x.id===$('#distCropSource').value);
      if(crop&&sources.years.includes(crop.year))$('#distYear').value=String(crop.year);
    }
    clearResult('输入已切换，请重新模拟。');await loadGrid();
  })));
  $('#distFilter').addEventListener('change',draw);
  $('#distYear').addEventListener('change',guarded(async()=>{
    const year=Number($('#distYear').value);
    const sameYear=sources.datasets.filter(x=>x.kind==='crop'&&x.status==='ready'&&x.year===year);
    if(!$('#distCrossYear').checked){
      const current=sources.datasets.find(x=>x.id===$('#distCropSource').value);
      if(!current||current.year!==year)$('#distCropSource').value=(sameYear.find(x=>x.id===`builtin${year}`)||sameYear[0]||{id:''}).id;
    }
    clearResult('年份已切换，请重新模拟。');await loadGrid();
  }));
  $('#distFit').addEventListener('click',fitMap);$('#distClear').addEventListener('click',()=>{selected.clear();updateCount();});
  $('#distLocate').addEventListener('click',guarded(async()=>{const id=$('#distGridSearch').value.trim();if(!features.some(f=>f.id===id))throw new Error('未找到该格网 ID');await showDetail(id);if(!selected.has(id))toggleSelect(id);const f=features.find(f=>f.id===id),p=project([f.properties.longitude,f.properties.latitude]);scale=Math.max(scale,15000);offsetX=canvas.clientWidth/2-scale*p[0];offsetY=canvas.clientHeight/2-scale*p[1];draw();}));
  $('#distSelectReady').addEventListener('click',()=>{if(!$('#distCropSource').value){toast('先选择种植结构成果','err');return;}selected.clear();features.filter(visible).forEach(f=>{if(selected.size>=sources.max_cells)return;const p=f.properties;const soilOk=p.soil_status!=='missing';if(soilOk&&['rice','corn'].some(c=>p.soil_crops.includes(c)&&(p.areas_ha||{})[c]>1e-8))selected.add(f.id);});updateCount();notice(selected.size?'已选择可计算格网；每次最多64格，可清空后另选。':'没有可计算格网，请调整作物筛选或输入数据。');});
  $('#distRun').addEventListener('click',guarded(async()=>{if(busy)return;if(!selected.size)throw new Error('请点击地图选择格网，或使用“选择可计算格网”');const req={year:Number($('#distYear').value),grid_ids:[...selected],soil_source:$('#distSoilSource').value,crop_source:$('#distCropSource').value,allow_cross_year:$('#distCrossYear').checked};[['rice_planting_date','distRicePlant'],['rice_harvest_date','distRiceHarvest'],['corn_planting_date','distCornPlant'],['corn_harvest_date','distCornHarvest']].forEach(([key,id])=>{if($('#'+id).value)req[key]=$('#'+id).value;});const data=await post('/api/distributed/run',req);history.replaceState(null,'',location.pathname+'?distributed_job='+encodeURIComponent(data.job_id));notice(`已提交 ${data.n_grid_crops} 个格网作物，跳过 ${data.skipped.length} 个；正在计算。`);await watch(data.job_id,r=>renderResult(data.job_id,r));}));
  $('#distImportWeather').addEventListener('click',guarded(async()=>{const file=$('#distWeather').files[0];let data;try{data=await formUpload('/api/weather',file);}catch(e){if(!e.needConfirm)throw e;if(!confirm(e.message+'\n确定替换该年份气象？'))return;data=await formUpload('/api/weather',file,{override_era5:'true'});}await loadSources({year:data.year});notice(`已导入 ${data.year} 年气象；完整性将在模拟前检查。`);}));
  $('#distImportSoil').addEventListener('click',guarded(async()=>{const data=await formUpload('/api/distributed/soil',$('#distSoilFile').files[0]);await loadSources({soil:data.source_id});notice(`土壤已导入，包含 ${data.n_grids} 个格网。`);}));
  $('#distImportClass').addEventListener('click',guarded(async()=>{const data=await formUpload('/api/distributed/classification',$('#distClassFile').files[0],{year:$('#distClassYear').value});await watch(data.job_id,()=>loadSources({crop:data.source_id}));}));
  $('#distLinkJob').addEventListener('click',guarded(async()=>{const id=$('#distCropJob').value;if(!id)throw new Error('请选择可关联任务');const data=await post('/api/distributed/classification/from-job',{job_id:id});await watch(data.job_id,()=>loadSources({crop:data.source_id}));}));
  guarded(async()=>{await loadSources();const id=new URLSearchParams(location.search).get('distributed_job');if(id)await restoreJob(id);})().catch(()=>{});
})();
