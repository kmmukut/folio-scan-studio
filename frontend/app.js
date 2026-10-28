'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const icon = name => `<svg class="svg-icon"><use href="#ico-${name}"/></svg>`;
const html = input => String(input??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {job:null,jobs:[],selected:null,scanning:false,csrf:null,user:null,role:null,autoSuggestion:null,guestEnabled:false,guestCsrf:null,guestPreview:false,dragId:null,guestFile:null,memberFile:null,scanners:[],printQueues:[],
  settings:{scanner_id:'',source:'ADF',mode:'Color',resolution:300,auto:true,count:5,role:'normal',skipBlank:false}};
let pollTimer, toastTimer, cropGesture=null, cropRect=null, previewSerial=0;
let suppressPageClickUntil=0;
const largeView={zoom:1,grid:true,pan:true};
const cropView={zoom:1,fitWidth:0,pan:false};

async function api(url, options={}) {
  const opts={credentials:'same-origin',...options,headers:{...(options.headers||{})}};
  if(state.csrf && options.method && !['GET','HEAD'].includes(options.method.toUpperCase()))opts.headers['X-CSRF-Token']=state.csrf;
  if(opts.body && typeof opts.body!=='string' && !(opts.body instanceof FormData) && !(opts.body instanceof Blob)){
    opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.body);
  }
  const response=await fetch(url,opts);
  const body=await response.json().catch(()=>null);
  if(!response.ok){
    if(response.status===401 && !url.includes('/auth/login'))showLogin();
    throw Error(typeof body?.detail==='string'?body.detail:`Request failed (${response.status})`);
  }
  return body;
}
function toast(message,error=false){clearTimeout(toastTimer);let el=$('#toast');el.textContent=message;el.className='toast'+(error?' error':'');toastTimer=setTimeout(()=>el.classList.add('hidden'),5200);}
function problem(e){console.error(e);toast(e.message||String(e),true);}
function currentPage(){return state.job?.pages.find(p=>p.id===state.selected)||null;}
function pageUrl(p,thumb=false,uncropped=false,large=false){return `/api/jobs/${state.job.id}/pages/${p.id}/image?thumb=${thumb}&uncropped=${uncropped}&large=${large}&v=${encodeURIComponent(p.edited_at||p.created||'')}`;}
function today(value){try{return new Date(value).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}catch{return '';}}
function setViews(name){['auth-view','app-view','guest-view'].forEach(v=>$('#'+v).classList.toggle('hidden',v!==name));}
function showLogin(){
  // Do not retain a previous user's private file selection on this device.
  if(typeof clearPrintSelections==='function')clearPrintSelections();
  clearTimeout(pollTimer);state.csrf=null;state.user=null;state.role=null;
  $('#login-name').value='';$('#login-password').value='';
  setViews('auth-view');
  ['forgot-form','forgot-sent','link-form','link-problem','auth-back'].forEach(id=>$('#'+id).classList.add('hidden'));
  $('#forgot-open').classList.toggle('hidden',!state.passwordReset||!!state.setup?.setup_required);
  // A new installation: the first visitor creates the administrator.
  const setup=state.setup?.setup_required;
  $('#setup-form').classList.toggle('hidden',!setup||!state.setup.setup_open);
  $('#setup-closed').classList.toggle('hidden',!setup||state.setup.setup_open);
  $('#login-form').classList.toggle('hidden',!!setup);
  $('#auth-title').innerHTML=setup?'Welcome to<br><em>your paper desk.</em>':'Your paper,<br><em>in good hands.</em>';
  $('#auth-intro').textContent=setup?'Create the administrator account. You can add people, printers and scanners next.':'Sign in to organize your scans, edit individual pages, and finish documents at your own pace.';
  $('#login-guest-button').classList.toggle('hidden',!!setup);$('#login-guest-status').classList.toggle('hidden',!!setup);
  if(setup)return;
  $('#login-guest-button').textContent=state.guestEnabled?'Just need to print? Open guest printing →':'Guest printing is currently disabled';
  $('#login-guest-status').textContent=state.guestEnabled?'No account needed for a limited print job.':'An administrator can enable anonymous printing in Print desk.';
}

function workspace(showPrint=false,showUsers=false,showDevices=false,showStats=false){
  if(state.role==='printer'){showPrint=true;showUsers=false;showStats=false;}
  setViews('app-view');$('#workspace-view').classList.toggle('hidden',showPrint||showUsers||showDevices||showStats);
  $('#admin-print-view').classList.toggle('hidden',!showPrint);
  $('#admin-users-view').classList.toggle('hidden',!showUsers);
  $('#admin-devices-view').classList.toggle('hidden',!showDevices);
  $('#admin-stats-view').classList.toggle('hidden',!showStats);
  $('#workspace-tab').classList.toggle('current',!showPrint&&!showUsers&&!showDevices&&!showStats);
  $('#print-tab').classList.toggle('current',showPrint);
  $('#users-tab').classList.toggle('current',showUsers);
  $('#devices-tab').classList.toggle('current',showDevices);
  $('#stats-tab').classList.toggle('current',showStats);
  $('#top-location').textContent=showStats?'STATISTICS':showDevices?'DEVICES':showUsers?'PEOPLE & ACCESS':showPrint?'PRINT DESK':'WORKBENCH';
}


async function signIn(){
  const name=$('#login-name').value.trim(), password=$('#login-password').value;
  const status=await api('/api/auth/login',{method:'POST',body:{username:name,password}});
  state.user=status.username;state.role=status.role;state.csrf=status.csrf;$('#login-password').value='';
  if(state.role==='printer')await setupPrintOnly();else await setupAdmin();
}
async function setupAdmin(){
  state.guestPreview=false;state.guestEnabled=!!state.guestEnabled;
  $('#app-view').classList.remove('printer-only');$('#workspace-tab').classList.remove('hidden');
  workspace(false);$('#current-user').textContent=(state.user||'A').toUpperCase();
  $('#users-tab').classList.toggle('hidden',state.role!=='admin');
  $('#devices-tab').classList.toggle('hidden',state.role!=='admin');
  $('#stats-tab').classList.toggle('hidden',state.role!=='admin');
  try{let jobs=await api('/api/jobs');let saved=localStorage.getItem('folio.lastDocument');if(jobs.length)await loadJob(jobs.some(j=>j.id===saved)?saved:jobs[0].id);else await newJob();}
  catch(e){problem(e);}
  updateSettings();
  if(state.role==='admin')loadChecklist().catch(()=>{});
  loadSendTargets().catch(()=>{});
  api('/api/scanner').then(r=>{$('#device-led').classList.toggle('on',r.connected);$('#device-status').textContent=r.connected?`Connected via ${r.protocol||'SANE'}`:'Scanner unavailable';}).catch(()=>{$('#device-status').textContent='Detection unavailable';});
  pollScan();
  refreshScannerChoices().catch(problem);
}
async function setupPrintOnly(){
  state.guestPreview=false;
  $('#app-view').classList.add('printer-only');
  $('#workspace-tab').classList.add('hidden');
  $('#current-user').textContent=state.user.toUpperCase();
  $('#users-tab').classList.add('hidden');
  $('#devices-tab').classList.add('hidden');
  $('#stats-tab').classList.add('hidden');
  await showPrintDesk();
}
// Lite and Full editions share this page; hide what this installation lacks.
async function loadFeatures(){
  try{state.features=await api('/api/features');}
  catch{state.features={edition:'full',features:{ocr:true,auto_straighten:true}};}
  const available=state.features.features;
  $('#auto-straighten').classList.toggle('hidden',!available.auto_straighten);
  $('.ocr-choice').classList.toggle('hidden',!available.ocr);
  if(!available.ocr)$('#pdf-ocr').checked=false;
  $('.rail-license').firstChild.textContent=`${state.features.edition.toUpperCase()} EDITION `;
  state.builtinServer=state.features.builtin_print_server||null;
}
// "Built-in" for Folio's own print server, the address for any other.
function serverName(server){return server&&server===state.builtinServer?'Built-in':server;}
async function boot(){
  try {const saved=JSON.parse(localStorage.getItem('folio.scanSettings')||'null');if(saved)state.settings={...state.settings,...saved};}catch{}
  await loadFeatures();
  const status=await api('/api/auth/status').catch(()=>null);
  state.guestEnabled=!!status?.guest_print;state.passwordReset=!!status?.password_reset;
  state.setup={setup_required:!!status?.setup_required,setup_open:!!status?.setup_open};
  if(location.hash.startsWith('#password=') && !await followPasswordLink(!!status?.authenticated))return;
  if(status?.authenticated){state.csrf=status.csrf;state.user=status.username;state.role=status.role;if(state.role==='printer')await setupPrintOnly();else await setupAdmin();}
  else showLogin();
  if(location.hash==='#guest' && state.guestEnabled && state.role!=='printer')await openGuest();
}
async function newJob(){let job=await api('/api/jobs',{method:'POST',body:{title:'Untitled document'}});state.selected=null;await loadJob(job.id);toast('A fresh document is ready.');}
async function loadJob(id){state.job=await api(`/api/jobs/${id}`);localStorage.setItem('folio.lastDocument',id);if(!state.job.pages.some(p=>p.id===state.selected))state.selected=state.job.pages[0]?.id||null;await refreshJobs();renderDocument();}
async function refreshJobs(){
  state.jobs=await api('/api/jobs');
  $('#recent-jobs').innerHTML=state.jobs.length?state.jobs.map(j=>`<div class="recent-job-row ${j.id===state.job?.id?'active':''}"><button class="recent-job ${j.id===state.job?.id?'active':''}" data-job="${j.id}" title="${html(j.title)}">${icon('document')}<span>${html(j.title)}</span><em>${j.pages}</em></button><button class="delete-job" data-delete-job="${j.id}" title="Delete ${html(j.title)}" aria-label="Delete ${html(j.title)}">${icon('trash')}</button></div>`).join(''):'<p class="muted">Nothing on the table yet.</p>';
  $$('.recent-job').forEach(el=>el.onclick=async()=>{try{await loadJob(el.dataset.job);workspace(false);}catch(e){problem(e);}});
  $$('.delete-job').forEach(el=>el.onclick=async e=>{
    e.stopPropagation();const j=state.jobs.find(j=>j.id===el.dataset.deleteJob);if(!j)return;
    if(!confirm(`Permanently delete "${j.title}" and all ${j.pages} scanned pages? This cannot be undone.`))return;
    try{
      await api(`/api/jobs/${j.id}`,{method:'DELETE'});
      if(state.job?.id===j.id){state.job=null;state.selected=null;const remaining=state.jobs.filter(x=>x.id!==j.id);if(remaining.length)await loadJob(remaining[0].id);else await newJob();}
      else await refreshJobs();
      toast('Document and its scans deleted.');
    }catch(err){problem(err);}
  });
}
function renderDocument(){
  const j=state.job;if(!j)return;
  if(document.activeElement!==$('#document-title'))$('#document-title').value=j.title;
  $('#doc-count').textContent=`${j.pages.length} page${j.pages.length===1?'':'s'}`;
  $('#doc-date').textContent=today(j.updated);
  const sent=(j.sent||[]).at(-1);$('#doc-sent').textContent=sent?` · Sent to ${sent.destination} ${today(sent.at)}`:'';
  $('#pages-count-pill').textContent=j.pages.length;
  const deletedCount=j.deleted_pages?.length||0;
  $('#undo-delete').disabled=deletedCount===0;
  $('#undo-delete').title=deletedCount?`Restore most recently deleted page (${deletedCount} available)`: 'No deleted pages to restore';
  $('#deleted-page-count').textContent=String(deletedCount);
  $('#export-button').setAttribute('aria-disabled',!j.pages.length);
  renderPages();renderPreview();renderBatches();
}
// Touch and mouse pointer-based page reordering plus accessible arrow controls.
async function commitOrder(sourceId,targetId,after){
  if(!state.job||sourceId===targetId)return;
  const ids=state.job.pages.map(p=>p.id);
  if(!ids.includes(sourceId)||!ids.includes(targetId))return;
  ids.splice(ids.indexOf(sourceId),1);
  ids.splice(ids.indexOf(targetId)+(after?1:0),0,sourceId);
  if(ids.every((id,i)=>id===state.job.pages[i].id))return;
  try{state.job=await api(`/api/jobs/${state.job.id}/reorder`,{method:'POST',body:{ids}});renderDocument();toast('Page order saved.');}catch(e){problem(e);}
}
async function shiftPage(id,direction){
  const ids=state.job.pages.map(p=>p.id),index=ids.indexOf(id),target=index+direction;
  if(index<0||target<0||target>=ids.length)return;
  await commitOrder(id,ids[target],direction>0);
}
function attachReorderPointer(element,card,selectOnTap=false){
  element.addEventListener('pointerdown',start=>{
    if(start.pointerType==='mouse'&&start.button!==0)return;
    if(start.target.closest('.page-shift'))return;
    if(!selectOnTap){start.preventDefault();start.stopPropagation();}
    const x=start.clientX,y=start.clientY,sourceId=card.dataset.id;
    let active=false,targetId=null;
    const clearMarks=()=>$$('.page-card').forEach(c=>c.classList.remove('drag-source','drop-target'));
    const move=e=>{
      if(e.pointerId!==start.pointerId)return;
      if(!active&&Math.hypot(e.clientX-x,e.clientY-y)<7)return;
      active=true;clearMarks();card.classList.add('drag-source');
      const over=document.elementFromPoint(e.clientX,e.clientY)?.closest('.page-card');
      if(over&&over.dataset.id!==sourceId){targetId=over.dataset.id;over.classList.add('drop-target');}
      else targetId=null;
    };
    const cleanup=()=>{
      window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',stop);
      window.removeEventListener('pointercancel',cancel);clearMarks();
    };
    const stop=e=>{
      if(e.pointerId!==start.pointerId)return;
      cleanup();
      if(active){
        suppressPageClickUntil=Date.now()+450;
        if(targetId){
          const ids=state.job.pages.map(p=>p.id);
          commitOrder(sourceId,targetId,ids.indexOf(sourceId)<ids.indexOf(targetId));
        }
      }else if(!selectOnTap){state.selected=sourceId;renderPages();renderPreview();}
    };
    const cancel=()=>cleanup();
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',stop);
    window.addEventListener('pointercancel',cancel);
  });
}
function renderPages(){
  const pages=state.job.pages,grid=$('#page-grid');
  if(!pages.length){grid.innerHTML=`<div class="empty-pages"><div class="empty-number">0.</div><h3>A clean slate.</h3><p>Feed a few pages, choose your scan settings, and start collecting.</p></div>`;return;}
  grid.innerHTML=pages.map((p,i)=>`<div class="page-card ${state.selected===p.id?'active':''}" tabindex="0" role="listitem" data-id="${p.id}" aria-label="Page ${i+1} of ${pages.length}"><div class="page-number"><span>${String(i+1).padStart(2,'0')} / PAGE</span><button type="button" class="page-grip" title="Drag to reorder page ${i+1}" aria-label="Drag to reorder page ${i+1}">${icon('grip')}</button></div><div class="page-image-holder"><img draggable="false" loading="lazy" alt="Scanned page ${i+1}" src="${pageUrl(p,true)}"></div><div class="page-bottom"><span>${p.dpi} DPI</span><span>${p.export_mode==='Gray'?'GRAY':p.scanned_mode.toUpperCase()} ${p.crop?' · CROPPED':''}</span></div><div class="page-shift-row"><button type="button" class="page-shift" data-way="-1" ${i===0?'disabled':''} aria-label="Move page ${i+1} earlier" title="Move earlier">←</button><span>ORDER</span><button type="button" class="page-shift" data-way="1" ${i===pages.length-1?'disabled':''} aria-label="Move page ${i+1} later" title="Move later">→</button></div></div>`).join('');
  $$('.page-card').forEach(card=>{
    const select=()=>{resetSuggestion();state.selected=card.dataset.id;renderPages();renderPreview();};
    card.onclick=e=>{if(Date.now()<suppressPageClickUntil||e.target.closest('button'))return;select();};
    card.onkeydown=e=>{
      if(e.target.closest('button'))return;
      if(['Enter',' '].includes(e.key)){e.preventDefault();select();}
      if(e.altKey&&['ArrowLeft','ArrowRight'].includes(e.key)){
        e.preventDefault();shiftPage(card.dataset.id,e.key==='ArrowLeft'?-1:1);
      }
    };
    // Trackpad/mouse: hold the card. Touchscreens: drag via the grip.
    if(window.matchMedia('(pointer:fine)').matches)attachReorderPointer(card,card,true);
    const grip=card.querySelector('.page-grip');attachReorderPointer(grip,card,false);
    grip.onclick=e=>e.stopPropagation();
    card.querySelectorAll('.page-shift').forEach(button=>button.onclick=e=>{
      e.stopPropagation();shiftPage(card.dataset.id,Number(button.dataset.way));
    });
  });
}
function displayAngle(value){return `${Number(value).toFixed(2).replace(/\.?0+$/,'')}°`;}
function previewRotation(value){
  const p=currentPage();if(!p)return;
  for(const image of [$('#preview-stage img'),$('#large-preview-image')]){
    if(image)image.style.transform=`rotate(${-(Number(value)-Number(p.deskew||0))}deg)`;
  }
}
function previewTone(){
  const p=currentPage();if(!p)return;
  const brightness=Number($('#brightness').value),contrast=Number($('#contrast').value);
  $('#brightness-value').textContent=brightness>0?`+${brightness}`:String(brightness);
  $('#contrast-value').textContent=contrast>0?`+${contrast}`:String(contrast);
  const relativeBright=(100+brightness)/(100+(p.brightness??0));
  const relativeContrast=(100+contrast)/(100+(p.contrast??0));
  const visual=`brightness(${relativeBright}) contrast(${relativeContrast})`;
  for(const image of [$('#preview-stage img'),$('#large-preview-image')]){
    if(image)image.style.filter=visual;
  }
}
// A pointer-based pan works with a mouse, trackpad drag, pen, and touch.
// It scrolls the viewport without moving/cropping the source image.
function wirePanViewport(viewport, enabled){
  let drag=null;
  viewport.addEventListener('pointerdown',e=>{
    if(!enabled() || (e.pointerType==='mouse' && e.button!==0))return;
    if(viewport.scrollWidth<=viewport.clientWidth+2 && viewport.scrollHeight<=viewport.clientHeight+2)return;
    drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};
    viewport.setPointerCapture(e.pointerId);
    viewport.classList.add('is-panning');
    e.preventDefault();
    e.stopPropagation();
  },true);
  viewport.addEventListener('pointermove',e=>{
    if(!drag||e.pointerId!==drag.id)return;
    viewport.scrollLeft=drag.left+drag.x-e.clientX;
    viewport.scrollTop=drag.top+drag.y-e.clientY;
    e.preventDefault();
  });
  const finish=e=>{
    if(!drag||e.pointerId!==drag.id)return;
    drag=null;viewport.classList.remove('is-panning');
    if(viewport.hasPointerCapture(e.pointerId))viewport.releasePointerCapture(e.pointerId);
  };
  viewport.addEventListener('pointerup',finish);
  viewport.addEventListener('pointercancel',finish);
  viewport.addEventListener('lostpointercapture',finish);
}
function setLargePanMode(on){
  largeView.pan=!!on;
  const btn=$('#large-pan-toggle'),viewport=$('#large-viewport');
  btn.setAttribute('aria-pressed',String(largeView.pan));
  btn.classList.toggle('active',largeView.pan);
  viewport.classList.toggle('pan-enabled',largeView.pan);
}
function setZoom(zoom){
  const viewport=$('#large-viewport'),image=$('#large-preview-image'),wrap=$('#large-image-wrap');
  const before=wrap.getBoundingClientRect();
  const oldWidth=before.width||1,oldHeight=before.height||1;
  const midX=(viewport.scrollLeft+viewport.clientWidth/2)/oldWidth;
  const midY=(viewport.scrollTop+viewport.clientHeight/2)/oldHeight;
  largeView.zoom=Math.max(.5,Math.min(4,zoom));
  $('#zoom-label').textContent=`${Math.round(largeView.zoom*100)}%`;
  const base=Math.max(180,Math.min(image.naturalWidth||850,viewport.clientWidth-55));
  wrap.style.width=Math.round(base*largeView.zoom)+'px';
  $('#zoom-out').disabled=largeView.zoom<=.5;
  $('#zoom-in').disabled=largeView.zoom>=4;
  if(before.width>0 && viewport.clientWidth){
    requestAnimationFrame(()=>{
      viewport.scrollLeft=Math.max(0,Math.round(midX*wrap.offsetWidth-viewport.clientWidth/2));
      viewport.scrollTop=Math.max(0,Math.round(midY*wrap.offsetHeight-viewport.clientHeight/2));
    });
  }
}
function refreshLargePreview(){
  if(!$('#preview-dialog').open)return;
  const p=currentPage();if(!p){$('#preview-dialog').close();return;}
  $('#large-page-meta').textContent=`Page ${state.job.pages.indexOf(p)+1} of ${state.job.pages.length} · ${p.dpi} DPI · ${p.scanned_mode}`;
  const img=$('#large-preview-image'),url=pageUrl(p,false,false,true);
  if(img.dataset.src!==url){
    img.dataset.src=url;
    img.onload=()=>{setZoom(largeView.zoom);previewRotation(Number($('#deskew-angle').value));previewTone();};
    img.onerror=()=>toast('Expanded preview could not load',true);
    img.src=url;
  }else{setZoom(largeView.zoom);previewRotation(Number($('#deskew-angle').value));previewTone();}
  $('#large-angle').value=$('#deskew-angle').value;
  $('#large-angle-output').textContent=displayAngle($('#large-angle').value);
}
function toggleGuides(){
  $('#preview-stage').classList.toggle('grid-visible',$('#small-grid').checked);
  $('#large-scene').classList.toggle('grid-enabled',$('#large-grid').checked);
}
function resetSuggestion(keepPreview=false){
  state.autoSuggestion=null;$('#auto-suggestion').classList.add('hidden');
  if(!keepPreview)previewRotation(currentPage()?.deskew||0);
}
function renderPreview(){
  const p=currentPage(), stage=$('#preview-stage');
  if(!p){previewSerial++;stage.innerHTML='<div class="empty-preview">No page selected, yet.</div>';stage.dataset.src='';
    stage.classList.remove('preview-pending');$('#preview-message').classList.add('hidden');
    $('#preview-meta').textContent='Choose a page to refine it.';$('#page-position').textContent='— / —';}
  else{
    const url=pageUrl(p),index=state.job.pages.indexOf(p);
    // Keep the previous image on screen while the edited preview loads.
    // Never clear the preview during a fine-straighten drag.
    if(stage.dataset.src!==url){
      const generation=++previewSerial;stage.classList.add('preview-pending');
      const next=new Image();next.alt=`Page ${index+1} preview`;next.decoding='async';
      next.onload=()=>{
        if(generation!==previewSerial||currentPage()?.id!==p.id)return;
        // Keep a pending slider adjustment or auto-straighten suggestion
        // visible even if this image finishes loading mid-drag.
        const desired=(state.autoSuggestion?.pageId===p.id)
          ?state.autoSuggestion.angle:Number($('#deskew-angle').value);
        next.style.transform=`rotate(${-(desired-Number(p.deskew||0))}deg)`;
        stage.replaceChildren(next);
        stage.dataset.src=url;stage.classList.remove('preview-pending');
        $('#preview-message').classList.add('hidden');
        previewTone();
        refreshLargePreview();
      };
      next.onerror=()=>{
        if(generation!==previewSerial)return;
        stage.classList.remove('preview-pending');
        $('#preview-message').textContent='Preview could not be refreshed. Your original scan remains saved.';
        $('#preview-message').classList.remove('hidden');
      };
      next.src=url;
    }
    $('#preview-meta').textContent=`PAGE ${index+1} / ${state.job.pages.length}   ·   ${p.dpi} DPI   ·   ${p.scanned_mode}${p.crop?'  ·  CROPPED':''}`;
    $('#page-position').textContent=`${index+1} / ${state.job.pages.length}`;
    $('#page-output').value=p.export_mode;
    $('#page-size').value=String(p.scale??100);
    $('#deskew-angle').value=String(p.deskew??0);
    $('#angle-output').textContent=displayAngle(p.deskew??0);
    $('#brightness').value=String(p.brightness??0);
    $('#contrast').value=String(p.contrast??0);
    $('#brightness-value').textContent=String(p.brightness??0);
    $('#contrast-value').textContent=String(p.contrast??0);
    $('#auto-levels').checked=!!p.auto_levels;
    refreshLargePreview();
  }
  ['rotate-left','rotate-right','crop-button','auto-straighten','deskew-angle','page-size','page-output','brightness','contrast','auto-levels','reset-tones','enlarge-preview','duplicate','delete','reset-edits','view-reset','prev-page','next-page'].forEach(id=>$('#'+id).disabled=!p);
  if(p){let idx=state.job.pages.indexOf(p);$('#prev-page').disabled=idx===0;$('#next-page').disabled=idx===state.job.pages.length-1;}
}

