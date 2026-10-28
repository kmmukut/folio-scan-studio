'use strict';
// The workbench: documents, pages, scanning, editing, cropping, exporting and sending.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
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
// The crop frame by keyboard: arrows move it, Shift + arrows resize it from
// its right and bottom edges, Ctrl makes bigger steps, Enter applies. With no
// frame yet, the first arrow press makes one just inside the page.
const CROP_KEYS={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
function nudgeCrop(e){
  if(e.key==='Enter'){e.preventDefault();finishCrop();return;}
  const direction=CROP_KEYS[e.key];if(!direction)return;
  e.preventDefault();
  const step=(e.ctrlKey||e.metaKey)?.05:.01,[dx,dy]=direction;
  let [x,y,w,h]=cropRect||[.05,.05,.9,.9];
  if(cropRect&&e.shiftKey){
    w=Math.max(cropMinimum,Math.min(1-x,w+dx*step));h=Math.max(cropMinimum,Math.min(1-y,h+dy*step));
  }else if(cropRect){
    x=Math.max(0,Math.min(1-w,x+dx*step));y=Math.max(0,Math.min(1-h,y+dy*step));
  }
  cropDraw([x,y,w,h]);
  const pct=v=>Math.round(v*100);
  $('#crop-status').textContent=`Crop ${pct(w)}% wide and ${pct(h)}% high, ${pct(x)}% from the left and ${pct(y)}% from the top.`;
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
$('#new-job').onclick=()=>newJob().catch(problem);
$('#new-job-sidebar').onclick=()=>{workspace(false);newJob().catch(problem);};
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
$('#scan-now').onclick=scanNow;
$('#cancel-scan').onclick=async()=>{try{renderScanStatus(await api('/api/scan/cancel',{method:'POST'}));}catch(e){problem(e);}};
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
$('#prev-page').onclick=()=>selectAdjacentPage(-1);
$('#next-page').onclick=()=>selectAdjacentPage(1);
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
$('#crop-image-wrap').addEventListener('keydown',nudgeCrop);
$('#crop-image-wrap').addEventListener('pointermove',moveCropSelection);
$('#crop-image-wrap').addEventListener('pointerup',endCropSelection);
$('#crop-image-wrap').addEventListener('pointercancel',endCropSelection);
$('#crop-dialog').onclick=e=>{if(e.target===$('#crop-dialog'))$('#crop-dialog').close();};
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
