'use strict';
// Shared basics: talking to Folio's server, messages, switching views, background-job progress.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const icon = name => `<svg class="svg-icon"><use href="#ico-${name}"/></svg>`;
const html = input => String(input??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {job:null,jobs:[],selected:null,scanning:false,scan:null,csrf:null,user:null,role:null,autoSuggestion:null,guestEnabled:false,guestCsrf:null,guestPreview:false,dragId:null,guestFile:null,memberFile:null,scanners:[],printQueues:[],
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
function setPrintFeedback(element,message,kind){element.textContent=message;element.className=`print-feedback ${kind}`;}
const count=value=>Number(value||0).toLocaleString();
// --- Printer administration (0.11) ---------------------------------------------
const printerPath=id=>`/api/admin/printers/${encodeURIComponent(id)}`;
const serverPath=server=>`/api/admin/cups/servers/${encodeURIComponent(server)}`;