function renderBatches(){
  const batches=state.job.batches,first=$('#front-batch'),second=$('#back-batch');
  let oldFirst=first.value,oldSecond=second.value;
  let markup=batches.map((b,i)=>`<option value="${b.id}">Batch ${i+1} · ${html(b.mode)} · ${b.pages.length}pg</option>`).join('');first.innerHTML=markup;second.innerHTML=markup;
  const fronts=batches.find(b=>b.role==='fronts'),backs=batches.find(b=>b.role==='backs');
  first.value=batches.some(b=>b.id===oldFirst)?oldFirst:(fronts?.id||batches.at(-2)?.id||'');
  second.value=batches.some(b=>b.id===oldSecond)?oldSecond:(backs?.id||batches.at(-1)?.id||'');
  $('#interleave-button').disabled=batches.length<2;
}
function updateSettings(){
  const s=state.settings;
  for(const group of ['source','mode'])$$(`#${group}-picker button`).forEach(b=>b.classList.toggle('selected',b.dataset.value===s[group]));
  $('#resolution').value=s.resolution;$('#auto-count').checked=s.auto;$('#batch-count').value=s.count;
  $('#batch-count').disabled=s.auto;$('#batch-count-row').classList.toggle('hidden',!s.source.includes('ADF'));$('#batch-role').value=s.role;
  // Duplex fronts and backs must stay paired, so blank pages are only left out of regular batches.
  $('#skip-blank').checked=s.skipBlank;$('#skip-blank-line').classList.toggle('hidden',s.role!=='normal');
  localStorage.setItem('folio.scanSettings',JSON.stringify(s));
}
async function patchPage(patch){let p=currentPage();if(!p)return;const pageId=p.id;resetSuggestion(true);try{state.job=await api(`/api/jobs/${state.job.id}/pages/${pageId}`,{method:'PATCH',body:patch});renderDocument();}catch(e){problem(e);}}
function pageHasEdits(p){
  return Boolean(p.crop) || Number(p.rotation||0)!==0 || Number(p.deskew||0)!==0
    || Number(p.scale??100)!==100 || Number(p.brightness||0)!==0
    || Number(p.contrast||0)!==0 || Boolean(p.auto_levels)
    || (p.export_mode||'Original')!=='Original';
}
async function restoreOriginalScan(){
  const p=currentPage();if(!p)return;
  if(pageHasEdits(p) && !confirm('Restore the original scan for this page?\n\nCrop, rotation, straightening, resize, brightness, contrast and grayscale edits will be discarded. The original scanned file will remain safe.'))return;
  try{
    // One atomic server-side reset. This prevents future edit controls from
    // accidentally leaving a stale transformation on the saved page.
    const job=await api(`/api/jobs/${state.job.id}/pages/${p.id}/restore-original`,{method:'POST'});
    if(state.job?.id!==job.id)return;
    state.job=job;
    state.autoSuggestion=null;
    $('#auto-suggestion').classList.add('hidden');
    // Also reset temporary browser-only preview adjustments so the user sees
    // the original immediately, rather than a leftover slider transform.
    for(const img of [$('#preview-stage img'),$('#large-preview-image')]){
      if(img){img.style.transform='none';img.style.filter='none';}
    }
    largeView.zoom=1;
    $('#small-grid').checked=false;$('#large-grid').checked=false;toggleGuides();
    $('#large-viewport').scrollTo(0,0);
    renderDocument();
    toast('Original scanned page restored. All edits removed.');
  }catch(error){problem(error);}
}
async function postPage(suffix,notification){let p=currentPage();if(!p)return;try{let result=await api(`/api/jobs/${state.job.id}/pages/${p.id}/${suffix}`,{method:'POST'});state.job=result.job||result;renderDocument();if(notification)toast(result.message||notification);}catch(e){problem(e);}}
function selectAdjacentPage(way){let p=currentPage();if(!p)return;const pages=state.job.pages,i=pages.findIndex(item=>item.id===p.id),j=i+way;if(j<0||j>=pages.length)return;resetSuggestion();state.selected=pages[j].id;renderPages();renderPreview();}
function renderScanStatus(status){
  state.scanning=!!status.active;$('#scan-now').disabled=state.scanning;
  $('#scan-now').innerHTML=state.scanning?`${icon('scanner')} Scanning…`:`${icon('scanner')} Start scan <span>↗</span>`;
  $('#cancel-scan').hidden=!state.scanning;$('#cancel-scan').disabled=!state.scanning||!!status.cancel_requested;
  $('#cancel-scan').textContent=status.cancel_requested?'Cancelling…':'Cancel current scan';
  let el=$('#scan-alert');if(status.active){el.className='notice';el.textContent=status.cancel_requested?'Requesting graceful cancellation…':'Scanning. Completed pages are preserved even if interrupted.';}
  else if(status.error){el.className='notice error';el.textContent=`Scan error: ${status.error}`;}
  else if(status.pages_added>0){el.className='notice';el.textContent=status.message||`Added ${status.pages_added} pages`;}else el.className='notice hidden';
}
async function pollScan(){
  clearTimeout(pollTimer);
  try {const r=await api('/api/scan');let was=state.scanning;renderScanStatus(r);
    if(r.active)pollTimer=setTimeout(pollScan,1200);
    else if(was){if(r.job_id===state.job?.id)await loadJob(state.job.id);toast(r.error||r.message||'Scan complete',!!r.error);}
  }catch(e){if(state.user)pollTimer=setTimeout(pollScan,4000);}
}
async function scanNow(){
  if(state.scanning)return;if(!state.job)await newJob();const s=state.settings;
  const body={scanner_id:s.scanner_id,source:s.source,mode:s.mode,resolution:s.resolution,role:s.role,count:s.source.includes('ADF')?(s.auto?null:s.count):1,skip_blank:s.skipBlank&&s.role==='normal'};
  try{let status=await api(`/api/jobs/${state.job.id}/scan`,{method:'POST',body});renderScanStatus(status);pollTimer=setTimeout(pollScan,700);}catch(e){problem(e);}
}
async function interleave(){try{state.job=await api(`/api/jobs/${state.job.id}/interleave`,{method:'POST',body:{front_batch_id:$('#front-batch').value,back_batch_id:$('#back-batch').value,reverse_backs:$('#reverse-backs').checked}});renderDocument();toast('Both sides collated. Review before exporting.');}catch(e){problem(e);}}

