'use strict';
// The Print desk: uploads, saved documents, print options, guest printing and print jobs.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
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
$('#login-guest-button').onclick=openGuest;
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
$('#save-guest').onclick=async()=>{try{let r=await api('/api/admin/guest',{method:'POST',body:{enabled:$('#guest-enable').checked}});state.guestEnabled=r.enabled;toast(r.enabled?'Guest printing enabled':'Guest printing disabled');}catch(e){problem(e);}};
$('#open-guest-view').onclick=openGuest;
$('#guest-back').onclick=closeGuest;
wireDropZone({key:'guest',zoneId:'upload-area',inputId:'guest-print-file'});
wireDropZone({key:'member',zoneId:'member-upload-area',inputId:'member-print-file'});
$('#guest-print-form').onsubmit=e=>submitPrintFile(e,true);
$('#member-print-form').onsubmit=e=>submitPrintFile(e,false);
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
