'use strict';
const $ = id => document.getElementById(id);
const num = x => x == null ? '—' : new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(x);
const monthText = s => s ? new Date(`${String(s).slice(0,10)}T00:00:00`).toLocaleDateString('ru-RU',{month:'short',year:'numeric'}) : '—';
const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const formatChange = x => x == null ? '—' : `${x>0?'+':''}${num(x)}%`;
const state = {catalog:[],regions:[],regionalNames:[],allRuns:[],chartData:null};
async function api(url,options={}) {const r=await fetch(url,{cache:'no-store',...options});if(!r.ok){let message='';try{const j=await r.json();message=JSON.stringify(j.detail||j)}catch(e){}throw Error(`HTTP ${r.status}${message ? ': '+message : ''}`)}return r.json()}
function message(text,error=false){const root=$('alert');root.hidden=false;root.className=error?'alert error':'alert';root.textContent=text;setTimeout(()=>{if(root.textContent===text)root.hidden=true},8500)}
function selectOptions(node, options, previous, render=x=>x){node.replaceChildren();for(const x of options){const o=document.createElement('option');o.value=x;o.textContent=render(x);node.append(o)}if(options.includes(previous))node.value=previous}
const palette=['#1f844e','#d6a147','#5a93bc','#8956b0','#dd6d65'];
function plot(node, traces, layout={}){
 if(!window.Plotly){node.innerHTML='<div class="empty">Библиотека визуализации недоступна</div>';return}
 const config={responsive:true,scrollZoom:true,displaylogo:false,modeBarButtonsToAdd:['drawline','eraseshape'],toImageButtonOptions:{format:'png',filename:'mortgage-analytics'}};
 const standard={paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{family:'Inter,Segoe UI,Arial',size:11,color:'#5c7767'},hovermode:'x unified',margin:{l:75,r:22,t:50,b:55},xaxis:{type:'date',showgrid:true,gridcolor:'#eaf0ec',rangeselector:{buttons:[{step:'month',count:6,label:'6 мес.',stepmode:'backward'},{step:'year',count:1,label:'1 год',stepmode:'backward'},{step:'year',count:3,label:'3 года',stepmode:'backward'},{step:'all',label:'Все'}]},rangeslider:{visible:true,thickness:.13}},yaxis:{showgrid:true,gridcolor:'#eaf0ec',zeroline:false},legend:{orientation:'h',y:1.17,x:0},dragmode:'zoom',autosize:true,...layout};
 return Plotly.react(node,traces,standard,config);
}
function switchPage(){const page=(location.hash.match(/^#\/(overview|regions|forecast|updates)$/)||[])[1]||'overview';
 for(const p of ['overview','regions','forecast','updates']) $('view-'+p).hidden=p!==page;
 document.querySelectorAll('[data-route]').forEach(n=>{n.classList.toggle('active',n.dataset.route===page);n.setAttribute('aria-current',n.dataset.route===page?'page':'false')});
 $('breadcrumb').textContent=({overview:'Обзор рынка',regions:'Регионы России',forecast:'Прогнозирование',updates:'Журнал обновлений'})[page];
 if(page==='updates'){loadRuns();loadQuality();}if(page==='forecast'&&!$('forecast-result').dataset.loaded)loadForecast();if(page==='regions')refreshRegional();
 window.scrollTo({top:0,behavior:'instant'});
 setTimeout(()=>{for(const id of ['overview-chart','region-chart','forecast-chart']){if($(id)?.data&&window.Plotly)Plotly.Plots.resize($(id))}},140);
}
window.addEventListener('hashchange',switchPage);
async function boot(){
 switchPage();
 try{
  const [health, catalog,meta]=await Promise.all([api('/api/v1/health'),api('/api/v1/series?limit=2000'),api('/api/v1/regions')]);
  // The public portal is read-only in a production deployment.
  for(const id of ['refresh-top','refresh-page'])$(id).hidden=!health.public_refresh;
  loadQuality();
  $('health').textContent='● API доступен';state.catalog=catalog;
  state.regions=meta.regions||[];state.regionalNames=['РОССИЙСКАЯ ФЕДЕРАЦИЯ',...state.regions];
  $('kpi-series').textContent=num(health.series_count);$('kpi-obs').textContent=num(health.observation_count);
  const hasDemo=catalog.some(x=>x.source==='demo'),hasOfficial=catalog.some(x=>x.source.startsWith('cbr'));
  const banner=$('data-notice');banner.className=hasOfficial?'notice good':'notice';
  banner.textContent=hasOfficial?`Загружена официальная статистика ЦБ. ${hasDemo?'В базе также присутствуют тестовые ряды [ДЕМО].':''} Историю обновлений можно проверить в журнале.`:hasDemo?'ВНИМАНИЕ: отображаются вымышленные демонстрационные данные. Официальная статистика пока не загружена.':'База данных пока пуста. Проверьте планировщик и доступ к Банку России в журнале обновлений.';
  const nation=catalog.filter(s=>s.source!=='cbr_xlsx'||s.dimension==='РОССИЙСКАЯ ФЕДЕРАЦИЯ');
  nation.sort((a,b)=>(a.source==='demo')-(b.source==='demo')||a.name.localeCompare(b.name,'ru'));
  selectOptions($('series'),nation.map(x=>x.code),$('series').value,k=>{const s=nation.find(x=>x.code===k);return `${s.name} · ${s.dimension}${s.source==='demo'?' [ДЕМО]':''}`});
  if(nation.length) await loadOverview();else $('overview-chart').innerHTML='<div class="empty">Показателей ещё нет. Подождите загрузки из ЦБ.</div>';
  selectOptions($('region-a'),state.regions,$('region-a').value||'г. Москва');
  selectOptions($('region-b'),state.regions,$('region-b').value||'Московская область');
  if(state.regions.length>1&&$('region-a').value===$('region-b').value)$('region-b').selectedIndex=1;
  selectOptions($('forecast-region'),state.regionalNames,$('forecast-region').value||'РОССИЙСКАЯ ФЕДЕРАЦИЯ');
  await refreshRegional();await loadRuns();
 }catch(e){$('health').textContent='● Ошибка API';message('Не удалось загрузить данные: '+e.message,true)}
}
async function loadOverview(){const code=$('series').value;if(!code)return;
 try{
 const [d,summary]=await Promise.all([api('/api/v1/observations?code='+encodeURIComponent(code)),api('/api/v1/summary?code='+encodeURIComponent(code))]);
 $('kpi-last').textContent=num(summary.last_value);$('kpi-unit').textContent=summary.unit;
 $('kpi-mom').textContent=formatChange(summary.previous_change_pct);$('kpi-mom').className=summary.previous_change_pct>=0?'positive':'negative';
 $('kpi-period').textContent='Период: '+monthText(summary.last_date);
 $('series-meta').textContent=`${d.series.unit} · ${d.count} наблюдений · ${d.series.dimension} · ${d.series.source==='demo'?'СИНТЕТИКА':'Банк России'}`;
 if(d.series.source==='demo'){$('series-source').removeAttribute('href');$('series-source').textContent='Учебные данные'}else {$('series-source').href=d.series.source_url;$('series-source').textContent='Первоисточник ↗'}
 const csv=$('export');csv.href='/api/v1/exports/observations.csv?code='+encodeURIComponent(code);csv.setAttribute('aria-disabled','false');
 $('recent-table').innerHTML=d.observations.slice(-8).reverse().map(r=>`<tr><td>${monthText(r.date)}</td><td><strong>${num(r.value)}</strong> ${esc(d.series.unit)}</td><td>${r.source_url?`<a target="_blank" rel="noopener noreferrer" href="${esc(r.source_url)}">ЦБ ↗</a>`:'—'}</td></tr>`).join('');
 const points=d.observations,trace={x:points.map(r=>r.date),y:points.map(r=>r.value),type:'scatter',mode:'lines+markers',name:'Факт',line:{color:palette[0],width:3},marker:{size:5},fill:'tozeroy',fillcolor:'rgba(43,137,75,.07)',hovertemplate:'%{x|%b %Y}<br>%{y:,.2f}<extra></extra>'};
 plot($('overview-chart'),[trace],{yaxis:{title:{text:d.series.unit},gridcolor:'#eaf0ec'}});
 }catch(e){$('overview-chart').innerHTML=`<div class="not-available">${esc(e.message)}</div>`}
}
async function refreshRegional(){
 if(!state.regions.length){$('region-status').hidden=false;$('region-content').hidden=true;return}
 $('region-status').hidden=true;$('region-content').hidden=false;
 const sheet=$('region-sheet').value;
 try{const info=await api('/api/v1/regions/periods?sheet='+sheet);const dates=info.periods.slice().reverse();
 selectOptions($('region-month'),dates,$('region-month').value,monthText);
 await Promise.all([loadRanking(),loadCompare(),loadRegionMetrics()]);
 }catch(e){$('region-status').hidden=false;$('region-status').textContent='Ошибка региональных данных: '+e.message}
}
async function loadRanking(){const sheet=$('region-sheet').value, period=$('region-month').value;if(!period)return;
 try{const d=await api('/api/v1/regions/ranking?sheet='+sheet+'&period='+period+'&limit=15');let max=d.ranking[0]?.value||1;
 $('ranking').innerHTML=d.ranking.map((r,i)=>`<div class="rank-item" data-region="${esc(r.region)}" title="Нажмите для сравнения"><div class="rank-top"><span>${i+1}. ${esc(r.region)}</span><strong>${num(r.value)}</strong></div><div class="rank-track"><div class="rank-progress" style="width:${Math.max(0,r.value/max*100)}%"></div></div></div>`).join('')||'<div class="empty">Нет наблюдений</div>';
 document.querySelectorAll('.rank-item').forEach(n=>n.addEventListener('click',()=>{$('region-a').value=n.dataset.region;loadCompare()}));
 }catch(e){$('ranking').textContent=e.message}
}
async function loadCompare(){const a=$('region-a').value,b=$('region-b').value,sheet=$('region-sheet').value;
 if(!a||!b)return;
 try{const params=new URLSearchParams({sheet});params.append('regions',a);if(b!==a)params.append('regions',b);
 const d=await api('/api/v1/regions/compare?'+params.toString());
 const traces=d.series.map((s,i)=>({x:s.observations.map(v=>v.date),y:s.observations.map(v=>v.value),type:'scatter',mode:'lines',name:s.region,line:{color:palette[i],width:2.7},hovertemplate:'%{x|%b %Y}<br>%{y:,.0f} млн ₽<extra></extra>'}));
 plot($('region-chart'),traces,{margin:{l:70,r:15,t:40,b:50},yaxis:{title:'млн ₽',gridcolor:'#eaf0ec'}});
 }catch(e){$('region-chart').innerHTML=`<div class="not-available">${esc(e.message)}</div>`}
}
async function loadRegionMetrics(){try{const params=new URLSearchParams({sheet:$('region-sheet').value,region:'РОССИЙСКАЯ ФЕДЕРАЦИЯ',through:$('region-month').value});const d=await api('/api/v1/regions/insights?'+params);
 $('reg-country').textContent=num(d.value);$('reg-yoy').textContent=formatChange(d.yoy_pct);$('reg-avg').textContent=num(d.avg_12m);$('reg-ytd').textContent=num(d.sum_ytd);
 }catch(e){for(const id of ['reg-country','reg-yoy','reg-avg','reg-ytd'])$(id).textContent='—'}
}
async function loadForecast(){const region=$('forecast-region').value;if(!region){$('forecast-result').innerHTML='<div class="notice">Нет региональных данных для прогноза.</div>';return}
 $('forecast-result').dataset.loaded='true';$('forecast-result').innerHTML='<div class="notice">Выполняется проверка моделей временных рядов…</div>';
 const params=new URLSearchParams({region,sheet:$('forecast-sheet').value,months:$('forecast-months').value,scenario_pct:$('scenario').value});
 try{
 const r=await api('/api/v1/forecast?'+params);
 const names={seasonal_naive:'Сезонный наивный',rolling_mean:'Скользящее среднее',damped_trend:'Тренд с затуханием'};
 const rows=r.forecasts.map(x=>`<tr><td>${monthText(x.date)}</td><td><strong>${num(x.value)}</strong></td><td>${num(x.low)}–${num(x.high)}</td></tr>`).join('');
 $('forecast-result').innerHTML=`<section class="panel"><div class="panel-head"><div><h2>История и прогноз · ${esc(r.region)}</h2><p>Зелёная линия — фактические данные ЦБ. Оранжевая — модель. Светлая зона — эвристический диапазон.</p></div></div><div class="forecast-card"><div class="forecast-kpis"><div class="forecast-pill">Победившая модель<b>${names[r.model]}</b></div><div class="forecast-pill">MAE на прошлом периоде<b>${num(r.backtest_mae)} млн ₽</b></div><div class="forecast-pill">Тестовых месяцев<b>${r.backtest_points}</b></div><div class="forecast-pill">Исторических месяцев<b>${r.training_count}</b></div></div><div id="forecast-chart" class="plot"></div><p class="forecast-note">${esc(r.interval_warning)} ${esc(r.stale_data_warning||'')}</p></div></section><div class="two-col"><section class="panel"><div class="panel-head"><div><h2>Прогнозные значения</h2><p>Объём ипотечных выдач, млн рублей</p></div></div><div class="table-scroll"><table><thead><tr><th>Месяц</th><th>Прогноз</th><th>Диапазон</th></tr></thead><tbody>${rows}</tbody></table></div></section><section class="panel"><div class="panel-head"><div><h2>Сравнение моделей</h2><p>MAE, чем меньше — тем лучше</p></div></div><div class="table-scroll"><table><thead><tr><th>Модель</th><th>MAE</th></tr></thead><tbody>${Object.entries(r.alternatives).map(([model,v])=>`<tr><td>${esc(names[model])}${model===r.model?' ✓':''}</td><td>${num(v.mae)}</td></tr>`).join('')}</tbody></table></div><div class="panel-foot">${esc(r.scenario_warning)}</div></section></div>`;
 const h=r.history,fc=r.forecasts;
 const traces=[{x:h.map(x=>x.date),y:h.map(x=>x.value),name:'Факт ЦБ',type:'scatter',mode:'lines',line:{color:palette[0],width:3}},{x:[h.at(-1).date,...fc.map(x=>x.date)],y:[h.at(-1).value,...fc.map(x=>x.value)],name:'Прогноз',type:'scatter',mode:'lines+markers',line:{color:'#d39c35',width:3,dash:'dash'}},{x:[...fc.map(x=>x.date),...fc.slice().reverse().map(x=>x.date)],y:[...fc.map(x=>x.high),...fc.slice().reverse().map(x=>x.low)],name:'Эвристический диапазон',type:'scatter',fill:'toself',fillcolor:'rgba(211,156,53,.16)',line:{color:'rgba(0,0,0,0)'},hoverinfo:'skip'}];
 plot($('forecast-chart'),traces,{yaxis:{title:'млн ₽',gridcolor:'#eaf0ec'}});
 }catch(e){$('forecast-result').innerHTML=`<div class="notice">Не удалось построить прогноз: ${esc(e.message)}. Для прогноза нужно не менее 24 последовательных месяцев официальных данных.</div>`}
}
async function loadQuality(){
 try{
  const [q,artifacts]=await Promise.all([api('/api/v1/data-quality'),api('/api/v1/source-artifacts?limit=1')]);
  const groups=Object.entries(q.groups);
  const types={cbr:'REST API',cbr_xlsx:'Региональный XLSX',cbr_report:'Обзор ЦБ',cbr_snapshot:'Офлайн-снимок'};
  $('quality-table').innerHTML=groups.map(([kind,x])=>`<tr><td>${esc(types[kind]||kind)}</td><td>${num(x.series)}</td><td>${esc(x.latest_period||'—')}</td><td>${num(x.stale_series)}</td><td>${num(x.gapped_series)}</td></tr>`).join('')||'<tr><td colspan="5">Официальных рядов пока нет</td></tr>';
  const details=q.cross_source_checks||[],mismatches=details.filter(x=>x.warning);
  $('quality-evidence').textContent=artifacts.length?`Последний подтверждённый импорт: ${artifacts[0].kind} · ${artifacts[0].retrieval} · SHA-256 ${artifacts[0].sha256.slice(0,16)}…`:'Контрольные суммы ещё не зафиксированы.';
  $('quality-compare').textContent=details.length?`Сверено общих периодов: ${details.length} (последние 24). Расхождений выше 2%: ${mismatches.length}. Методология и округление источников могут различаться.`:'Совпадающих периодов национального обзора и регионального XLSX пока нет.';
  const text=q.status==='no_official_data'?'Официальных рядов нет: загрузите и проверьте первоисточники.':q.status==='ok'?`Контроль: ${q.official_series} официальных рядов, критических индикаторов качества не обнаружено.`:`Внимание: ${q.flagged_series} рядов с предупреждениями; расхождений источников выше 2% — ${q.cross_source_discrepancies}. Проверьте периоды и журнал обновлений.`;
  $('quality-summary').textContent=text;
  $('quality-notice').textContent=text;
  $('quality-notice').className=q.status==='ok'?'notice good':'notice';
 }catch(e){$('quality-notice').textContent='Контроль качества недоступен: '+e.message;$('quality-summary').textContent='Не удалось получить данные контроля качества.'}
}
async function loadRuns(){try{const runs=await api('/api/v1/sync-runs?limit=100');state.allRuns=runs;renderRuns();const stat=await api('/api/v1/refresh/status');
 $('refresh-status').textContent=stat.state==='running'?'Сейчас выполняется фоновое обновление Банка России. Обновите таблицу через несколько секунд.':stat.last_start?`Последняя проверка источников: ${new Date(stat.last_start+'Z').toLocaleString('ru-RU')}, статус: ${stat.state}. ${stat.last_error||''}`:'Автоматическое обновление ещё не запускалось. Если процесс только стартовал, подождите и обновите журнал.';
 }catch(e){$('log-table').innerHTML=`<tr><td colspan="7">Ошибка журнала: ${esc(e.message)}</td></tr>`}}
function renderRuns(){let runs=state.allRuns;let filt=$('log-filter').value;
 if(filt==='success')runs=runs.filter(x=>x.status==='success');if(filt==='error')runs=runs.filter(x=>['error','partial'].includes(x.status));if(filt==='running')runs=runs.filter(x=>x.status==='running');
 $('log-count').textContent=num(state.allRuns.length);$('log-success').textContent=num(state.allRuns.filter(x=>x.status==='success').length);$('log-errors').textContent=num(state.allRuns.filter(x=>x.status==='error'||x.status==='partial').length);
 $('log-time').textContent=state.allRuns.length?new Date(state.allRuns[0].started_at+'Z').toLocaleString('ru-RU'):'—';
 const titles={cbr_report:'Обзор ипотечного рынка ЦБ',cbr_xlsx:'Региональная таблица ЦБ',cbr_snapshot:'Проверенный локальный снимок',manual_refresh:'Ручное обновление',auto_refresh:'Автообновление',demo:'Учебные данные',cbr:'REST API ЦБ'};
 const labels={success:'Успешно',error:'Ошибка',partial:'Частично',running:'В работе'};
 $('log-table').innerHTML=runs.map(x=>`<tr><td>${new Date(x.started_at+'Z').toLocaleString('ru-RU')}</td><td>${esc(titles[x.source]||x.source)}</td><td><span class="badge ${esc(x.status)}">${esc(labels[x.status]||x.status)}</span></td><td>${num(x.inserted)}</td><td>${num(x.updated)}</td><td>${num(x.unchanged)}</td><td>${esc(x.error||'—')}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">Записей с таким статусом нет.</td></tr>';
}
async function requestRefresh(){
 const buttons=[$('refresh-top'),$('refresh-page')];buttons.forEach(x=>x.disabled=true);
 try{
 let response=await fetch('/api/v1/refresh',{method:'POST',headers:{'Content-Type':'application/json'}});
 if(response.status===403){const token=prompt('Сервер защищён. Введите ADMIN_TOKEN для запуска обновления:');if(!token)return;response=await fetch('/api/v1/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':token}})}
 if(!response.ok)throw Error('HTTP '+response.status);
 const data=await response.json();message(data.message||'Запрос принят');await loadRuns();
 }catch(e){message('Не удалось запустить обновление: '+e.message,true)}finally{buttons.forEach(x=>x.disabled=false)}
}
$('export').addEventListener('click',e=>{if($('export').getAttribute('aria-disabled')==='true')e.preventDefault()});
$('series').addEventListener('change',loadOverview);
$('region-sheet').addEventListener('change',refreshRegional);$('region-month').addEventListener('change',()=>Promise.all([loadRanking(),loadRegionMetrics()]));
$('region-a').addEventListener('change',loadCompare);$('region-b').addEventListener('change',loadCompare);
$('forecast-run').addEventListener('click',loadForecast);$('log-filter').addEventListener('change',renderRuns);$('reload-log').addEventListener('click',loadRuns);
$('refresh-top').addEventListener('click',requestRefresh);$('refresh-page').addEventListener('click',requestRefresh);
boot();

// UI refreshes only after a background ETL job changes state. This avoids
// resetting the zoom/pan position on the charts during normal browsing.
let lastRefreshStamp = null;
async function watchRefresh(){
 try{
  const [r, latest]=await Promise.all([api('/api/v1/refresh/status'),api('/api/v1/sync-runs?limit=1')]);
  const job=latest[0];
  const stamp=`${job?.id||'none'}:${job?.status||'none'}:${job?.finished_at||''}`;
  if(r.state==='running'||job?.status==='running') $('health').textContent='● API доступен · обновление…';
  else $('health').textContent='● API доступен';
  if(lastRefreshStamp && stamp!==lastRefreshStamp && r.state!=='running' && job?.status!=='running'){
    await boot();
    if(location.hash==='#/forecast') $('forecast-result').dataset.loaded='';
  }
  lastRefreshStamp=stamp;
 }catch(e){/* Do not interrupt the dashboard due to temporary network errors. */}
}
setInterval(watchRefresh,20000);
watchRefresh();