// Crop coordinates live in the original page AFTER right-angle rotation,
// BEFORE fine deskew. The crop dialog deliberately shows that same base.
// Persistent crops therefore cannot accidentally reveal deleted content when
// a user changes the fine-straighten angle later.
const unit=v=>Math.max(0,Math.min(1,v));
const cropMinimum=.03;
function cropDraw(rect){
  const overlay=$('#crop-selection');
  cropRect=rect?.map(unit)||null;
  if(!cropRect){overlay.classList.add('hidden');return;}
  overlay.classList.remove('hidden');
  overlay.style.left=(cropRect[0]*100)+'%';
  overlay.style.top=(cropRect[1]*100)+'%';
  overlay.style.width=(cropRect[2]*100)+'%';
  overlay.style.height=(cropRect[3]*100)+'%';
}
function cropPoint(e){
  const box=$('#crop-image').getBoundingClientRect();
  return [unit((e.clientX-box.left)/box.width),unit((e.clientY-box.top)/box.height)];
}
function setCropPanMode(on){
  cropView.pan=!!on;
  const toggle=$('#crop-pan-toggle');
  toggle.classList.toggle('active',cropView.pan);
  toggle.setAttribute('aria-pressed',String(cropView.pan));
  $('#crop-canvas').classList.toggle('pan-enabled',cropView.pan);
}
function setCropZoom(value,keepPosition=true){
  const viewport=$('#crop-canvas'),wrap=$('#crop-image-wrap'),image=$('#crop-image');
  if(!image.naturalWidth)return;
  const before=wrap.getBoundingClientRect();
  const oldWidth=before.width||1,oldHeight=before.height||1;
  const midX=(viewport.scrollLeft+viewport.clientWidth/2)/oldWidth;
  const midY=(viewport.scrollTop+viewport.clientHeight/2)/oldHeight;
  const inset=48;
  cropView.fitWidth=Math.max(110,Math.min(image.naturalWidth,
    viewport.clientWidth-inset,(viewport.clientHeight-inset)*image.naturalWidth/image.naturalHeight));
  cropView.zoom=Math.max(.5,Math.min(5,value));
  wrap.style.width=Math.round(cropView.fitWidth*cropView.zoom)+'px';
  $('#crop-zoom-label').textContent=`${Math.round(cropView.zoom*100)}%`;
  $('#crop-zoom-out').disabled=cropView.zoom<=.5;
  $('#crop-zoom-in').disabled=cropView.zoom>=5;
  requestAnimationFrame(()=>{
    viewport.scrollLeft=keepPosition?Math.max(0,Math.round(midX*wrap.offsetWidth-viewport.clientWidth/2)):0;
    viewport.scrollTop=keepPosition?Math.max(0,Math.round(midY*wrap.offsetHeight-viewport.clientHeight/2)):0;
  });
}
async function startCrop(){
  const page=currentPage();if(!page)return;
  const dialog=$('#crop-dialog'),image=$('#crop-image');
  cropGesture=null;
  cropDraw(page.crop||null);
  cropView.zoom=1;setCropPanMode(false);
  image.onload=()=>setCropZoom(1,false);
  image.onerror=()=>toast('Could not load crop image',true);
  dialog.showModal();
  // Use the large unmodified orientation image; cropping remains stable even
  // when the actual scan is already fine-straightened.
  image.src=pageUrl(page,false,true,true);
  if(image.complete&&image.naturalWidth)setCropZoom(1,false);
}
function finishCrop(){
  const rect=cropRect;
  if(!rect){toast('Draw a rectangle over the page before applying the crop.');return;}
  if(rect[2]<cropMinimum||rect[3]<cropMinimum){toast('Crop is too small',true);return;}
  $('#crop-dialog').close();patchPage({crop:rect});
}
function moveCropSelection(e){
  if(!cropGesture||cropGesture.id!==e.pointerId)return;
  const [px,py]=cropPoint(e),g=cropGesture,[sx,sy]=g.start;
  if(g.kind==='draw'){
    cropDraw([Math.min(px,sx),Math.min(py,sy),Math.abs(px-sx),Math.abs(py-sy)]);
  }else if(g.kind==='move'){
    const [x,y,w,h]=g.rect;
    cropDraw([Math.max(0,Math.min(1-w,x+px-sx)),Math.max(0,Math.min(1-h,y+py-sy)),w,h]);
  }else if(g.kind==='resize'){
    const [x,y,w,h]=g.rect,handle=g.handle;
    let left=x,right=x+w,top=y,bottom=y+h;
    if(handle.includes('w'))left=Math.min(right-cropMinimum,px);
    if(handle.includes('e'))right=Math.max(left+cropMinimum,px);
    if(handle.includes('n'))top=Math.min(bottom-cropMinimum,py);
    if(handle.includes('s'))bottom=Math.max(top+cropMinimum,py);
    cropDraw([unit(left),unit(top),unit(right-left),unit(bottom-top)]);
  }
  e.preventDefault();
}
function beginCropSelection(e){
  if(cropView.pan || (e.pointerType==='mouse'&&e.button!==0))return;
  const handle=e.target.closest('.crop-handle'),onSelection=e.target.closest('.crop-selection');
  const kind=handle?'resize':onSelection?'move':'draw';
  cropGesture={id:e.pointerId,kind,start:cropPoint(e),rect:cropRect?[...cropRect]:null,handle:handle?.dataset.handle};
  e.currentTarget.setPointerCapture(e.pointerId);
  if(kind==='draw')cropDraw([cropGesture.start[0],cropGesture.start[1],0,0]);
  e.preventDefault();
}
function endCropSelection(e){
  if(!cropGesture || cropGesture.id!==e.pointerId)return;
  cropGesture=null;
  if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);
}
// Print Desk is a signed-in workspace. Guest page is a separate, restricted mode.
async function loadPrintLibrary(){
  const list=$('#print-library');if(state.role==='printer'){list.innerHTML="<p class='muted'>Printer-only accounts can upload files, but cannot browse anyone's scans.</p>";return;}
  try{
    const jobs=await api('/api/jobs');
    list.innerHTML=jobs.length?jobs.map(j=>`<div class="print-library-row"><button type="button" class="print-library-open" data-open-job="${j.id}" title="Open in Workbench">${icon('document')}<span>${html(j.title)}</span><small>${j.pages} pg</small></button><button type="button" class="print-library-send" data-print-job="${j.id}" ${j.pages?'':'disabled'} title="Print current document">Print ↗</button></div>`).join(''):'<p class="muted">No scanned documents yet. Upload a file above or scan from the workbench.</p>';
    $$('.print-library-open').forEach(button=>button.onclick=async()=>{
      try{await loadJob(button.dataset.openJob);workspace(false);window.scrollTo({top:0,behavior:'instant'});}catch(e){problem(e);}
    });
    $$('.print-library-send').forEach(button=>button.onclick=()=>printSavedDocument(button));
  }catch(e){problem(e);}
}
async function printSavedDocument(button){
  try{
    const id=button.dataset.printJob;
    const job=await api(`/api/jobs/${id}`);
    if(!job.pages.length){toast('This document has no pages',true);return;}
    savedPrint.job=job;savedPrint.index=0;
    $('#saved-print-title').textContent=job.title;
    $('#saved-print-feedback').classList.add('hidden');
    $('#saved-range-mode').value='all';$('#saved-range-box').classList.add('hidden');
    await refreshPrintQueues();renderSavedPrintPreview();$('#saved-print-dialog').showModal();
  }catch(error){problem(error);}
}
function renderSavedPrintPreview(){
  const job=savedPrint.job;if(!job)return;
  savedPrint.index=Math.max(0,Math.min(job.pages.length-1,savedPrint.index));
  const page=job.pages[savedPrint.index];
  $('#saved-preview-image').src=`/api/jobs/${job.id}/pages/${page.id}/image?large=true&v=${encodeURIComponent(page.edited_at||page.created||'')}`;
  $('#saved-page-label').textContent=`Page ${savedPrint.index+1} of ${job.pages.length}`;
  $('#saved-prev').disabled=savedPrint.index===0;
  $('#saved-next').disabled=savedPrint.index===job.pages.length-1;
}
$('#saved-prev').onclick=()=>{savedPrint.index--;renderSavedPrintPreview();};
$('#saved-next').onclick=()=>{savedPrint.index++;renderSavedPrintPreview();};
$('#saved-print-close').onclick=()=>$('#saved-print-dialog').close();
$('#saved-print-form').onsubmit=async event=>{
  event.preventDefault();const job=savedPrint.job;if(!job)return;
  const button=$('#saved-print-form button[type=submit]'),feedback=$('#saved-print-feedback');
  button.disabled=true;setPrintFeedback(feedback,'Preparing document pages…','info');
  try{
    const started=await api(`/api/jobs/${job.id}/print?background=true`,{method:'POST',
      headers:printHeaders('saved',savedPrint.index+1)});
    const {result}=await followTask(started.task,{onUpdate:task=>setPrintFeedback(feedback,taskText(task),'info')});
    setPrintFeedback(feedback,`${receiptMessage(result)} · ${result.pages_selected} page(s) selected.`,'ok');loadMyJobs();
  }catch(error){setPrintFeedback(feedback,error.message,'error');problem(error);}finally{button.disabled=false;}
};

// Slow work (PDF export, OCR, print preparation) runs as a background job on
// the server, so it is never cut off by a reverse proxy's request timeout.
const finishedTask=new Set(['done','failed','cancelled']);
async function followTask(task,{guest=false,onUpdate}={}){
  const base=guest?'/api/guest/tasks':'/api/tasks';let delay=600,failures=0;
  onUpdate?.(task);
  while(!finishedTask.has(task.status)){
    await new Promise(resolve=>setTimeout(resolve,delay));delay=Math.min(delay*1.3,2500);
    try{task=await api(`${base}/${task.id}`);failures=0;}
    catch(error){if(++failures>=4)throw error;continue;}  // Ride out brief network blips
    onUpdate?.(task);
  }
  if(task.status==='failed')throw Error(task.error||'The job could not be completed');
  if(task.status==='cancelled')throw Object.assign(Error('Cancelled'),{cancelled:true});
  return task;
}
function taskText(task){
  const percent=task.status==='running'&&task.progress!=null?` · ${Math.round(task.progress*100)}%`:'';
  return `${task.message}${percent}`;
}
let exportTask=null;
async function exportDocument(event){
  event.preventDefault();
  if(exportTask){
    if(confirm('Cancel this PDF export?'))api(`/api/tasks/${exportTask.id}`,{method:'DELETE'}).catch(problem);
    return;
  }
  if(!state.job?.pages.length){toast('Scan at least one page to export.');return;}
  const button=$('#export-button'),label=button.innerHTML,ocr=$('#pdf-ocr').checked;
  try{
    const started=await api(`/api/jobs/${state.job.id}/exports`,{method:'POST',body:{quality:$('#pdf-quality').value,ocr}});
    exportTask=started.task;button.classList.add('busy');button.title='Click to cancel this export';
    if(ocr)toast('Making a searchable PDF. Text recognition can take a few minutes; you can keep working.');
    const done=await followTask(started.task,{onUpdate:task=>{
      exportTask=task;
      button.innerHTML=`${icon('download')} ${task.status==='queued'?'Waiting…':`Exporting${task.progress!=null?' '+Math.round(task.progress*100)+'%':'…'}`}`;
    }});
    const link=document.createElement('a');link.href=`/api/tasks/${done.id}/file`;link.download=done.filename||'';
    document.body.append(link);link.click();link.remove();
    toast(ocr?'Searchable PDF ready.':'PDF ready.');
  }catch(error){if(error.cancelled)toast('Export cancelled.');else problem(error);}
  finally{exportTask=null;button.classList.remove('busy');button.title='';button.innerHTML=label;}
}
function setPrintFeedback(element,message,kind){element.textContent=message;element.className=`print-feedback ${kind}`;}
function receiptMessage(result){return result.job_id?`Accepted by CUPS as ${result.job_id}. Check the printer for completion.`:`${result.message}. ${result.cups_response||''}`;}
async function loadPrinterStatus(){
  $('#print-status-text').textContent='Checking CUPS queue…';$('#print-status-led').classList.remove('on');
  try{
    const selected=$('#member-printer').value;
    const r=await api(`/api/print/status${selected?'?queue='+encodeURIComponent(selected):''}`);$('#print-queue-name').textContent=`${r.queue||'No default printer'}${r.server?' · '+(r.server_label||r.server):''}`;
    const low=(r.supplies_low||[]).map(s=>`${s.name} ${s.level} %`).join(', ');
    $('#print-status-text').textContent=(r.ready?r.message:`${r.message}${r.available_queues?.length?' · Available: '+r.available_queues.join(', '):''}`)+(low?` · Running low: ${low}`:'');
    $('#print-status-led').classList.toggle('on',!!r.ready);
    showMyQuota().catch(()=>{});
    // If CUPS supplied concrete PPD PageSize values, mark unsupported choices.
    // Refresh capabilities consistently for all three print flows; the
    // device-specific selector is authoritative (not this status probe).
    for(const prefix of ['member','saved'])refreshPrintOptions(prefix);
  }catch(e){$('#print-status-text').textContent=e.message;}
}
// "42 of 100 pages this month", for people with a monthly limit.
async function showMyQuota(){
  const quota=await api('/api/print/quota');
  $('#print-quota').classList.toggle('hidden',quota.limit===null);
  if(quota.limit!==null)$('#print-quota').textContent=`This month you have printed ${count(quota.used)} of your ${count(quota.limit)} pages; ${count(quota.left)} left.`;
}
async function showPrintDesk(){
  workspace(true);
  $('#guest-settings-panel').classList.toggle('hidden',state.role!=='admin');
  $('.print-doc-panel').classList.toggle('hidden',state.role==='printer');
  if(state.role==='admin'){
    try{const s=await api('/api/auth/status');$('#guest-enable').checked=!!s.guest_print;state.guestEnabled=!!s.guest_print;}catch(e){problem(e);}
  }
  await Promise.all([loadPrintLibrary(),refreshPrintQueues()]);
  loadMyJobs();
  await loadPrinterStatus();
}
async function guestStatus(){let data=await api('/api/guest/status');state.guestEnabled=data.enabled;state.guestCsrf=data.csrf;return data;}
async function openGuest(){
  try{
    const guest=await guestStatus();
    if(!guest.enabled&&!state.user){toast('Guest printing is currently disabled',true);return;}
    state.guestPreview=!!state.user;setViews('guest-view');
    $('#guest-back').textContent=state.user?'← Return to print desk':'← Sign in';
    const disabled=!guest.enabled;
    $('#guest-availability').classList.toggle('hidden',!disabled);
    $('#guest-availability').textContent=disabled?'Guest printing is currently OFF. Enable it under Print Desk to test a real guest job.':' ';
    $('#guest-print-form button[type=submit]').disabled=disabled;
    await refreshGuestPrintQueues();
    location.hash='guest';
  }catch(e){problem(e);}
}
async function closeGuest(){
  location.hash='';if(state.user)await showPrintDesk();else showLogin();
}
async function signOut(){
  try{await api('/api/auth/logout',{method:'POST'});}catch{}
  state.user=null;state.role=null;state.csrf=null;$('#app-view').classList.remove('printer-only');
  showLogin();
}

// Your account: an email address (for "Forgot your password?" and, for
// administrators, notifications) and a new password.
const ROLE_NAMES={admin:'Administrator',member:'Member',printer:'Print-only account'};
async function openAccount(){
  const me=await api('/api/auth/account');
  $('#account-name').textContent=me.username;$('#account-role').textContent=ROLE_NAMES[me.role]||me.role;
  $('#account-email').value=me.email||'';$('#account-username').value=me.username;
  $('#account-email-note').textContent=(me.email_ready?'Used to send you a link if you forget your password':'Kept for when email is set up on this Folio: then it’s used for a link if you forget your password')
    +(me.role==='admin'?', and for notifications about printers and people.':'.');
  $('#account-password-form').reset();
  $('#account-dialog').showModal();
}
$('#current-user').onclick=()=>openAccount().catch(problem);
$('#account-close').onclick=()=>$('#account-dialog').close();
$('#account-email-form').onsubmit=async e=>{
  e.preventDefault();
  try{const me=await api('/api/auth/account',{method:'PUT',body:{email:$('#account-email').value.trim()}});
    toast(me.email?`Saved: ${me.email}`:'Email address removed.');}
  catch(err){problem(err);}
};
$('#account-password-form').onsubmit=async e=>{
  e.preventDefault();
  if($('#account-new').value!==$('#account-repeat').value){toast('The two new passwords are different.',true);return;}
  try{
    await api('/api/auth/password',{method:'POST',body:{current_password:$('#account-current').value,new_password:$('#account-new').value}});
    $('#account-dialog').close();state.user=null;state.role=null;state.csrf=null;showLogin();
    toast('Password changed. Sign in with the new one.');
  }catch(err){problem(err);}
};

// Small local previews and file drop. No uploaded file enters the Folio library.
const printPreviews={guest:null,member:null};
const printPreviewSerial={guest:0,member:0};
const savedPrint={job:null,index:0};
// Preview is rendered on Folio's backend, not an embedded PDF browser plugin.
// This works for drag/drop and input selection, including multi-page PDFs.
function uploadPreviewUrl(key,meta,page){
  const route=key==='guest'?'/api/guest/preview':'/api/print/preview';
  return `${route}/${encodeURIComponent(meta.token)}/page/${page}`;
}
function showPrintPage(key,n){
  const meta=printPreviews[key];if(!meta)return;
  meta.current=Math.max(1,Math.min(meta.pages,n));
  const preview=$(`#${key}-file-preview`),current=meta.current;
  preview.replaceChildren();preview.classList.remove('hidden');
  const header=document.createElement('div');header.className='file-preview-header';
  const title=document.createElement('strong');title.textContent=meta.kind==='pdf'?'PDF PREVIEW':'IMAGE PREVIEW';
  const counter=document.createElement('span');counter.textContent=`Page ${current} of ${meta.pages}`;
  header.append(title,counter);
  const img=document.createElement('img');img.className='print-page-image';
  img.alt=`Preview of page ${current}`;img.loading='eager';
  img.src=uploadPreviewUrl(key,meta,current);
  img.onerror=()=>{if(meta===printPreviews[key])setPrintFeedback($(`#${key}-print-feedback`),'The preview expired or the page could not be rendered. Select the file again.','error');};
  preview.append(header,img);
  if(meta.pages>1){
    const nav=document.createElement('div');nav.className='file-preview-controls';
    const previous=document.createElement('button');previous.type='button';previous.textContent='← Previous';previous.disabled=current===1;
    previous.onclick=()=>showPrintPage(key,current-1);
    const select=document.createElement('select');select.setAttribute('aria-label','Choose a PDF page to preview');
    // A numerical selector avoids creating hundreds of image canvases.
    for(let page=1;page<=meta.pages;page++){
      const item=document.createElement('option');item.value=String(page);item.textContent=`Page ${page}`;select.append(item);
    }
    select.value=String(current);select.onchange=e=>showPrintPage(key,Number(e.target.value));
    const next=document.createElement('button');next.type='button';next.textContent='Next →';next.disabled=current===meta.pages;
    next.onclick=()=>showPrintPage(key,current+1);
    nav.append(previous,select,next);preview.append(nav);
    const thumbs=document.createElement('div');thumbs.className='print-thumbnails';
    for(let page=Math.max(1,current-2);page<=Math.min(meta.pages,current+2);page++){
      const button=document.createElement('button');button.type='button';button.title=`Preview page ${page}`;
      if(page===current)button.classList.add('active');
      const mini=document.createElement('img');mini.alt='';mini.loading='lazy';mini.src=uploadPreviewUrl(key,meta,page);
      const label=document.createElement('span');label.textContent=String(page);
      button.append(mini,label);button.onclick=()=>showPrintPage(key,page);thumbs.append(button);
    }
    preview.append(thumbs);
  }
}
async function assignPrintFile(key,file){
  const selected=key==='guest'?'#chosen-file':'#member-chosen-file';
  const preview=$(`#${key}-file-preview`),feedback=$(`#${key}-print-feedback`);
  const version=++printPreviewSerial[key];
  printPreviews[key]=null;
  state[key==='guest'?'guestFile':'memberFile']=file||null;
  preview.replaceChildren();preview.classList.add('hidden');
  if(!file){$(selected).textContent='No file selected';return;}
  $(selected).textContent=`${file.name} · ${(file.size/1048576).toFixed(2)} MB · Validating…`;
  if(key==='guest'&&file.size>10*1048576){setPrintFeedback(feedback,'Guest files must be 10 MB or smaller.','error');return;}
  setPrintFeedback(feedback,'Reading pages and generating a preview…','info');
  try{
    const headers={'X-Upload-Filename':encodeURIComponent(file.name),'Content-Type':file.type||'application/octet-stream'};
    if(key==='guest')headers['X-Guest-CSRF']=state.guestCsrf;
    else headers['X-CSRF-Token']=state.csrf;
    const path=key==='guest'?'/api/guest/inspect':'/api/print/inspect';
    const response=await fetch(path,{method:'POST',credentials:'same-origin',headers,body:file});
    const result=await response.json().catch(()=>null);
    if(!response.ok)throw Error(result?.detail||`Unable to inspect document (${response.status})`);
    if(version!==printPreviewSerial[key])return;
    printPreviews[key]={...result,current:1};
    $(selected).textContent=`${file.name} · ${result.pages} ${result.pages===1?'page':'pages'} · ${(file.size/1048576).toFixed(2)} MB`;
    showPrintPage(key,1);feedback.classList.add('hidden');
  }catch(error){
    if(version!==printPreviewSerial[key])return;
    $(selected).textContent=`${file.name} · Preview unavailable`;
    setPrintFeedback(feedback,error.message,'error');
  }
}

