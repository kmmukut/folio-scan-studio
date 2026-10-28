'use strict';
// Administration: the setup checklist, destinations, email, backups and System check.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
// First steps for administrators, ticked off by what is actually set up.
const CHECKLIST_ACTIONS={
  printer:['Add a printer',async()=>{await openDeviceManager();$('#add-printer-panel').scrollIntoView({behavior:'smooth'});}],
  print:['Print a test page',async()=>{await openDeviceManager();$('#device-printer-list').scrollIntoView({behavior:'smooth'});}],
  scanner:['Add a scanner',async()=>{await openDeviceManager();$('#device-scanner-list').scrollIntoView({behavior:'smooth'});}],
  people:['Add people',()=>$('#users-tab').onclick()],
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
