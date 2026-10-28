'use strict';
// Statistics: what went to paper.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
// --- Print statistics (administrators) -----------------------------------------
const STATS_KEPT={90:'for 3 months',365:'for 1 year',730:'for 2 years',1825:'for 5 years',0:'until you delete them'};
function statsName(user,guestUser){return user===guestUser?'Guests':user===''?'Removed account':user;}
function bucketDate(key){const [y,m,d]=key.split('-').map(Number);return new Date(y,m-1,d||1);}
function bucketLabel(key,unit){return bucketDate(key).toLocaleDateString(undefined,unit==='day'?{weekday:'short',month:'short',day:'numeric'}:{month:'long',year:'numeric'});}
function niceCeiling(value){
  const base=10**Math.floor(Math.log10(Math.max(value,1)));
  return [1,2,2.5,3,4,5,6,8,10].map(step=>step*base).find(step=>step>=value)||10*base;
}
async function openStats(){if(state.role!=='admin')return;workspace(false,false,false,true);await loadStats();}
async function loadStats(){
  const period=$('#stats-period').value;
  $('#stats-export').href=`/api/admin/stats/export?period=${encodeURIComponent(period)}`;
  renderStats(await api(`/api/admin/stats?period=${encodeURIComponent(period)}`));
}
function renderStats(report){
  const t=report.totals;
  state.statsSettings=report.settings;
  $('#stats-off-note').classList.toggle('hidden',report.settings.enabled);
  $('#stats-tiles').innerHTML=[['Pages printed',t.pages],['Sheets of paper',t.sheets],['Print jobs',t.jobs],
    ['Sheets saved',t.sheets_saved,'by double-sided and multi-page printing']]
    .map(([label,value,note])=>`<div class="stat-tile"><span>${label}</span><strong>${count(value)}</strong>${note?`<small>${note}</small>`:''}</div>`).join('');
  const parts=[];
  if(t.colour_pages)parts.push(`${count(t.colour_pages)} pages in colour`);
  if(t.greyscale_pages)parts.push(`${count(t.greyscale_pages)} in greyscale`);
  if(t.auto_pages)parts.push(`${count(t.auto_pages)} with colour left to the printer`);
  if(t.cancelled)parts.push(`${count(t.cancelled)} cancelled ${t.cancelled===1?'job':'jobs'} not counted`);
  if(t.direct_jobs)parts.push(`${count(t.direct_pages)} ${t.direct_pages===1?'page':'pages'} from ${count(t.direct_jobs)} ${t.direct_jobs===1?'job':'jobs'} sent straight to the print server`);
  $('#stats-breakdown').textContent=parts.join(' · ');
  renderStatsChart(report);
  renderStatsTable('#stats-people','Person',report.people.map(p=>({...p,name:statsName(p.user,report.guest_user)})),'Nobody printed in this period.');
  const servers=new Set(report.printers.map(p=>p.server));
  renderStatsTable('#stats-printers','Printer',report.printers.map(p=>({...p,name:p.queue,detail:servers.size>1?serverName(p.server):''})),'No printer was used in this period.');
  $('#stats-enabled').checked=report.settings.enabled;
  $('#stats-retention').value=String(report.settings.retention_days);
  $('#stats-include-cups').checked=!!report.settings.include_cups_jobs;
  $('#stats-oldest').textContent=report.oldest?`The oldest record is from ${today(report.oldest)}.`:'Nothing has been recorded yet.';
}
function renderStatsChart(report){
  const {unit,series}=report;
  $('#stats-chart-title').textContent=`Sheets of paper per ${unit}`;
  const most=Math.max(0,...series.map(b=>b.sheets));
  $('#stats-chart-table').innerHTML=`<table class="stats-table"><thead><tr><th scope="col">${unit==='day'?'Day':'Month'}</th><th scope="col">Jobs</th><th scope="col">Pages</th><th scope="col">Sheets</th></tr></thead><tbody>${
    series.map(b=>`<tr><th scope="row">${html(bucketLabel(b.key,unit))}</th><td>${count(b.jobs)}</td><td>${count(b.pages)}</td><td>${count(b.sheets)}</td></tr>`).join('')}</tbody></table>`;
  const chart=$('#stats-chart');
  if(!most){chart.innerHTML=`<p class="stats-empty">Nothing was printed in this period.</p>`;return;}
  const top=niceCeiling(most), every=unit==='day'?7:Math.ceil(series.length/12);
  const label=(b,i)=>{
    if(i%every)return '';
    const date=bucketDate(b.key);
    if(unit==='day')return date.toLocaleDateString(undefined,{month:'short',day:'numeric'});
    const month=date.toLocaleDateString(undefined,{month:'short'});
    return i===0||date.getMonth()===0?`${month}<small>${date.getFullYear()}</small>`:month;
  };
  chart.innerHTML=`<div class="stats-axis" aria-hidden="true"><span>${count(top)}</span><span>${top%2?'':count(top/2)}</span><span>0</span></div>
    <div class="stats-plot"><div class="stats-bars" style="--count:${series.length}">${series.map((b,i)=>
      `<button type="button" class="stats-bar" data-index="${i}" aria-label="${html(`${bucketLabel(b.key,unit)}: ${count(b.sheets)} sheets, ${count(b.pages)} pages, ${count(b.jobs)} jobs`)}"><span style="height:${b.sheets?`max(2px,${(b.sheets/top*100).toFixed(2)}%)`:'0'}"></span></button>`).join('')}</div>
    <div class="stats-labels" aria-hidden="true" style="--count:${series.length}">${series.map((b,i)=>`<span>${label(b,i)}</span>`).join('')}</div>
    <div class="stats-tooltip hidden"><strong></strong><span></span><small></small></div></div>`;
  const tip=chart.querySelector('.stats-tooltip'), plot=chart.querySelector('.stats-plot');
  const show=bar=>{
    const b=series[Number(bar.dataset.index)];
    tip.querySelector('strong').textContent=`${count(b.sheets)} ${b.sheets===1?'sheet':'sheets'}`;
    tip.querySelector('span').textContent=`${count(b.pages)} ${b.pages===1?'page':'pages'} · ${count(b.jobs)} ${b.jobs===1?'job':'jobs'}`;
    tip.querySelector('small').textContent=bucketLabel(b.key,unit);
    tip.classList.remove('hidden');
    const box=plot.getBoundingClientRect(), mark=bar.getBoundingClientRect();
    const left=Math.min(Math.max(mark.left-box.left+mark.width/2-tip.offsetWidth/2,0),box.width-tip.offsetWidth);
    tip.style.left=`${left}px`;
  };
  chart.querySelectorAll('.stats-bar').forEach(bar=>{bar.onpointerenter=()=>show(bar);bar.onfocus=()=>show(bar);});
  plot.onpointerleave=()=>tip.classList.add('hidden');
  chart.querySelectorAll('.stats-bar').forEach(bar=>bar.onblur=()=>tip.classList.add('hidden'));
}
function renderStatsTable(target,heading,rows,empty){
  if(!rows.length){$(target).innerHTML=`<p class="stats-empty">${empty}</p>`;return;}
  const colour=rows.some(r=>r.colour_pages), most=Math.max(1,...rows.map(r=>r.sheets));
  $(target).innerHTML=`<table class="stats-table"><thead><tr><th scope="col">${heading}</th><th scope="col">Jobs</th><th scope="col">Pages</th>${colour?'<th scope="col">Colour</th>':''}<th scope="col">Sheets</th></tr></thead><tbody>${
    rows.map(r=>`<tr><th scope="row">${html(r.name)}${r.detail?`<small>${html(r.detail)}</small>`:''}</th><td>${count(r.jobs)}</td><td>${count(r.pages)}</td>${colour?`<td>${count(r.colour_pages)}</td>`:''}<td class="stats-sheets"><span class="stats-meter"><span style="width:${(r.sheets/most*100).toFixed(1)}%"></span></span>${count(r.sheets)}</td></tr>`).join('')}</tbody></table>`;
}
async function saveStatsSettings(){
  const enabled=$('#stats-enabled').checked, retention=Number($('#stats-retention').value), before=state.statsSettings?.retention_days;
  if(retention && (!before || retention<before) && !confirm(`Records older than ${$('#stats-retention').selectedOptions[0].textContent.toLowerCase()} will be deleted now. Continue?`))return;
  const saved=await api('/api/admin/stats/settings',{method:'PUT',body:{enabled,retention_days:retention,include_cups_jobs:$('#stats-include-cups').checked}});
  toast(saved.enabled?`Print statistics are kept ${STATS_KEPT[saved.retention_days]}.`:'Print statistics are switched off. Existing records stay until you delete them.');
  await loadStats();
}
async function clearStats(){
  if(!confirm('Delete all print statistics? This cannot be undone.'))return;
  const result=await api('/api/admin/stats',{method:'DELETE'});
  toast(`Deleted ${count(result.deleted)} ${result.deleted===1?'record':'records'}.`);
  await loadStats();
}