function clearPrintSelections(){
  for(const key of ['guest','member']){
    ++printPreviewSerial[key];printPreviews[key]=null;
    state[key==='guest'?'guestFile':'memberFile']=null;
    $(`#${key}-file-preview`).replaceChildren();$(`#${key}-file-preview`).classList.add('hidden');
    $(key==='guest'?'#chosen-file':'#member-chosen-file').textContent='No file selected';
  }
  $('#guest-print-form').reset();$('#member-print-form').reset();
  $('#guest-print-feedback').classList.add('hidden');$('#member-print-feedback').classList.add('hidden');
  $('#print-document-feedback').classList.add('hidden');
}
function wireDropZone({key,zoneId,inputId}){
  const zone=$('#'+zoneId),input=$('#'+inputId);
  input.addEventListener('change',()=>assignPrintFile(key,input.files?.[0]||null));
  zone.addEventListener('keydown',e=>{if(e.target!==zone)return;if(e.key==='Enter'||e.key===' '){e.preventDefault();input.click();}});
  for(const type of ['dragenter','dragover'])zone.addEventListener(type,e=>{e.preventDefault();e.stopPropagation();zone.classList.add('drag-over');if(e.dataTransfer)e.dataTransfer.dropEffect='copy';});
  for(const type of ['dragleave','dragend'])zone.addEventListener(type,e=>{e.preventDefault();e.stopPropagation();if(type==='dragend'||!zone.contains(e.relatedTarget))zone.classList.remove('drag-over');});
  zone.addEventListener('drop',e=>{
    e.preventDefault();e.stopPropagation();zone.classList.remove('drag-over');
    const file=[...(e.dataTransfer?.files||[])][0];if(!file)return;
    assignPrintFile(key,file);
    // Programmatic FileList assignment supports focus/Choose across browsers.
    try{const transfer=new DataTransfer();transfer.items.add(file);input.files=transfer.files;}catch{}
  });
}
function printHeaders(prefix,currentPage=1){
  const mode=$(`#${prefix}-range-mode`).value;
  const pages=mode==='current'?String(currentPage):mode==='custom'?$(`#${prefix}-pages`).value.trim():'all';
  if(mode==='custom'&&!pages)throw Error('Enter page numbers (for example 1,3-5,8).');
  const scaling=$(`#${prefix}-scale`).value;
  const scale=scaling==='custom'?$(`#${prefix}-scale-percent`).value:scaling;
  return {
    'X-Print-Copies':String($(`#${prefix}-copies`).value),
    'X-Print-Queue':$(`#${prefix}-printer`).value,
    'X-Print-Color':$(`#${prefix}-color`).value,
    'X-Print-Media':$(`#${prefix}-media`).value,
    'X-Print-Source':$(`#${prefix}-source`).value,
    'X-Print-Sides':$(`#${prefix}-sides`).value,
    'X-Print-Orientation':$(`#${prefix}-orientation`).value,
    'X-Print-Nup':$(`#${prefix}-nup`).value,
    'X-Print-Scale':scale,
    'X-Print-Pages':pages,
    'X-Print-Quality':$(`#${prefix}-quality`).value,
    'X-Print-Border':$(`#${prefix}-border`).value,
    'X-Print-Collate':String($(`#${prefix}-collate`).checked),
  };
}
const envelopeSizes=new Set(['com10','dl','c5','c6','monarch']);
function updatePaperSizeHint(prefix){
  const media=$(`#${prefix}-media`), value=media.value;
  const info=printQueueData[prefix==='guest'?'guest':'member'];
  const queue=$(`#${prefix}-printer`).value;
  const caps=info?.printers?.find(q=>q.id===queue)?.capabilities||{};
  const hint=$(`#${prefix}-media-hint`);
  const sides=$(`#${prefix}-sides`);
  const envelope=envelopeSizes.has(value);
  const paperName=media.options[media.selectedIndex]?.textContent||'';
  if(envelope&&sides.value!=='one-sided')sides.value='one-sided';
  for(const opt of sides.options){
    opt.disabled=(envelope&&opt.value!=='one-sided')||(!envelope&&opt.value!=='one-sided'&&caps.duplex===false);
  }
  if(!hint)return;
  if(!envelope){hint.textContent=value!=='default'?`Selected size: ${paperName}.`:'';return;}
  const source=$(`#${prefix}-source`);
  const chosenSource=source.options[source.selectedIndex]?.textContent||'Printer default';
  const feederChoices=(caps.paper_sources||[]).filter(t=>/manual|multi|bypass|envelope/i.test(t.label+' '+t.value));
  hint.textContent=`Selected size: ${paperName}. Envelopes print one-sided. ${feederChoices.length?'Choose the manual / multipurpose feeder for this printer.':'Verify that your printer has an envelope-compatible feeder.'} Current source: ${chosenSource}. Printer driver decides whether this size / tray combination is allowed.`;
}
function wirePrintSettings(prefix){
  const range=$(`#${prefix}-range-mode`),scaling=$(`#${prefix}-scale`);
  range.onchange=()=>{$(`#${prefix}-range-box`).classList.toggle('hidden',range.value!=='custom');};
  scaling.onchange=()=>{$(`#${prefix}-scale-box`).classList.toggle('hidden',scaling.value!=='custom');};
  $(`#${prefix}-media`).addEventListener('change',()=>updatePaperSizeHint(prefix));
  $(`#${prefix}-source`).addEventListener('change',()=>updatePaperSizeHint(prefix));
}
for(const prefix of ['guest','member','saved'])wirePrintSettings(prefix);
async function submitPrintFile(event,guest){
  event.preventDefault();const key=guest?'guest':'member',meta=printPreviews[key];
  const form=$(guest?'#guest-print-form':'#member-print-form'),feedback=$(guest?'#guest-print-feedback':'#member-print-feedback');
  if(!meta){setPrintFeedback(feedback,'Choose a file and wait for its page preview before printing.','error');return;}
  if(guest&&!state.guestEnabled){setPrintFeedback(feedback,'Guest printing is disabled.','error');return;}
  const button=form.querySelector('button[type=submit]');button.disabled=true;button.textContent='Sending to CUPS…';
  setPrintFeedback(feedback,'Preparing your chosen pages…','info');
  try{
    const headers={'Content-Type':'application/json',...printHeaders(key,meta.current),
      ...(guest?{'X-Guest-CSRF':state.guestCsrf}:{'X-CSRF-Token':state.csrf})};
    const response=await fetch(`${guest?'/api/guest/submit-preview':'/api/print/submit-preview'}?background=true`,{
      method:'POST',credentials:'same-origin',headers,body:JSON.stringify({token:meta.token})});
    const started=await response.json().catch(()=>null);
    if(!response.ok)throw Error(started?.detail||`Print failed (${response.status})`);
    const {result}=await followTask(started.task,{guest,onUpdate:task=>setPrintFeedback(feedback,taskText(task),'info')});
    setPrintFeedback(feedback,`${receiptMessage(result)} · ${result.pages_selected} source page(s) selected.`,'ok');
    toast(`Submitted to CUPS${result.job_id?' as '+result.job_id:''}`);if(!guest)loadMyJobs();
  }catch(error){setPrintFeedback(feedback,error.message,'error');problem(error);}finally{
    button.disabled=guest&&!state.guestEnabled;button.innerHTML=`Send to printer ${icon('arrow')}`;
  }
}

// null (the default) >= 0 is true in JavaScript, so test for a number.
const ownLimit=u=>typeof u.monthly_pages==='number'&&u.monthly_pages>=0;
async function loadUsers(){
  try{
    const [users,mailState]=await Promise.all([api('/api/admin/users'),api('/api/admin/mail').catch(()=>null)]);
    state.mailReady=!!mailState?.configured;
    $('#user-list').innerHTML=users.map(u=>`<div class="person" data-person="${html(u.username)}">
      <div class="person-title"><strong>${html(u.username)}</strong>${u.username===state.user?'<span class="self-mark">You</span>':''}<span class="person-state">${!u.enabled?'Disabled':u.pending?(u.invite_expires?'Invited':'Invitation expired'):'Active'}</span></div>
      ${u.pending?`<p class="inline-note">${u.invite_expires?`Hasn’t chosen a password yet; the invitation works until ${html(new Date(u.invite_expires*1000).toLocaleString(undefined,{weekday:'short',hour:'numeric',minute:'2-digit'}))}.`:'Hasn’t chosen a password, and the invitation has expired. Send a new one.'}</p>`:''}
      <label>Role <select class="person-role"><option value="member" ${u.role==='member'?'selected':''}>Member</option><option value="printer" ${u.role==='printer'?'selected':''}>Printer only</option><option value="admin" ${u.role==='admin'?'selected':''}>Administrator</option></select></label>
      <label class="checkline"><input class="person-enabled" type="checkbox" ${u.enabled?'checked':''}> Account enabled</label>
      <label>Email address <input class="person-email" type="email" maxlength="254" value="${html(u.email||'')}" placeholder="None"></label>
      <label>Reset password (optional) <input class="person-password" type="password" minlength="12" autocomplete="new-password" placeholder="New password, 12+ chars"></label>
      ${u.role==='admin'?'':`<div class="person-quota"><label>Pages a month <select class="person-quota-mode"><option value="default" ${u.monthly_pages===null?'selected':''}>Default</option><option value="none" ${u.monthly_pages===-1?'selected':''}>No limit</option><option value="custom" ${ownLimit(u)?'selected':''}>Their own</option></select></label><input class="person-quota-pages ${ownLimit(u)?'':'hidden'}" type="number" min="0" max="100000" value="${ownLimit(u)?u.monthly_pages:''}" aria-label="Pages a month"><small>${count(u.quota.used)} printed this month${u.quota.limit===null?'':` of ${count(u.quota.limit)}`}</small></div>`}
      <div class="person-actions"><button class="button outline-button person-save">Save changes</button><button class="small-action danger person-delete">Delete user</button></div>
      <div class="person-actions person-links">${u.email&&state.mailReady?`<button type="button" class="small-action" data-person-link="send">${u.pending?'Email a new invitation':'Email a password link'}</button>`:''}<button type="button" class="small-action" data-person-link="copy">${u.pending?'Copy a new invitation link':'Copy a password link'}</button></div>
      <div class="invite-result hidden"></div>
    </div>`).join('')||'<p>No accounts found.</p>';
    $$('[data-person-link]').forEach(button=>button.onclick=async()=>{
      const row=button.closest('.person'),username=row.dataset.person,send=button.dataset.personLink==='send';
      button.disabled=true;
      try{showLinkResult(row.querySelector('.invite-result'),username,await api(`/api/admin/users/${encodeURIComponent(username)}/link`,{method:'POST',body:{send}}));}
      catch(e){problem(e);}
      finally{button.disabled=false;}
    });
    $$('.person-save').forEach(button=>button.onclick=async()=>{
      let row=button.closest('.person'),username=row.dataset.person;
      let body={role:row.querySelector('.person-role').value,enabled:row.querySelector('.person-enabled').checked,email:row.querySelector('.person-email').value.trim()};
      let newPassword=row.querySelector('.person-password').value;
      if(newPassword)body.password=newPassword;
      try{
        await api(`/api/admin/users/${encodeURIComponent(username)}`,{method:'PATCH',body});
        const mode=row.querySelector('.person-quota-mode')?.value;
        if(mode){
          const pages=mode==='default'?null:mode==='none'?-1:Number(row.querySelector('.person-quota-pages').value||0);
          await api(`/api/admin/users/${encodeURIComponent(username)}/quota`,{method:'PUT',body:{monthly_pages:pages}});
        }
        toast('Updated '+username);await loadUsers();
      }catch(e){problem(e);}
    });
    $$('.person-quota-mode').forEach(select=>select.onchange=()=>select.closest('.person').querySelector('.person-quota-pages').classList.toggle('hidden',select.value!=='custom'));
    $$('.person-delete').forEach(button=>button.onclick=async()=>{
      const username=button.closest('.person').dataset.person;
      if(!confirm(`Delete account "${username}"? Its scans will remain available to administrators.`))return;
      try{await api(`/api/admin/users/${encodeURIComponent(username)}`,{method:'DELETE'});toast('Account deleted');await loadUsers();}catch(e){problem(e);}
    });
  }catch(e){problem(e);}
}
// An invitation or password link, emailed or ready to copy and pass on.
function showLinkResult(box,username,result){
  const link=result.link||`${location.origin}/#password=${result.token}`,invite=result.purpose==='invite';
  const valid=invite?'It works once, within 48 hours.':'It works once, within one hour.';
  const said=result.emailed?`Emailed ${invite?'an invitation':'a password link'} to ${html(result.email)}. ${valid} You can also pass on this link yourself:`
    :`${result.email_error?`Couldn’t email it: ${html(result.email_error)} `:''}Send ${html(username)} this link (by message, or however you like). ${valid}`;
  box.innerHTML=`<p>${said}</p><div class="invite-link"><input readonly value="${html(link)}" aria-label="Link for ${html(username)}"><button type="button" class="button outline-button">Copy</button></div>`;
  box.classList.toggle('error',!!result.email_error);box.classList.remove('hidden');
  const input=box.querySelector('input');
  box.querySelector('button').onclick=async()=>{
    input.select();
    try{await navigator.clipboard.writeText(link);}catch{document.execCommand('copy');}  // Plain http has no clipboard API
    toast('Link copied.');
  };
}
function updateNewUserWay(){
  const invite=document.querySelector('input[name=new-way]:checked').value==='invite';
  $('#new-password-box').classList.toggle('hidden',invite);$('#new-password').required=!invite;
  $('#add-user-submit').textContent=invite?'Invite →':'Create account →';
}
$$('input[name=new-way]').forEach(radio=>radio.onchange=updateNewUserWay);
$('#add-user-form').onsubmit=async e=>{
  e.preventDefault();
  const invite=document.querySelector('input[name=new-way]:checked').value==='invite';
  const body={username:$('#new-username').value.trim(),role:$('#new-role').value,email:$('#new-email').value.trim()||null};
  try{
    if(invite){
      const result=await api('/api/admin/invitations',{method:'POST',body});
      showLinkResult($('#invite-result'),body.username,result);
    }else{
      await api('/api/admin/users',{method:'POST',body:{...body,password:$('#new-password').value}});
      $('#invite-result').classList.add('hidden');toast('Created account '+body.username);
    }
    $('#add-user-form').reset();updateNewUserWay();await loadUsers();
  }catch(err){problem(err);}
};
// --- Print statistics (administrators) -----------------------------------------
const STATS_KEPT={90:'for 3 months',365:'for 1 year',730:'for 2 years',1825:'for 5 years',0:'until you delete them'};
const count=value=>Number(value||0).toLocaleString();
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
// First steps for administrators, ticked off by what is actually set up.
const CHECKLIST_ACTIONS={
  printer:['Add a printer',async()=>{await openDeviceManager();$('#add-printer-panel').scrollIntoView({behavior:'smooth'});}],
  print:['Print a test page',async()=>{await openDeviceManager();$('#device-printer-list').scrollIntoView({behavior:'smooth'});}],
  scanner:['Add a scanner',async()=>{await openDeviceManager();$('#device-scanner-list').scrollIntoView({behavior:'smooth'});}],
  people:['Add people',async()=>{workspace(false,true);await loadUsers();}],
  airprint:['How',()=>window.open('https://github.com/kmmukut/folio-scan-studio/blob/main/docs/DEPLOYMENT.md#airprint-phones-and-computers','_blank','noopener')],
  backup:['Back up',async()=>{await openDeviceManager();$('#backup-panel').scrollIntoView({behavior:'smooth'});}],
  email:['Set up',async()=>{await openDeviceManager();$('#email-panel').scrollIntoView({behavior:'smooth'});}],
};
async function loadChecklist(){
  const list=await api('/api/admin/checklist');
  const required=list.steps.filter(s=>!s.optional),done=required.filter(s=>s.done).length;
  $('#setup-checklist').classList.toggle('hidden',list.dismissed);
  if(list.dismissed)return;
  $('#checklist-progress').textContent=list.complete?'The essentials are done. The optional steps make Folio more useful.':`${done} of ${required.length} essential steps done.`;
  $('#checklist-steps').innerHTML=list.steps.map(s=>`<li class="${s.done?'done':''}"><span class="checklist-mark">${s.done?icon('check'):''}</span><span class="checklist-title">${html(s.title)}${s.optional?' <small>optional</small>':''}</span>${s.done?'':`<button type="button" class="small-action" data-checklist="${html(s.id)}">${html(CHECKLIST_ACTIONS[s.id]?.[0]||'Open')}</button>`}</li>`).join('');
  $$('[data-checklist]').forEach(b=>b.onclick=()=>Promise.resolve(CHECKLIST_ACTIONS[b.dataset.checklist]?.[1]()).catch(problem));
}
$('#checklist-hide').onclick=async()=>{try{await api('/api/admin/checklist',{method:'PUT',body:{dismissed:true}});$('#setup-checklist').classList.add('hidden');toast('Hidden. Everything stays available under Devices and People & access.');}catch(e){problem(e);}};
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await signIn();}catch(err){problem(err);}};
$('#setup-form').onsubmit=async e=>{
  e.preventDefault();
  if($('#setup-password').value!==$('#setup-repeat').value){toast('The two passwords are different.',true);return;}
  try{
    const status=await api('/api/setup/admin',{method:'POST',body:{username:$('#setup-name').value.trim(),password:$('#setup-password').value}});
    state.setup={setup_required:false,setup_open:false};
    state.user=status.username;state.role=status.role;state.csrf=status.csrf;$('#setup-password').value='';$('#setup-repeat').value='';
    await setupAdmin();
  }catch(err){problem(err);}
};
// "Forgot your password?" and the links in invitation and reset emails (0.20).
function authOnly(...ids){
  ['setup-form','setup-closed','login-form','forgot-open','login-guest-button','login-guest-status','forgot-form','forgot-sent',
   'link-form','link-problem','auth-back'].forEach(id=>$('#'+id).classList.toggle('hidden',!ids.includes(id)));
}
function authHeading(title,intro){$('#auth-title').innerHTML=title;$('#auth-intro').textContent=intro;}
$('#forgot-open').onclick=()=>{
  authOnly('forgot-form','auth-back');
  authHeading('A new<br><em>password.</em>','Enter your username or email address. If your account has an email address, Folio sends it a link to choose a new password.');
  $('#forgot-login').value=$('#login-name').value;$('#forgot-login').focus();
};
$('#forgot-form').onsubmit=async e=>{
  e.preventDefault();const button=e.currentTarget.querySelector('button');button.disabled=true;
  try{
    const {detail}=await api('/api/auth/forgot',{method:'POST',body:{login:$('#forgot-login').value.trim()}});
    $('#forgot-sent').textContent=detail;authOnly('forgot-sent','auth-back');
  }catch(err){problem(err);}
  finally{button.disabled=false;}
};
$('#auth-back').onclick=()=>{history.replaceState(null,'',location.pathname+location.search);showLogin();};
let passwordToken=null;
// A link from an email: show its form (false), or say why it can't be used and
// let the caller carry on (true) if someone is signed in.
async function followPasswordLink(signedIn){
  const linkProblem=await openPasswordLink(location.hash.slice(10));
  if(!linkProblem)return false;
  if(!signedIn){showLogin();$('#link-problem').textContent=linkProblem;$('#link-problem').classList.remove('hidden');return false;}
  toast(linkProblem,true);
  return true;
}
// Pasted into a tab where Folio is already open: only the fragment changes.
window.addEventListener('hashchange',()=>{
  if(!location.hash.startsWith('#password='))return;
  const signedIn=!!state.user;
  followPasswordLink(signedIn).then(carryOn=>{if(carryOn)setViews('app-view');}).catch(problem);
});
// Shows the form for a link from an email; returns why it can't, or null.
async function openPasswordLink(token){
  passwordToken=token;
  history.replaceState(null,'',location.pathname+location.search);  // Keep the token out of the address bar and history
  setViews('auth-view');
  try{
    const link=await api('/api/auth/link',{method:'POST',body:{token}});
    $('#link-username').value=link.username;
    authOnly('link-form');
    if(link.purpose==='invite')authHeading(`Welcome,<br><em>${html(link.username)}.</em>`,`Choose a password for your Folio account. Your username is ${link.username}.`);
    else authHeading(`A new password,<br><em>${html(link.username)}.</em>`,'Choose a new password. Everywhere else you were signed in, you will be signed out.');
    $('#link-password').focus();
    return null;
  }catch(err){return err.message;}
}
$('#link-form').onsubmit=async e=>{
  e.preventDefault();
  if($('#link-password').value!==$('#link-repeat').value){toast('The two passwords are different.',true);return;}
  try{
    const status=await api('/api/auth/link/password',{method:'POST',body:{token:passwordToken,password:$('#link-password').value}});
    passwordToken=null;$('#link-password').value='';$('#link-repeat').value='';
    state.user=status.username;state.role=status.role;state.csrf=status.csrf;
    if(state.role==='printer')await setupPrintOnly();else await setupAdmin();
    toast(`Your password is saved. Welcome, ${status.username}.`);
  }catch(err){problem(err);}
};
$('#login-guest-button').onclick=openGuest;
$('#logout-button').onclick=signOut;
$('#top-logout').onclick=signOut;
$('#workspace-tab').onclick=()=>{workspace(false);if(state.role==='admin')loadChecklist().catch(()=>{});};
$('#print-tab').onclick=()=>showPrintDesk().catch(problem);
$('#devices-tab').onclick=()=>openDeviceManager().catch(problem);
$('#stats-tab').onclick=()=>openStats().catch(problem);
$('#stats-period').onchange=()=>loadStats().catch(problem);
$('#stats-save').onclick=()=>saveStatsSettings().catch(problem);
$('#stats-clear').onclick=()=>clearStats().catch(problem);
$('#devices-refresh').onclick=()=>refreshDeviceManager(true).catch(problem);
$('#diagnostics-run').onclick=()=>runDiagnostics().catch(problem);
// Destinations: where finished documents can be sent (Paperless-ngx, folders).
async function loadSendTargets(){
  const list=await api('/api/destinations');
  $('#send-control').classList.toggle('hidden',!list.length);
  $('#send-destination').innerHTML=list.map(d=>`<option value="${html(d.id)}">${html(d.name)}</option>`).join('');
}
$('#send-button').onclick=async()=>{
  if(!state.job?.pages.length){toast('Scan at least one page to send.');return;}
  const button=$('#send-button'),label=button.innerHTML,select=$('#send-destination'),name=select.selectedOptions[0]?.textContent;
  button.disabled=true;
  try{
    const title=$('#document-title').value.trim()||state.job.title;
    const started=await api(`/api/jobs/${state.job.id}/send`,{method:'POST',body:{destination:select.value,title,ocr:$('#pdf-ocr').checked}});
    const done=await followTask(started.task,{onUpdate:task=>{button.innerHTML=`${icon('arrow')} ${task.status==='queued'?'Waiting…':'Sending…'}`;}});
    toast(`Sent to ${name}. ${done.result.detail}.`);
    state.job=await api(`/api/jobs/${state.job.id}`);renderDocument();
  }catch(error){problem(error);}
  finally{button.disabled=false;button.innerHTML=label;}
};
function destinationKind(){return $('#destination-kind').value;}
function destinationBody(){
  const kind=destinationKind();
  return {kind,name:$('#destination-name').value.trim(),url:$('#destination-url').value.trim(),token:$('#destination-token').value,
    verify_tls:$('#destination-verify').checked,folder:$('#destination-folder').value.trim(),
    tags:$$('#destination-tags input:checked').map(i=>Number(i.value))};
}
$('#destination-kind').onchange=()=>{
  const paperless=destinationKind()==='paperless';
  $$('.destination-paperless').forEach(e=>e.classList.toggle('hidden',!paperless));
  $$('.destination-folder').forEach(e=>e.classList.toggle('hidden',paperless));
  $('#destination-tags').classList.add('hidden');
  if(!$('#destination-name').value||['Paperless','NAS'].includes($('#destination-name').value))$('#destination-name').value=paperless?'Paperless':'NAS';
};
$('#destination-check').onclick=async()=>{
  try{
    const result=await api('/api/admin/destinations/check',{method:'POST',body:destinationBody()});
    if(destinationKind()==='paperless'){
      $('#destination-tags').innerHTML=result.tags.length?`<p class="inline-note">Tag every document sent here with:</p>${result.tags.map(t=>`<label class="checkline"><input type="checkbox" value="${t.id}"> ${html(t.name)}</label>`).join('')}`:'<p class="inline-note">Paperless has no tags yet.</p>';
      $('#destination-tags').classList.remove('hidden');
      toast('Paperless answered and accepted the token.');
    }else toast('Folio can write to that folder.');
  }catch(e){problem(e);}
};
$('#destination-form').onsubmit=async event=>{
  event.preventDefault();
  try{
    const saved=await api('/api/admin/destinations',{method:'POST',body:destinationBody()});
    toast(`${saved.name} added. Documents can be sent there from the workbench.`);
    $('#destination-form').reset();$('#destination-tags').classList.add('hidden');$('#destination-kind').onchange();
    await loadDestinations();await loadSendTargets();
  }catch(e){problem(e);}
};
// --- Email (0.20) -------------------------------------------------------------------
const EMAIL_PORTS={starttls:587,ssl:465,none:25};
function emailStatus(m){
  if(!m.configured)return 'Not set up yet.';
  const last=m.last?.ok===false?` The last email failed: ${m.last.error}`:m.last?.ok?' The last email was sent fine.':'';
  const missing=[!m.public_url&&'Folio’s address is missing, so emails can’t contain links.',
    !m.admins_with_email&&Object.values(m.notify).some(Boolean)&&'No administrator has an email address yet, so notifications have nowhere to go: add yours under Your account (top right).'].filter(Boolean).join(' ');
  return `Sending through ${m.host}:${m.port}.${last}${missing?' '+missing:''}`;
}
async function loadEmail(){
  const m=await api('/api/admin/mail');
  $('#email-status').textContent=emailStatus(m);
  $('#email-public-url').value=m.public_url||'';$('#email-public-url').placeholder=location.origin;
  $('#email-host').value=m.host;$('#email-security').value=m.security;$('#email-port').value=m.configured?m.port:'';
  $('#email-port').placeholder=EMAIL_PORTS[m.security];
  $('#email-username').value=m.username;$('#email-password').value='';
  $('#email-password').placeholder=m.has_password?'Saved; type to replace':'';
  $('#email-sender').value=m.sender;$('#email-verify').checked=m.verify_tls;$('#email-reset').checked=m.password_reset;
  $('#email-notify').innerHTML=Object.entries(m.notifications).map(([kind,label])=>`<label class="checkline"><input type="checkbox" data-notify="${html(kind)}" ${m.notify[kind]?'checked':''}> ${html(label)}</label>`).join('');
  $('#email-remove').classList.toggle('hidden',!m.configured);
  $('#email-test').disabled=!m.configured;
  state.mailReady=m.configured;
}
$('#email-security').onchange=()=>{
  const port=$('#email-port');port.placeholder=EMAIL_PORTS[$('#email-security').value];
  if(Object.values(EMAIL_PORTS).includes(Number(port.value)))port.value=EMAIL_PORTS[$('#email-security').value];
};
$('#email-form').onsubmit=async event=>{
  event.preventDefault();
  const notify={};$$('[data-notify]').forEach(box=>notify[box.dataset.notify]=box.checked);
  const body={public_url:$('#email-public-url').value.trim(),host:$('#email-host').value.trim(),security:$('#email-security').value,
    port:Number($('#email-port').value)||null,username:$('#email-username').value.trim(),password:$('#email-password').value,
    sender:$('#email-sender').value.trim(),verify_tls:$('#email-verify').checked,password_reset:$('#email-reset').checked,notify};
  try{await api('/api/admin/mail',{method:'PUT',body});await loadEmail();toast('Email settings saved. Send a test email to check them.');}
  catch(err){problem(err);}
};
$('#email-test').onclick=async()=>{
  const me=(await api('/api/admin/mail').catch(()=>({}))).my_email;
  const to=prompt('Send a test email to:',me||'');
  if(!to)return;
  const button=$('#email-test');button.disabled=true;button.textContent='Sending…';
  try{const result=await api('/api/admin/mail/test',{method:'POST',body:{to:to.trim()}});toast(`Test email sent to ${result.sent_to}. Check that it arrived.`);}
  catch(err){problem(err);}
  finally{button.disabled=false;button.textContent='Send a test email';loadEmail().catch(()=>{});}
};
$('#email-remove').onclick=async()=>{
  if(!confirm('Remove the email settings? Invitations can still be passed on as links; “Forgot your password?” and notifications stop.'))return;
  try{await api('/api/admin/mail',{method:'DELETE'});await loadEmail();toast('Email settings removed.');}
  catch(err){problem(err);}
};

async function loadDestinations(){
  const list=await api('/api/admin/destinations');
  $('#destination-list').innerHTML=list.map(d=>`<article class="device-card" data-destination="${html(d.id)}"><header><div><strong>${html(d.name)}</strong><small>${html(d.kind_label)} · ${html(d.kind==='paperless'?d.url:'/destinations/'+d.folder)}${d.tags.length?` · ${d.tags.length} default ${d.tags.length===1?'tag':'tags'}`:''}</small></div></header><div class="device-actions"><button class="danger-action" data-remove-destination>Remove</button></div></article>`).join('')||'<p class="muted">No destinations yet.</p>';
  $$('[data-remove-destination]').forEach(b=>b.onclick=async()=>{
    const id=b.closest('[data-destination]').dataset.destination;
    if(!confirm('Remove this destination? Documents already sent stay where they are.'))return;
    try{await api(`/api/admin/destinations/${id}`,{method:'DELETE'});await loadDestinations();await loadSendTargets();}catch(e){problem(e);}
  });
}
// Backup and restore (administrators, Devices page).
async function showLastBackup(){
  try{const list=await api('/api/admin/checklist');
    $('#backup-status').textContent=list.last_backup_at?`Last backup: ${today(list.last_backup_at)}.`:'No backup yet.';}catch{}
}
$('#backup-run').onclick=async()=>{
  const button=$('#backup-run');button.disabled=true;
  try{
    const started=await api('/api/admin/backup',{method:'POST'});
    const done=await followTask(started.task,{onUpdate:task=>{$('#backup-status').textContent=taskText(task);}});
    const link=document.createElement('a');link.href=`/api/tasks/${done.id}/file`;link.download=done.filename||'';
    document.body.append(link);link.click();link.remove();
    const docs=done.result.documents,printers=done.result.printers.length;
    $('#backup-status').textContent=`Backup ready: ${count(docs)} ${docs===1?'document':'documents'}, ${printers} built-in ${printers===1?'printer':'printers'}, ${(done.result.bytes/1048576).toFixed(1)} MB.`;
  }catch(error){$('#backup-status').textContent='';problem(error);}
  finally{button.disabled=false;}
};
$('#restore-file').onchange=async event=>{
  const file=event.target.files[0];event.target.value='';
  if(!file)return;
  if(!confirm(`Restore “${file.name}”? Folio’s current documents, accounts and settings are replaced by the backup’s (and kept aside in data/.before-restore).`))return;
  const status=$('#restore-status');status.textContent='Uploading…';
  try{
    const started=await api('/api/admin/restore',{method:'POST',body:file,headers:{'Content-Type':'application/gzip'}});
    const done=await followTask(started.task,{onUpdate:task=>{status.textContent=taskText(task);}});
    const failed=Object.entries(done.result.printers_failed||{});
    status.textContent=`Restored the backup from ${today(done.result.manifest.created)}.`+(done.result.printers_restored.length?` Printers set up again: ${done.result.printers_restored.join(', ')}.`:'')+(failed.length?` Not restored: ${failed.map(([n,why])=>`${n} (${why})`).join('; ')}.`:'');
    toast('Backup restored. Reloading…');setTimeout(()=>location.reload(),2500);
  }catch(error){status.textContent='';problem(error);}
};
$('#print-refresh').onclick=()=>loadPrinterStatus().catch(problem);
// Keep a quick refresh right beside the drag-and-drop upload tray control.
// This re-queries CUPS after an administrator changes the driver's trays.
for(const prefix of ['member','guest']){
  $(`#${prefix}-source-refresh`).onclick=async event=>{
    const button=event.currentTarget;
    button.disabled=true;
    try{
      if(prefix==='guest')await refreshGuestPrintQueues();
      else await refreshPrintQueues(true);
      toast('Paper sources refreshed from CUPS.');
    }catch(error){problem(error);}
    finally{button.disabled=false;}
  };
}

$('#users-tab').onclick=async()=>{if(state.role!=='admin')return;workspace(false,true);await Promise.all([loadUsers(),loadQuotaDefault()]);};
async function loadQuotaDefault(){
  const result=await api('/api/admin/quota-default');
  $('#quota-default').value=result.monthly_pages??'';
  $('#quota-warning').classList.toggle('hidden',result.statistics_enabled);
}
$('#quota-default-form').onsubmit=async event=>{
  event.preventDefault();
  const value=$('#quota-default').value.trim();
  try{
    await api('/api/admin/quota-default',{method:'PUT',body:{monthly_pages:value===''?null:Number(value)}});
    toast(value===''?'No monthly limit by default.':`Everyone may print ${count(Number(value))} pages a month unless they have their own limit.`);
    await Promise.all([loadUsers(),loadQuotaDefault()]);
  }catch(e){problem(e);}
};
$('#save-guest').onclick=async()=>{try{let r=await api('/api/admin/guest',{method:'POST',body:{enabled:$('#guest-enable').checked}});state.guestEnabled=r.enabled;toast(r.enabled?'Guest printing enabled':'Guest printing disabled');}catch(e){problem(e);}};
$('#open-guest-view').onclick=openGuest;
$('#guest-back').onclick=closeGuest;
$('#new-job').onclick=()=>newJob().catch(problem);$('#new-job-sidebar').onclick=()=>{workspace(false);newJob().catch(problem);};
$('#delete-current-job').onclick=()=>{const id=state.job?.id;if(id){const btn=document.querySelector(`.delete-job[data-delete-job="${id}"]`);if(btn)btn.click();}};
$('#document-title').onchange=async e=>{if(!state.job)return;try{state.job=await api(`/api/jobs/${state.job.id}`,{method:'PATCH',body:{title:e.target.value.trim()||'Untitled document'}});await refreshJobs();renderDocument();}catch(err){problem(err);}};
$('#export-button').onclick=exportDocument;
for(const group of ['source','mode'])$$(`#${group}-picker button`).forEach(b=>b.onclick=()=>{state.settings[group]=b.dataset.value;updateSettings();});
$('#resolution').onchange=e=>{state.settings.resolution=Number(e.target.value);updateSettings();};
$('#auto-count').onchange=e=>{state.settings.auto=e.target.checked;updateSettings();};
$('#batch-count').onchange=e=>{state.settings.count=Math.max(1,Math.min(100,Number(e.target.value)||1));updateSettings();};
$('#batch-role').onchange=e=>{state.settings.role=e.target.value;updateSettings();};
$('#skip-blank').onchange=e=>{state.settings.skipBlank=e.target.checked;updateSettings();};
$('#remove-blank').onclick=async()=>{
  if(!state.job?.pages.length){toast('There are no pages yet.');return;}
  const button=$('#remove-blank');button.disabled=true;button.textContent='Looking for blank pages…';
  try{
    const result=await api(`/api/jobs/${state.job.id}/remove-blank-pages`,{method:'POST'});
    state.job=result.job;if(!state.job.pages.some(p=>p.id===state.selected))state.selected=state.job.pages[0]?.id||null;
    renderDocument();await refreshJobs();
    const n=result.removed.length;toast(n?`Removed ${n} blank ${n===1?'page':'pages'}. Undo restores them one by one.`:'No blank pages found.');
  }catch(e){problem(e);}
  finally{button.disabled=false;button.textContent='Remove blank pages';}
};
$$('.preset').forEach(b=>b.onclick=()=>{const preset={documents:{source:'ADF',mode:'Color',resolution:300},grayscale:{source:'ADF',mode:'Gray',resolution:200},photos:{source:'Flatbed',mode:'Color',resolution:300}}[b.dataset.preset];state.settings={...state.settings,...preset};updateSettings();$$('.preset').forEach(el=>el.classList.remove('selected'));b.classList.add('selected');});
$('#scan-now').onclick=scanNow;$('#cancel-scan').onclick=async()=>{try{renderScanStatus(await api('/api/scan/cancel',{method:'POST'}));}catch(e){problem(e);}};
$('#rotate-right').onclick=()=>{let p=currentPage();if(p)patchPage({rotation:((p.rotation||0)+90)%360});};
$('#rotate-left').onclick=()=>{let p=currentPage();if(p)patchPage({rotation:((p.rotation||0)+270)%360});};
$('#crop-button').onclick=startCrop;
$('#auto-straighten').onclick=async()=>{
  const p=currentPage();if(!p)return;resetSuggestion();
  const button=$('#auto-straighten');button.disabled=true;
  try{
    const result=await api(`/api/jobs/${state.job.id}/pages/${p.id}/auto-straighten`,{method:'POST'});
    if(!result.angle){toast(result.message);return;}
    if(currentPage()?.id!==p.id)return;
    state.autoSuggestion={pageId:p.id,angle:result.angle};
    $('#suggestion-angle').textContent=`Suggested correction ${result.angle>0?'+':''}${displayAngle(result.angle)} · ${result.line_count} lines`;
    $('#auto-suggestion').classList.remove('hidden');
    previewRotation(result.angle);toast('Review the preview, then apply or dismiss the suggestion.');
  }catch(e){problem(e);}finally{button.disabled=false;}
};
$('#suggestion-apply').onclick=()=>{
  if(state.autoSuggestion?.pageId!==currentPage()?.id)return;
  const angle=state.autoSuggestion.angle;patchPage({deskew:angle});
};
$('#suggestion-dismiss').onclick=resetSuggestion;
function liveAngle(value){
  resetSuggestion();$('#deskew-angle').value=String(value);$('#angle-output').textContent=displayAngle(value);
  $('#large-angle').value=String(value);$('#large-angle-output').textContent=displayAngle(value);
  previewRotation(value);
}
$('#deskew-angle').oninput=e=>liveAngle(e.target.value);
$('#deskew-angle').onchange=e=>patchPage({deskew:Number(e.target.value)});
$('#large-angle').oninput=e=>liveAngle(e.target.value);
$('#large-angle').onchange=e=>patchPage({deskew:Number(e.target.value)});
for(const id of ['brightness','contrast']){
  $('#'+id).oninput=previewTone;
  $('#'+id).onchange=e=>patchPage({[id]:Number(e.target.value)});
}
$('#auto-levels').onchange=e=>patchPage({auto_levels:e.target.checked});
$('#reset-tones').onclick=()=>patchPage({brightness:0,contrast:0,auto_levels:false});
$('#enlarge-preview').onclick=()=>{
  if(!currentPage())return;
  largeView.zoom=1;$('#preview-dialog').showModal();
  setLargePanMode(true);refreshLargePreview();toggleGuides();
};
$('#small-grid').onchange=()=>{$('#large-grid').checked=$('#small-grid').checked;toggleGuides();};
$('#large-grid').onchange=()=>{$('#small-grid').checked=$('#large-grid').checked;toggleGuides();};
$('#large-close').onclick=()=>$('#preview-dialog').close();
$('#zoom-out').onclick=()=>setZoom(largeView.zoom-.25);
$('#zoom-in').onclick=()=>setZoom(largeView.zoom+.25);
$('#zoom-fit').onclick=()=>{setZoom(1);const v=$('#large-viewport');v.scrollLeft=0;v.scrollTop=0;};
$('#large-pan-toggle').onclick=()=>setLargePanMode(!largeView.pan);
wirePanViewport($('#large-viewport'),()=>largeView.pan);
setLargePanMode(true);
$('#view-reset').onclick=restoreOriginalScan;
$('#preview-dialog').onclose=()=>{const img=$('#large-preview-image');img.dataset.src='';img.removeAttribute('src');};
$('#large-viewport').addEventListener('wheel',e=>{
  if(!e.ctrlKey&&!e.metaKey)return;e.preventDefault();setZoom(largeView.zoom+(e.deltaY>0?-.1:.1));
},{passive:false});

$('#page-size').onchange=e=>patchPage({scale:Number(e.target.value)});
$('#page-output').onchange=e=>patchPage({export_mode:e.target.value});
$('#duplicate').onclick=()=>postPage('duplicate','Page duplicated');
$('#delete').onclick=async()=>{let p=currentPage();if(!p)return;try{state.job=await api(`/api/jobs/${state.job.id}/pages/${p.id}`,{method:'DELETE'});state.selected=state.job.pages[0]?.id||null;renderDocument();toast('Page removed. Use Restore deleted page to recover it.');}catch(e){problem(e);}};
$('#reset-edits').onclick=restoreOriginalScan;
$('#undo-delete').onclick=async()=>{if(!state.job?.deleted_pages?.length)return;const lastDeletedId=state.job.deleted_pages.at(-1)?.page?.id;try{state.job=await api(`/api/jobs/${state.job.id}/undo-delete`,{method:'POST'});state.selected=state.job.pages.some(p=>p.id===lastDeletedId)?lastDeletedId:(state.job.pages[0]?.id||null);renderDocument();toast('Deleted page restored to its previous position.');}catch(e){problem(e);}};
$('#prev-page').onclick=()=>selectAdjacentPage(-1);$('#next-page').onclick=()=>selectAdjacentPage(1);
$('#interleave-button').onclick=interleave;
$('#crop-close').onclick=()=>$('#crop-dialog').close();
$('#crop-reset').onclick=()=>{$('#crop-dialog').close();patchPage({reset_crop:true});};
$('#crop-save').onclick=finishCrop;
$('#crop-zoom-out').onclick=()=>setCropZoom(cropView.zoom-.25);
$('#crop-zoom-in').onclick=()=>setCropZoom(cropView.zoom+.25);
$('#crop-zoom-fit').onclick=()=>setCropZoom(1,false);
$('#crop-pan-toggle').onclick=()=>setCropPanMode(!cropView.pan);
wirePanViewport($('#crop-canvas'),()=>cropView.pan);
$('#crop-image-wrap').addEventListener('pointerdown',beginCropSelection);
$('#crop-image-wrap').addEventListener('pointermove',moveCropSelection);
$('#crop-image-wrap').addEventListener('pointerup',endCropSelection);
$('#crop-image-wrap').addEventListener('pointercancel',endCropSelection);
$('#crop-dialog').onclick=e=>{if(e.target===$('#crop-dialog'))$('#crop-dialog').close();};
wireDropZone({key:'guest',zoneId:'upload-area',inputId:'guest-print-file'});
wireDropZone({key:'member',zoneId:'member-upload-area',inputId:'member-print-file'});
$('#guest-print-form').onsubmit=e=>submitPrintFile(e,true);
$('#member-print-form').onsubmit=e=>submitPrintFile(e,false);
// ===== Folio 0.9: device manager and capability-driven controls =====
function currentScanner(){return state.scanners.find(s=>s.id===state.settings.scanner_id)||null;}
async function refreshScannerChoices(){
  if(state.role==='printer')return;
  const result=await api('/api/scanners');state.scanners=result.scanners||[];
  const select=$('#scan-device'), previous=state.settings.scanner_id;
  select.innerHTML=state.scanners.map(d=>`<option value="${html(d.id)}">${html(d.name)}${d.connected?'':' · offline'}</option>`).join('')
    || '<option value="">Add a scanner in Devices</option>';
  if(state.scanners.some(x=>x.id===previous))select.value=previous;
  else select.value=result.default||state.scanners[0]?.id||'';
  state.settings.scanner_id=select.value;
  applyScannerCapabilities();
}
function applyScannerCapabilities(){
  const device=currentScanner(),caps=device?.capabilities||{};
  const sourceList=caps.sources?.length?caps.sources:(device?.kind==='existing'?['ADF','Flatbed']:[]);
  const modeList=caps.modes?.length?caps.modes:(device?.kind==='existing'?['Color','Gray']:[]);
  const dpiList=caps.resolutions?.length?caps.resolutions:(device?.kind==='existing'?[75,100,150,200,300]:[]);
  for(const [name,values] of [['source',sourceList],['mode',modeList]]){
    const picker=$(`#${name}-picker`);
    picker.innerHTML=values.map(value=>`<button type="button" data-value="${html(value)}" class="${state.settings[name]===value?'selected':''}">${html(({ADF:'Feeder',Flatbed:'Glass',Gray:'Gray',Color:'Color'})[value]||value)}</button>`).join('');
    if(!values.includes(state.settings[name]))state.settings[name]=values[0]||'';
    picker.querySelectorAll('button').forEach(btn=>btn.onclick=()=>{state.settings[name]=btn.dataset.value;updateSettings();});
  }
  const dpi=$('#resolution');dpi.innerHTML=dpiList.map(n=>`<option value="${n}">${n} dpi</option>`).join('');
  if(!dpiList.includes(Number(state.settings.resolution)))state.settings.resolution=dpiList.includes(300)?300:(dpiList[0]||0);
  const live=!!device?.connected;
  $('#device-led').classList.toggle('on',live);
  $('#device-status').textContent=device?`${device.name} · ${live?'connected':'offline'}`:'No scanner available';
  $('#rail-scanner-name').textContent=device?.name||'No scanner';
  $('#footer-scanner-label').textContent=`SCANNER: ${device?.name||'NOT SELECTED'}`;
  $('#scan-device-info').textContent=live?`${device.protocol||'SANE'} · ${sourceList.join(' / ')} · ${dpiList.length?Math.max(...dpiList)+' dpi':''}`:'Scanner unavailable. Visit Devices to test the connection.';
  $('#scan-now').disabled=!live||state.scanning;
  updateSettings();
}
$('#scan-device').onchange=e=>{state.settings.scanner_id=e.target.value;applyScannerCapabilities();};

const printQueueData={member:null,guest:null};
async function refreshPrintQueues(refresh=false){
  const info=await api(`/api/printers${refresh?'?refresh=true':''}`);printQueueData.member=info;
  for(const prefix of ['member','saved'])updatePrintQueueSelect(prefix,info);
  return info;
}
async function refreshGuestPrintQueues(){
  const info=await api('/api/guest/printers');printQueueData.guest=info;
  updatePrintQueueSelect('guest',info);return info;
}
function updatePrintQueueSelect(prefix,info){
  const select=$(`#${prefix}-printer`);
  const desired=select.value||localStorage.getItem('folio.print.lastQueue')||info.default;
  const list=info.printers||[];
  select.innerHTML=list.map(q=>`<option value="${q.ready?html(q.id):''}">${html(q.label||q.queue)}${q.ready?'':' · unavailable'}</option>`).join('');
  if(!list.length)select.innerHTML='<option value="">No permitted CUPS printers</option>';
  const found=list.find(q=>q.id===desired&&q.ready);
  select.value=found?.id||list.find(q=>q.ready)?.id||'';
  select.onchange=()=>{
    if(select.value)localStorage.setItem('folio.print.lastQueue',select.value);
    refreshPrintOptions(prefix);
    if(prefix==='member')loadPrinterStatus().catch(problem);
  };
  refreshPrintOptions(prefix);
}
function refreshPrintOptions(prefix){
  const info=printQueueData[prefix==='guest'?'guest':'member'];
  const queue=$(`#${prefix}-printer`).value;
  const current=info?.printers?.find(q=>q.id===queue);
  const caps=current?.capabilities||{};
  const color=$(`#${prefix}-color`);
  for(const option of color.options){
    option.disabled=option.value==='color'&&caps.color===false;
    if(option.disabled&&option.value===color.value)color.value='auto';
  }
  const sides=$(`#${prefix}-sides`);
  for(const option of sides.options){
    option.disabled=option.value!=='one-sided'&&caps.duplex===false;
    if(option.disabled&&option.value===sides.value)sides.value='one-sided';
  }
  // Unlike page size, input trays have vendor-specific names. Use only
  // choices reported for this exact CUPS server and printer queue.
  const source=$(`#${prefix}-source`), selectedSource=source.value;
  const discovered=caps.paper_sources||[];
  source.replaceChildren();
  const automatic=document.createElement('option');
  automatic.value='default'; automatic.textContent='Printer default';
  source.append(automatic);
  for(const tray of discovered){
    if(typeof tray.value!=='string'||typeof tray.label!=='string')continue;
    const option=document.createElement('option');option.value=tray.value;
    option.textContent=tray.label+(tray.is_default?' · queue default':'');
    source.append(option);
  }
  source.value=discovered.some(item=>item.value===selectedSource)?selectedSource:'default';
  // The selector is always visible next to the upload dropzone. Do not
  // silently hide it when a queue's driver reports no InputSlot/media-source.
  // Selecting Printer default is still legitimate in that situation.
  source.disabled=!current?.ready;
  const hint=$(`#${prefix}-source-hint`);
  if(hint){
    const printerLabel=current?.label||current?.queue||'this printer';
    hint.textContent=!current?.ready
      ? 'Select an available CUPS printer to load its paper sources.'
      :discovered.length
        ? `${discovered.length} paper source${discovered.length===1?'':'s'} reported by ${printerLabel}. Choose a tray, or leave Printer default.`
        : `No individual paper trays reported by CUPS for ${printerLabel}. Printer default will be used. Check the queue driver’s InputSlot or media-source setting in CUPS, then refresh.`;
  }
  source.title=discovered.length?'Paper sources reported by CUPS':'CUPS did not report individual paper sources for this queue';
  const media=$(`#${prefix}-media`);
  if(caps.media?.length){
    // 'Env10' in PPDs and 'COM10' in newer IPP drivers are the same size.
    // The backend provides authoritative queue-specific canonical aliases.
    const driverMap=caps.media_map||{};
    const known=Object.keys(driverMap).length>0;
    const fallback=new Set(caps.media.map(v=>v.toLowerCase()));
    for(const opt of media.options){
      opt.disabled=opt.value!=='default' && !(known?Object.hasOwn(driverMap,opt.value):fallback.has(opt.value.toLowerCase()));
      if(opt.disabled&&media.value===opt.value)media.value='default';
      opt.title=opt.disabled?'Not advertised by this CUPS printer queue':'';
    }
  }else for(const opt of media.options){opt.disabled=false;opt.title='';}
  updatePaperSizeHint(prefix);
}
async function openDeviceManager(){
  if(state.role!=='admin')return;
  workspace(false,false,true);
  showLastBackup();loadDestinations().catch(problem);loadEmail().catch(problem);
  await refreshDeviceManager();
}
async function refreshDeviceManager(refresh=false){
  const [result,servers]=await Promise.all([api(`/api/admin/devices${refresh?'?refresh=true':''}`),api('/api/admin/cups/servers')]);
  state.cupsServers=servers;renderCupsAdmin(servers);
  const managed=new Set(servers.filter(s=>s.admin.connected).map(s=>s.server));
  $('#device-cups-server').textContent=`Print servers · ${(result.cups_servers||[]).map(serverName).join(', ')||'None'} · ${result.printers.filter(x=>x.ready).length} printers ready`;
  const attach=$('#attach-scanner-select');
  attach.innerHTML=(result.unregistered||[]).map(d=>`<option value="${html(d.sane_name)}">${html(d.description||d.sane_name)}</option>`).join('')||'<option value="">No unregistered SANE devices detected</option>';
  $('#attach-scanner-form button[type=submit]').disabled=!(result.unregistered||[]).length;
  $('#device-scanner-list').innerHTML=result.scanners.map(s=>{
    const caps=s.capabilities||{};const tags=[s.protocol||'SANE',...(caps.sources||[]),...(caps.modes||[]),caps.resolutions?.length?Math.max(...caps.resolutions)+' DPI':'Options unavailable'];
    return `<article class="device-card ${s.id===result.default_scanner?'current':''}" data-scanner="${html(s.id)}">
      <header><div><strong>${html(s.name)}</strong><small>${html(s.ip||s.sane_name||s.hint||'Existing configuration')}</small></div><span class="device-state ${s.connected?'':'off'}">${s.connected?'Connected':'Offline'}</span></header>
      <div class="device-tags">${tags.map(x=>`<span>${html(x)}</span>`).join('')}</div>
      <div class="device-actions"><button data-scanner-action="test">Test</button><button data-scanner-action="edit">Edit</button>${s.id!==result.default_scanner?'<button data-scanner-action="default">Make default</button>':''}<button data-scanner-action="toggle">${s.enabled?'Disable':'Enable'}</button>${s.kind==='managed'?'<button data-scanner-action="delete">Remove</button>':''}</div></article>`;
  }).join('')||'<p>No scanner configured.</p>';
  $('#device-printer-list').innerHTML=result.printers.map(q=>{
    const settings=q.settings||{},caps=q.capabilities||{};
    const tags=[caps.color===true?'Color':caps.color===false?'Monochrome':'Color unknown',caps.duplex===true?'Duplex':caps.duplex===false?'Simplex':'Duplex unknown',caps.media?.length?caps.media.length+' paper sizes':'Sizes unknown',caps.trays?.length?caps.trays.length+' trays':null].filter(Boolean);
    return `<article class="device-card ${result.default_printer===q.id?'current':''}" data-printer="${html(q.id)}">
       <header><div><strong>${html(q.queue)}</strong><small>${html(q.server_label||q.server)} · ${html(q.status||'')}</small></div><span class="device-state ${q.ready?'':'off'}">${q.ready?'Ready':/disabled/i.test(q.status||'')?'Paused':'Offline'}</span></header>
       <div class="device-tags">${tags.map(x=>`<span>${html(x)}</span>`).join('')}</div>
       <div class="device-access">${[['enabled','Enabled'],['guest','Guest printing'],['member','Members'],['printer','Printer-only accounts']].map(([flag,label])=>`<label><input type="checkbox" data-printer-flag="${flag}" ${settings[flag]?'checked':''}> ${label}</label>`).join('')}</div>
       <div class="device-actions"><button data-printer-action="save" ${q.status==='Not found in CUPS'?'disabled':''}>Save permissions</button>${result.default_printer!==q.id?`<button data-printer-action="default" ${q.status==='Not found in CUPS'?'disabled':''}>Make default</button>`:''}${q.status!=='Not found in CUPS'?'<button data-queue-action="jobs">Jobs</button><button data-queue-action="test">Test page</button>':''}${managed.has(q.server)&&q.status!=='Not found in CUPS'?`<button data-queue-action="pause">${q.ready?'Pause':'Resume'}</button><button data-queue-action="remove" class="danger-action">Remove</button>`:''}</div>
       <div class="printer-supplies" aria-live="polite"></div>
       <div class="printer-jobs hidden"></div></article>`;
  }).join('')||'<p class="muted">No printers yet. Add one below.</p>';
  $('#device-scanner-list').querySelectorAll('[data-scanner-action]').forEach(btn=>btn.onclick=()=>handleScannerAction(btn).catch(problem));
  $('#device-printer-list').querySelectorAll('[data-printer-action]').forEach(btn=>btn.onclick=()=>handlePrinterAction(btn).catch(problem));
  $('#device-printer-list').querySelectorAll('[data-queue-action]').forEach(btn=>btn.onclick=()=>handleQueueAction(btn).catch(problem));
  for(const q of result.printers)if(q.status!=='Not found in CUPS')loadSupplies(q.id);
}
// Toner, ink and wearing parts, as the printer reports them; nothing if it doesn't.
async function loadSupplies(id){
  const card=document.querySelector(`[data-printer="${CSS.escape(id)}"] .printer-supplies`);
  if(!card)return;
  try{
    const {supplies}=await api(`${printerPath(id)}/supplies`);
    if(!supplies.length){card.innerHTML='';return;}
    const row=s=>`<div class="supply ${s.low?'low':''}"><span>${html(s.name)}</span><span class="supply-meter" role="img" aria-label="${html(s.name)}: ${s.level===null?(s.some_left?'some left':'level unknown'):s.level+' %'}"><span style="width:${s.level??0}%"></span></span><b>${s.level===null?(s.some_left?'Some left':'—'):s.level+' %'}${s.low?' · Low':''}</b></div>`;
    const consumables=supplies.filter(s=>s.consumable),parts=supplies.filter(s=>!s.consumable);
    card.innerHTML=`<h4>Supplies</h4>${consumables.map(row).join('')}${parts.length?`<details${parts.some(s=>s.low)?' open':''}><summary>Parts (${parts.length})</summary>${parts.map(row).join('')}</details>`:''}`;
  }catch{card.innerHTML='';}
}
async function handleScannerAction(btn){
  const card=btn.closest('[data-scanner]'),id=card.dataset.scanner,act=btn.dataset.scannerAction;
  const result=await api('/api/admin/devices');const current=result.scanners.find(s=>s.id===id);
  if(!current)return;
  if(act==='edit'){
    const f=$('#scanner-device-form');f.dataset.editId=id;
    $('#scanner-form-title').textContent=`Edit ${current.name}`;
    $('#device-scanner-name').value=current.name;
    $('#device-scanner-ip').value=current.ip||'';
    $('#device-scanner-protocol').value=current.protocol||'WSD';
    $('#device-scanner-port').value=current.port||8018;
    $('#device-scanner-path').value=current.path||'/wsd/scan';
    $('#device-scanner-default').checked=result.default_scanner===id;
    $('#scanner-form-cancel').classList.remove('hidden');
    $('#scanner-form-save').textContent='Save changes';
    $('#device-scanner-name').focus();
    toast('Edit the endpoint, then save and test your scanner. No Docker restart needed.');return;
  }
  if(act==='test'){
    const response=await api(`/api/admin/scanners/${id}/test`);
    const c=response.capabilities;
    toast(`${current.name}: ${c.available?'SANE ready':'Capabilities unavailable'} · ${(c.modes||[]).join(', ')} · ${(c.resolutions||[]).join(', ')} dpi`,!c.available);return;
  }
  if(act==='delete'&&!confirm(`Remove ${current.name} from Folio? Existing scans remain.`))return;
  if(act==='default')await api(`/api/admin/scanners/${id}`,{method:'PATCH',body:{make_default:true}});
  if(act==='toggle')await api(`/api/admin/scanners/${id}`,{method:'PATCH',body:{enabled:!current.enabled}});
  if(act==='delete')await api(`/api/admin/scanners/${id}`,{method:'DELETE'});
  await refreshDeviceManager();await refreshScannerChoices();toast('Scanner settings saved.');
}
function clearScannerEditor(){
  const f=$('#scanner-device-form');f.reset();delete f.dataset.editId;
  $('#scanner-form-title').textContent='Add a network scanner';
  $('#device-scanner-port').value='8018';$('#device-scanner-path').value='/wsd/scan';
  $('#scanner-form-save').textContent='Add scanner';
  $('#scanner-form-cancel').classList.add('hidden');
}
$('#scanner-form-cancel').onclick=clearScannerEditor;
$('#attach-scanner-form').onsubmit=async event=>{
  event.preventDefault();
  try{
    await api('/api/admin/scanners/attach',{method:'POST',body:{
      name:$('#attach-scanner-name').value,device_name:$('#attach-scanner-select').value}});
    $('#attach-scanner-form').reset();await refreshDeviceManager();await refreshScannerChoices();
    toast('Detected scanner attached to Folio.');
  }catch(error){problem(error);}
};

$('#device-scanner-protocol').onchange=()=>{
  const wsd=$('#device-scanner-protocol').value==='WSD';
  $('#device-scanner-port').value=wsd?8018:80;
  $('#device-scanner-path').value=wsd?'/wsd/scan':'/eSCL';
};
$('#scanner-device-form').onsubmit=async e=>{
  e.preventDefault();const f=e.currentTarget,id=f.dataset.editId;
  const body={name:$('#device-scanner-name').value,ip:$('#device-scanner-ip').value,
      protocol:$('#device-scanner-protocol').value,port:Number($('#device-scanner-port').value),
      path:$('#device-scanner-path').value,make_default:$('#device-scanner-default').checked};
  try{
    await api(id?`/api/admin/scanners/${id}`:'/api/admin/scanners',
              {method:id?'PATCH':'POST',body});
    clearScannerEditor();await refreshDeviceManager();await refreshScannerChoices();
    toast('Scanner saved. Its SANE connection has been updated without restarting Folio. Use Test to verify.');
  }catch(error){problem(error);}
};
async function handlePrinterAction(btn){
  const card=btn.closest('[data-printer]'),queue=card.dataset.printer;
  const body={};card.querySelectorAll('[data-printer-flag]').forEach(box=>body[box.dataset.printerFlag]=box.checked);
  if(btn.dataset.printerAction==='default')body.make_default=true;
  await api(`/api/admin/printers/${encodeURIComponent(queue)}`,{method:'PATCH',body});
  await refreshDeviceManager();await refreshPrintQueues();toast(`Updated ${queue}`);
}

// --- Printer administration (0.11) ---------------------------------------------
const printerPath=id=>`/api/admin/printers/${encodeURIComponent(id)}`;
const serverPath=server=>`/api/admin/cups/servers/${encodeURIComponent(server)}`;
function renderCupsAdmin(servers){
  const builtinCard=s=>`<article class="device-card cups-admin-card builtin" data-cups-server="${html(s.server)}">
    <header><div><strong>Built-in print server</strong><small>Part of Folio · set up and connected automatically</small></div><span class="device-state ${s.reachable?'':'off'}">${s.reachable?'Running':'Starting…'}</span></header>
    <p class="inline-note">${s.announcing?'Its printers are announced on your network (AirPrint/Bonjour), so phones and computers can find them.':'Its printers are not announced on your network. To let phones find them (AirPrint), see “Printing” in the deployment guide.'}</p>${webAdminPanel(s.web_admin||{})}</article>`;
  $('#cups-admin-list').innerHTML=servers.map(s=>s.source==='builtin'?builtinCard(s):`<article class="device-card cups-admin-card" data-cups-server="${html(s.server)}">
    <header><div><strong>${html(s.server)}</strong><small>${s.source==='settings'?'From the CUPS_SERVERS setting':'Added in Folio'} · ${s.admin.connected?`administrator login: ${html(s.admin.username)}${s.admin.encrypted?' (encrypted)':''}`:'no administrator login'}</small></div><span class="device-state ${s.reachable?'':'off'}">${s.reachable?'Reachable':'Not responding'}</span></header>
    ${s.admin.connected?'':`<form class="device-form cups-login-form"><p class="inline-note">To add, pause or remove printers on this server, enter a CUPS administrator login (a user in CUPS’s lpadmin group). It stays on this Folio server and is never shown again.</p><div class="device-form-row"><label>CUPS username <input name="username" autocomplete="off" required maxlength="64"></label><label>Password <input name="password" type="password" autocomplete="new-password" required maxlength="256"></label></div><button class="button outline-button" type="submit">Connect login</button></form>`}
    <div class="device-actions">${s.admin.connected?'<button data-cups-action="disconnect">Disconnect login</button>':''}${s.source==='folio'?'<button data-cups-action="remove" class="danger-action">Remove server</button>':''}</div></article>`).join('')||'<p class="muted">No print server yet. The built-in one is switched off (FOLIO_BUILTIN_CUPS); add the computer that runs CUPS below.</p>';
  $$('.web-admin-form').forEach(form=>form.onsubmit=async event=>{
    event.preventDefault();
    if(form.password.value!==form.repeat.value){toast('The two passwords are not the same.',true);form.repeat.focus();return;}
    const button=form.querySelector('[type=submit]');button.disabled=true;button.textContent='Applying…';
    try{
      const state=await api('/api/admin/cups/builtin/web-admin',{method:'PUT',body:{username:form.username.value.trim(),password:form.password.value}});
      toast(state.pending?'Handed to the print server; it applies the change as soon as it runs.':`CUPS administration is on. Sign in there as ${state.username}.`);
      await refreshDeviceManager();
    }catch(error){problem(error);button.disabled=false;button.textContent=form.dataset.label;}
  });
  $$('[data-web-admin=off]').forEach(button=>button.onclick=async()=>{
    if(!confirm('Turn off CUPS administration? Its login is removed; Folio keeps managing the print server as before.'))return;
    try{await api('/api/admin/cups/builtin/web-admin',{method:'DELETE'});toast('CUPS administration is off.');await refreshDeviceManager();}
    catch(error){problem(error);}
  });
  $$('.cups-login-form').forEach(form=>form.onsubmit=async event=>{
    event.preventDefault();const server=form.closest('[data-cups-server]').dataset.cupsServer,button=form.querySelector('button');
    button.disabled=true;button.textContent='Checking…';
    try{await api(`${serverPath(server)}/login`,{method:'PUT',body:{username:form.username.value,password:form.password.value}});toast(`Administrator login connected for ${server}.`);await refreshDeviceManager();}
    catch(error){problem(error);button.disabled=false;button.textContent='Connect login';}
  });
  $$('[data-cups-action]').forEach(button=>button.onclick=async()=>{
    const server=button.closest('[data-cups-server]').dataset.cupsServer,remove=button.dataset.cupsAction==='remove';
    if(!confirm(remove?`Stop using ${server}? Its printers disappear from Folio; nothing is changed on that server.`:`Forget the administrator login for ${server}? Printing keeps working.`))return;
    try{await api(remove?serverPath(server):`${serverPath(server)}/login`,{method:'DELETE'});await refreshDeviceManager(true);if(remove)await refreshPrintQueues(true);}
    catch(error){problem(error);}
  });
  const connected=servers.filter(s=>s.admin.connected),select=$('#add-printer-server'),previous=select.value;
  $('#add-printer-form').classList.toggle('hidden',!connected.length);
  $('#add-printer-intro').innerHTML=connected.length?'Folio sets the printer up for you, in four steps.':
    (servers.length?'Folio creates the printer in CUPS for you. First connect an administrator login for a print server under <strong>Print servers</strong> below.':
    'Folio creates the printer in CUPS for you. First add the computer that runs CUPS under <strong>Print servers</strong> below, and connect its administrator login.');
  select.innerHTML=connected.map(s=>`<option value="${html(s.server)}">${s.source==='builtin'?'Built-in print server':html(s.server)}</option>`).join('');
  if(connected.some(s=>s.server===previous))select.value=previous;
}
// CUPS's own web pages for the built-in server: off until an administrator chooses a login.
function webAdminPanel(w){
  const port=w.port||631,link=`https://${location.hostname}:${port}/admin`,label=w.enabled?'Change login':'Turn on';
  const state=w.pending?'waiting for the print server…':w.enabled?`on · sign in as ${html(w.username)}`:'off';
  return `<details class="web-admin"${w.error||w.pending?' open':''}><summary>CUPS administration · ${state}</summary>
    <p class="inline-note">${w.enabled?`Open <a href="${html(link)}" target="_blank" rel="noopener">CUPS’s own pages</a> for what Folio doesn’t cover, such as default options, classes and job limits. They work over HTTPS only: the first time, your browser warns that the print server’s certificate is its own (self-signed). Port ${port} must be published to reach them.`
      :'CUPS has its own administration pages, for what Folio doesn’t cover (default options, classes, job limits). They are off. Choose a login to turn them on: it works only over HTTPS, and Folio does not keep the password.'}</p>
    ${w.error?`<p class="print-feedback error">${html(w.error)}</p>`:''}
    <form class="device-form web-admin-form" data-label="${label}"><div class="device-form-row"><label>Username <input name="username" value="${html(w.username||'')}" required minlength="3" maxlength="32" pattern="[a-z][a-z0-9_\\-]{2,31}" title="3–32 lowercase letters, digits, - or _, starting with a letter" autocomplete="off"></label><label>${w.enabled?'New password':'Password'} <input name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></label><label>Repeat it <input name="repeat" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></label></div>
    <div class="device-actions"><button type="submit">${label}</button>${w.enabled?'<button type="button" data-web-admin="off" class="danger-action">Turn off</button>':''}</div></form></details>`;
}
$('#add-server-form').onsubmit=async event=>{
  event.preventDefault();const button=$('#add-server-submit');button.disabled=true;button.textContent='Checking…';
  try{
    const result=await api('/api/admin/cups/servers',{method:'POST',body:{server:$('#add-server-address').value}});
    toast(`${result.server} added. ${result.detail}`);$('#add-server-form').reset();
    await refreshDeviceManager(true);await refreshPrintQueues(true);
  }catch(error){problem(error);}
  finally{button.disabled=false;button.textContent='Add print server';}
};
const addPrinter={found:[],makesFor:null};
// 'manual', a detected device's URI, or '' while searching without a choice.
function selectedSource(){
  if(document.querySelector('input[name=printer-way]:checked').value==='manual')return 'manual';
  return document.querySelector('input[name=found-printer]:checked')?.value||'';
}
function driverKind(){return document.querySelector('input[name=driver-kind]:checked').value;}
function updatePrinterForm(){
  const searching=document.querySelector('input[name=printer-way]:checked').value==='search';
  $('#manual-printer').classList.toggle('hidden',searching);
  $('#printer-search').classList.toggle('hidden',!searching);
  $('#driver-picker').classList.toggle('hidden',driverKind()!=='ppd');
  if(driverKind()==='ppd')loadDriverMakes(addPrinter.found.find(d=>d.uri===selectedSource())).catch(problem);
}
function suggestName(text){return (text.match(/[A-Za-z0-9]+/g)||[]).slice(0,4).join('_').slice(0,60);}
function useFoundPrinter(device){
  $('#add-printer-name').value=suggestName(device.make_model||device.name);
  document.querySelector(`input[name=driver-kind][value=${device.driverless?'everywhere':'ppd'}]`).checked=true;
  updatePrinterForm();
}
$('#find-printers').onclick=async()=>{
  const status=$('#find-printers-status'),button=$('#find-printers');button.disabled=true;status.textContent='Searching… this takes about ten seconds.';
  try{
    const result=await api(`${serverPath($('#add-printer-server').value)}/devices`);addPrinter.found=result.devices;
    $('#found-printers').innerHTML=result.devices.map((d,i)=>`<label class="device-checkbox found-printer"><input type="radio" name="found-printer" value="${html(d.uri)}"${i===0?' checked':''}> <span><strong>${html(d.make_model||d.name)}</strong><small>${html(d.connection)} · ${html(d.uri)}</small></span></label>`).join('');
    status.textContent=result.devices.length?`Found ${result.devices.length}. Not the right one? Choose “Type the printer’s address” instead.`:
      'Nothing found. Printers on a different network (another subnet or VLAN) don’t show up here; type the printer’s address instead.';
    $$('input[name=found-printer]').forEach(radio=>radio.onchange=()=>useFoundPrinter(addPrinter.found.find(d=>d.uri===radio.value)));
    if(result.devices[0])useFoundPrinter(result.devices[0]);
  }catch(error){status.textContent='';problem(error);}
  finally{button.disabled=false;}
};
$$('input[name=printer-way],input[name=driver-kind]').forEach(radio=>radio.onchange=updatePrinterForm);
$('#add-printer-server').onchange=()=>{addPrinter.found=[];addPrinter.makesFor=null;$('#found-printers').innerHTML='';$('#find-printers-status').textContent='';updatePrinterForm();};
async function loadDriverMakes(device){
  const server=$('#add-printer-server').value;
  if(addPrinter.makesFor!==server){
    const {makes}=await api(`${serverPath(server)}/makes`);addPrinter.makesFor=server;
    $('#driver-make').innerHTML='<option value="">Choose…</option>'+makes.map(m=>`<option>${html(m)}</option>`).join('');
  }
  if(device?.make_model){
    const make=[...$('#driver-make').options].find(o=>o.value&&device.make_model.toLowerCase().startsWith(o.value.toLowerCase()));
    if(make&&$('#driver-make').value!==make.value){$('#driver-make').value=make.value;$('#driver-model').value=device.make_model.slice(make.value.length).trim();}
  }
  if($('#driver-make').value)await loadDrivers();
}
let driverSearch=0;
async function loadDrivers(){
  const make=$('#driver-make').value,serial=++driverSearch;if(!make){$('#driver-choice').innerHTML='';return;}
  const result=await api(`${serverPath($('#add-printer-server').value)}/drivers?make=${encodeURIComponent(make)}&model=${encodeURIComponent($('#driver-model').value)}`);
  if(serial!==driverSearch)return;
  $('#driver-choice').innerHTML=result.drivers.map((d,i)=>`<option value="${html(d.ppd)}"${i===0?' selected':''}>${html(d.name)}</option>`).join('');
  $('#driver-count').textContent=result.drivers.length?`${result.drivers.length} of ${result.total} ${make} drivers shown, best match first.`:`No ${make} driver matches that model. Clear the model to see all ${result.total}, or try Generic (PCL or PostScript).`;
}
$('#driver-make').onchange=()=>loadDrivers().catch(problem);
let driverTimer;$('#driver-model').oninput=()=>{clearTimeout(driverTimer);driverTimer=setTimeout(()=>loadDrivers().catch(problem),300);};
$('#add-printer-form').onsubmit=async event=>{
  event.preventDefault();
  const feedback=$('#add-printer-feedback'),button=$('#add-printer-submit'),source=selectedSource();
  if(!source){setPrintFeedback(feedback,'Search for the printer and choose it, or type its address.','error');return;}
  if(source==='manual'&&!$('#add-printer-address').value.trim()){setPrintFeedback(feedback,'Type the printer’s IP address or host name (step 2).','error');$('#add-printer-address').focus();return;}
  let driver=driverKind()==='everywhere'?'everywhere':$('#driver-choice').value;
  if(!driver){setPrintFeedback(feedback,'Choose a driver, or use Driverless.','error');return;}
  if(driver==='everywhere'){
    button.disabled=true;
    try{driver=await checkDriverless(source,feedback);}finally{button.disabled=false;}
    if(!driver)return;
  }
  const body={name:$('#add-printer-name').value.trim(),driver,location:$('#add-printer-location').value.trim(),
    make_default:$('#add-printer-default').checked,guest:$('#add-printer-guest').checked,
    ...(source==='manual'?{connection:$('#add-printer-connection').value,address:$('#add-printer-address').value.trim()}:{device_uri:source})};
  button.disabled=true;
  try{
    const started=await api(`${serverPath($('#add-printer-server').value)}/printers`,{method:'POST',body});
    const {result}=await followTask(started.task,{onUpdate:task=>setPrintFeedback(feedback,taskText(task),'info')});
    setPrintFeedback(feedback,`${result.queue} is ready${result.state?.driver?` (${result.state.driver})`:''}. Use Test page on its card to check it.`,'ok');
    $('#add-printer-form').reset();addPrinter.found=[];$('#found-printers').innerHTML='';$('#find-printers-status').textContent='';updatePrinterForm();
    await refreshDeviceManager(true);await refreshPrintQueues(true);
  }catch(error){setPrintFeedback(feedback,error.message,'error');}
  finally{button.disabled=false;}
};
// Driverless printing needs PDF or PWG raster; some printers that offer only
// AirPrint's raster print it as garbage. Ask the printer first, and offer a driver.
async function checkDriverless(source,feedback){
  const target=source==='manual'?($('#add-printer-connection').value==='ipp'?{connection:'ipp',address:$('#add-printer-address').value.trim()}:null)
    :(/^ipps?:\/\//i.test(source)?{device_uri:source}:null);
  if(!target)return 'everywhere';
  setPrintFeedback(feedback,'Asking the printer which formats it accepts…','info');
  let check;
  try{check=await api(`${serverPath($('#add-printer-server').value)}/driverless-check`,{method:'POST',body:target});}
  catch{return 'everywhere';}  // Can't tell; CUPS will try
  if(!check.checked||check.full)return 'everywhere';
  const printer=check.make_model||'This printer';
  const why=check.formats.includes('image/urf')?'offers driverless printing only in Apple’s AirPrint format (no PDF or PWG raster), and some printers print that as garbled pages':
    'doesn’t accept PDF or PWG raster, which driverless printing needs';
  if(check.suggested){
    if(confirm(`${printer} ${why}.\n\nUse the driver “${check.suggested.name}” instead?\n\nOK uses the driver; Cancel keeps driverless.`))return check.suggested.ppd;
    return 'everywhere';
  }
  if(confirm(`${printer} ${why}.\n\nAdd it as driverless anyway? If its pages come out garbled, remove it and choose a driver in step 3, or connect it as JetDirect.`))return 'everywhere';
  setPrintFeedback(feedback,'Not added. Choose a driver in step 3, or connect it as JetDirect.','info');
  return null;
}
const reasonText={'media-empty':'Out of paper','media-needed':'Load paper','media-jam':'Paper jam','toner-empty':'Out of toner','marker-supply-empty':'Out of toner or ink','door-open':'A cover is open','paused':'Printer paused','offline':'Printer offline','connecting-to-device':'Connecting to the printer','job-printing':'','none':''};
function jobRows(jobs,{showUser=false,cancel='mine'}={}){
  if(!jobs.length)return '<p class="muted">No print jobs.</p>';
  return jobs.map(job=>{
    const reasons=job.reasons.map(r=>reasonText[r]??'').filter(Boolean),detail=[...reasons,job.active?job.message:''].filter(Boolean).join(' · ');
    const when=job.created?new Date(job.created*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'';
    return `<div class="job-row ${html(job.state)}" data-job="${job.id}" data-server="${html(job.server)}"><span class="job-state">${html(job.label)}</span><div><strong>${html(job.title||`Job ${job.id}`)}</strong><small>${html(job.queue)}${showUser&&job.user?' · '+html(job.user):''} · ${html(when)}</small>${detail?`<p>${html(detail)}</p>`:''}</div>${job.active?`<button type="button" class="small-link" data-cancel-job="${cancel}">Cancel</button>`:''}</div>`;
  }).join('');
}
async function cancelJob(button){
  const row=button.closest('[data-job]'),admin=button.dataset.cancelJob==='admin';
  if(!confirm('Cancel this print job?'))return;
  button.disabled=true;
  try{await api(admin?'/api/admin/jobs/cancel':'/api/print/jobs/cancel',{method:'POST',body:{server:row.dataset.server,job_id:Number(row.dataset.job)}});toast('Print job cancelled.');}
  catch(error){problem(error);}
}
async function handleQueueAction(button){
  const card=button.closest('[data-printer]'),id=card.dataset.printer,action=button.dataset.queueAction;
  if(action==='jobs'){
    const panel=card.querySelector('.printer-jobs');
    if(!panel.classList.contains('hidden')&&button.dataset.open){panel.classList.add('hidden');delete button.dataset.open;return;}
    const result=await api(`${printerPath(id)}/jobs`);
    panel.innerHTML=(result.details_hidden?'<p class="inline-note">Connect the CUPS administrator login to see titles and owners.</p>':'')+jobRows(result.jobs,{showUser:true,cancel:'admin'});
    panel.classList.remove('hidden');button.dataset.open='1';
    panel.querySelectorAll('[data-cancel-job]').forEach(b=>b.onclick=async()=>{await cancelJob(b);button.dataset.open='';await handleQueueAction(button);});
    return;
  }
  if(action==='test'){button.disabled=true;try{const receipt=await api(`${printerPath(id)}/test-page`,{method:'POST'});toast(`Test page sent${receipt.job_id?' as '+receipt.job_id:''}.`);}finally{button.disabled=false;}return;}
  if(action==='pause'){const pausing=button.textContent==='Pause';await api(`${printerPath(id)}/pause`,{method:'POST',body:{paused:pausing}});toast(pausing?'Printer paused; new jobs wait until it is resumed.':'Printer resumed.');await refreshDeviceManager(true);return;}
  if(action==='remove'){
    const queue=id.split('|').pop();
    if(!confirm(`Remove ${queue} from CUPS? Waiting jobs on it are cancelled. This cannot be undone.`))return;
    await api(printerPath(id),{method:'DELETE'});toast(`${queue} removed.`);await refreshDeviceManager(true);await refreshPrintQueues(true);
  }
}
// The person's own print jobs on Print Desk, refreshed while any is active.
let jobsTimer=null;
async function loadMyJobs(){
  clearTimeout(jobsTimer);
  if($('#admin-print-view').classList.contains('hidden'))return;
  try{
    const result=await api('/api/print/jobs');
    $('#print-jobs').innerHTML=jobRows(result.jobs)+(result.unavailable.length?`<p class="inline-note">Not responding: ${result.unavailable.map(html).join(', ')}</p>`:'');
    $$('#print-jobs [data-cancel-job]').forEach(button=>button.onclick=async()=>{await cancelJob(button);await loadMyJobs();});
    if(result.jobs.some(job=>job.active))jobsTimer=setTimeout(loadMyJobs,4000);
  }catch(error){$('#print-jobs').innerHTML=`<p class="muted">${html(error.message)}</p>`;}
}
$('#print-jobs-refresh').onclick=()=>loadMyJobs();

const diagnosticLabels={ok:'OK',warn:'Check',fail:'Problem',info:'Info'};
async function runDiagnostics(){
  const button=$('#diagnostics-run'),out=$('#diagnostics-results');
  button.disabled=true;out.innerHTML='<p class="muted">Checking storage, printers and scanners…</p>';
  try{
    const result=await api('/api/admin/diagnostics');
    const titles={system:'System',printing:'Printing',scanning:'Scanning'};
    out.innerHTML=Object.entries(result.sections).map(([name,checks])=>`<div class="diagnostic-group"><h3>${html(titles[name]||name)}</h3>${checks.map(c=>`<div class="diagnostic-row ${html(c.status)}"><span class="diagnostic-state">${diagnosticLabels[c.status]||html(c.status)}</span><div><strong>${html(c.label)}</strong><p>${html(c.detail)}</p></div></div>`).join('')}</div>`).join('');
    $('#diagnostics-summary').textContent=result.overall==='ok'?'Everything Folio can check looks healthy.':result.overall==='warn'?'Working, with items worth a look.':'Something needs attention.';
  }catch(error){out.innerHTML='';throw error;}
  finally{button.disabled=false;}
}

// Installable as an app (0.20.1). Browsers allow the service worker only over
// HTTPS (or on the computer Folio runs on); over plain http Folio works as before.
if('serviceWorker' in navigator && window.isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
boot().catch(problem);
