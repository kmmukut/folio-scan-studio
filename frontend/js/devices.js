'use strict';
// Devices: scanners, printers, print servers and adding printers.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
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
    const caps=s.capabilities||{};const tags=[s.protocol||'SANE',...(caps.sources||[]),...(caps.modes||[]),caps.resolutions?.length?Math.max(...caps.resolutions)+' DPI':'Options unavailable',...(s.airscan?['Shared as AirScan']:[])];
    return `<article class="device-card ${s.id===result.default_scanner?'current':''}" data-scanner="${html(s.id)}">
      <header><div><strong>${html(s.name)}</strong><small>${html(s.ip||s.sane_name||s.hint||'Existing configuration')}</small></div><span class="device-state ${s.connected?'':'off'}">${s.connected?'Connected':'Offline'}</span></header>
      <div class="device-tags">${tags.map(x=>`<span>${html(x)}</span>`).join('')}</div>
      <div class="device-actions"><button data-scanner-action="test">Test</button><button data-scanner-action="edit">Edit</button>${s.id!==result.default_scanner?'<button data-scanner-action="default">Make default</button>':''}<button data-scanner-action="toggle">${s.enabled?'Disable':'Enable'}</button>${['managed','attached'].includes(s.kind)?`<button data-scanner-action="airscan">${s.airscan?'Stop sharing':'Share as AirScan'}</button>`:''}<button data-scanner-action="report">Hardware report</button>${s.kind==='managed'?'<button data-scanner-action="delete">Remove</button>':''}</div></article>`;
  }).join('')||'<p>No scanner configured.</p>';
  $('#device-printer-list').innerHTML=result.printers.map(q=>{
    const settings=q.settings||{},caps=q.capabilities||{};
    const tags=[caps.color===true?'Color':caps.color===false?'Monochrome':'Color unknown',caps.duplex===true?'Duplex':caps.duplex===false?'Simplex':'Duplex unknown',caps.media?.length?caps.media.length+' paper sizes':'Sizes unknown',caps.trays?.length?caps.trays.length+' trays':null].filter(Boolean);
    return `<article class="device-card ${result.default_printer===q.id?'current':''}" data-printer="${html(q.id)}">
       <header><div><strong>${html(q.queue)}</strong><small>${html(q.server_label||q.server)} · ${html(q.status||'')}</small></div><span class="device-state ${q.ready?'':'off'}">${q.ready?'Ready':/disabled/i.test(q.status||'')?'Paused':'Offline'}</span></header>
       <div class="device-tags">${tags.map(x=>`<span>${html(x)}</span>`).join('')}</div>
       <div class="device-access">${[['enabled','Enabled'],['guest','Guest printing'],['member','Members'],['printer','Printer-only accounts']].map(([flag,label])=>`<label><input type="checkbox" data-printer-flag="${flag}" ${settings[flag]?'checked':''}> ${label}</label>`).join('')}</div>
       <div class="device-actions"><button data-printer-action="save" ${q.status==='Not found in CUPS'?'disabled':''}>Save permissions</button>${result.default_printer!==q.id?`<button data-printer-action="default" ${q.status==='Not found in CUPS'?'disabled':''}>Make default</button>`:''}${q.status!=='Not found in CUPS'?'<button data-queue-action="jobs">Jobs</button><button data-queue-action="test">Test page</button><button data-queue-action="report">Hardware report</button>':''}${managed.has(q.server)&&q.status!=='Not found in CUPS'?`<button data-queue-action="pause">${q.ready?'Pause':'Resume'}</button><button data-queue-action="remove" class="danger-action">Remove</button>`:''}</div>
       <div class="printer-supplies" aria-live="polite"></div>
       <div class="printer-jobs hidden"></div></article>`;
  }).join('')||'<p class="muted">No printers yet. Add one below.</p>';
  $('#device-scanner-list').querySelectorAll('[data-scanner-action]').forEach(btn=>btn.onclick=()=>handleScannerAction(btn).catch(problem));
  $('#device-printer-list').querySelectorAll('[data-printer-action]').forEach(btn=>btn.onclick=()=>handlePrinterAction(btn).catch(problem));
  $('#device-printer-list').querySelectorAll('[data-queue-action]').forEach(btn=>btn.onclick=()=>handleQueueAction(btn).catch(problem));
  for(const q of result.printers)if(q.status!=='Not found in CUPS')loadSupplies(q.id);
  loadAirscan().catch(problem);
}
// Copy an element's text; where the browser doesn't allow it (plain HTTP), select it instead.
function copyText(element){
  const text=element.value??element.textContent;
  (navigator.clipboard?.writeText(text)||Promise.reject()).then(()=>toast('Copied.'),()=>{
    if(element.select)element.select();
    else{const range=document.createRange();range.selectNodeContents(element);getSelection().removeAllRanges();getSelection().addRange(range);}
    toast('Selected: copy it with Ctrl+C (⌘C on a Mac).');
  });
}
// Hardware reports (report.py): Folio writes one from what the device answers; the
// administrator reads it, may change it, and submits it on GitHub. Folio sends nothing.
const HARDWARE_FORM='https://github.com/kmmukut/folio-scan-studio/issues/new';
let hardwareReport=null;
async function openHardwareReport(request){
  const text=$('#report-text'),status=$('#report-status');
  hardwareReport=null;text.value='';text.disabled=true;updateReportLink();
  status.textContent='Asking the device and the print server. This can take up to half a minute.';
  $('#report-dialog').showModal();
  try{
    hardwareReport=await api('/api/admin/devices/report',{method:'POST',body:request});
    text.value=hardwareReport.text;text.disabled=false;status.textContent='';updateReportLink();
  }catch(error){status.textContent=error.message;}
}
function hardwareReportUrl(report,text){
  const url=new URL(HARDWARE_FORM),fields={template:'hardware_report.yml',title:report.title,...report.fields,notes:text};
  for(const [key,value] of Object.entries(fields))if(value)url.searchParams.set(key,value);
  if(url.href.length>8000)url.searchParams.delete('notes');  // Too long for a link: pasted by hand
  return url.href;
}
function updateReportLink(){
  const link=$('#report-open');
  if(!hardwareReport){link.removeAttribute('href');$('#report-long').classList.add('hidden');return;}
  link.href=hardwareReportUrl(hardwareReport,$('#report-text').value);
  $('#report-long').classList.toggle('hidden',new URL(link.href).searchParams.has('notes'));
}
// After something failed: offer a report on it, next to the message.
function offerHardwareReport(element,request){
  const button=document.createElement('button');
  button.type='button';button.className='small-link report-offer';button.textContent='Write a hardware report';
  button.onclick=()=>openHardwareReport(request).catch(problem);
  element.append(' ',button);
}
$('#report-text').oninput=updateReportLink;
$('#report-copy').onclick=()=>copyText($('#report-text'));
$('#report-close').onclick=()=>$('#report-dialog').close();
// Scanners shared as AirScan (AirSane): what's shared, and how to announce it by hand.
async function loadAirscan(){
  const status=await api('/api/admin/airscan'),out=$('#airscan-status');
  out.innerHTML=airscanHtml(status);
  out.querySelectorAll('[data-copy-service]').forEach(button=>button.onclick=()=>copyText(button.closest('details').querySelector('pre')));
  if(status.running&&!status.ready)setTimeout(()=>loadAirscan().catch(()=>{}),3000);  // Starting: look again shortly
}
function airscanHtml(status){
  if(!status.available)return '<p class="inline-note">This copy of Folio doesn’t include AirSane, so it can’t share scanners.</p>';
  if(!status.running)return '<p class="inline-note">No scanner is shared.</p>';
  if(!status.ready)return '<p class="inline-note">Starting AirSane…</p>';
  const byHand=s=>`Folio announces shared scanners itself when the host’s Avahi folder is mounted and the built-in print server is on (as for AirPrint), and then only on the host’s own network. If the Macs and phones are on another network, or the host’s Avahi can’t reach them, save this as <code>/etc/avahi/services/${html(s.service_name)}</code> on a Linux computer that runs Avahi on their network, and make its port ${status.port} reach Folio’s port 8090. If you share or stop sharing another scanner, or rename this one, copy the file again: its address can change.`;
  const rows=status.scanners.map(s=>`<div class="airscan-row"><strong>${html(s.name)}</strong> · ${s.problem?html(s.problem):s.announced?'shared, and announced through this host’s Avahi':'shared; not announced by Folio'}
    ${!s.problem&&s.service_file?`<details class="manual-details"${s.announced?'':' open'}><summary>Announce it yourself</summary><p class="inline-note">${byHand(s)}</p><pre>${html(s.service_file)}</pre><button type="button" class="small-link" data-copy-service>Copy</button></details>`:''}</div>`).join('');
  return `${status.error?`<p class="print-feedback error">${html(status.error)}</p>`:''}<div class="airscan-list">${rows}</div>
    <p class="inline-note">Other computers reach shared scanners on port ${status.port}: publish it to Folio’s port 8090 (see <code>compose.yaml</code>).</p>`;
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
  if(act==='report'){await openHardwareReport({scanner:id});return;}
  const result=await api('/api/admin/devices');const current=result.scanners.find(s=>s.id===id);
  if(!current)return;
  if(act==='edit'){
    const f=$('#scanner-device-form');f.dataset.editId=id;
    $('#scanner-form-title').textContent=`Edit ${current.name}`;
    $('#device-scanner-name').value=current.name;
    $('#device-scanner-ip').value=current.ip||'';
    $('#device-scanner-protocol').value=current.protocol||'WSD';updateScannerPlaceholders();
    $('#device-scanner-port').value=current.port||'';
    $('#device-scanner-path').value=current.path||'';
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
  if(act==='airscan'){
    if(!current.airscan&&!confirm(`Share ${current.name} as an AirScan scanner?\n\nMacs, Windows and Android devices on your network can then scan from it without signing in to Folio.`))return;
    await api(`/api/admin/scanners/${id}`,{method:'PATCH',body:{airscan:!current.airscan}});
    await refreshDeviceManager();
    toast(current.airscan?`${current.name} is no longer shared.`:`${current.name} is shared as AirScan; it takes a few seconds to start.`);return;
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
  updateScannerPlaceholders();
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
// WSD has no standard port or path (each brand picks its own); left empty, Folio asks the scanner.
function updateScannerPlaceholders(){
  const wsd=$('#device-scanner-protocol').value==='WSD';
  $('#device-scanner-port').placeholder=wsd?'automatic':'80';
  $('#device-scanner-path').placeholder=wsd?'automatic':'/eSCL';
}
$('#device-scanner-protocol').onchange=()=>{
  $('#device-scanner-port').value='';$('#device-scanner-path').value='';updateScannerPlaceholders();
};
$('#scanner-device-form').onsubmit=async e=>{
  e.preventDefault();const f=e.currentTarget,id=f.dataset.editId,save=$('#scanner-form-save'),label=save.textContent;
  const body={name:$('#device-scanner-name').value,ip:$('#device-scanner-ip').value,
      protocol:$('#device-scanner-protocol').value,port:Number($('#device-scanner-port').value)||null,
      path:$('#device-scanner-path').value.trim()||null,make_default:$('#device-scanner-default').checked};
  save.disabled=true;save.textContent=body.protocol==='WSD'&&!(body.port&&body.path)?'Asking the scanner…':'Saving…';
  try{
    await api(id?`/api/admin/scanners/${id}`:'/api/admin/scanners',
              {method:id?'PATCH':'POST',body});
    clearScannerEditor();await refreshDeviceManager();await refreshScannerChoices();
    toast('Scanner saved. Its SANE connection has been updated without restarting Folio. Use Test to verify.');
  }catch(error){problem(error);save.textContent=label;}
  finally{save.disabled=false;}
};
async function handlePrinterAction(btn){
  const card=btn.closest('[data-printer]'),queue=card.dataset.printer;
  const body={};card.querySelectorAll('[data-printer-flag]').forEach(box=>body[box.dataset.printerFlag]=box.checked);
  if(btn.dataset.printerAction==='default')body.make_default=true;
  await api(`/api/admin/printers/${encodeURIComponent(queue)}`,{method:'PATCH',body});
  await refreshDeviceManager();await refreshPrintQueues();toast(`Updated ${queue}`);
}
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
const addPrinter={found:[],makesFor:null,checked:null};  // checked: the model a network address check found
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
  if(driverKind()==='ppd')return loadDriverMakes(addPrinter.found.find(d=>d.uri===selectedSource())||(selectedSource()==='manual'?addPrinter.checked:null)).catch(problem);
}
// A typed address: port and path are optional, with each connection's usual ones as placeholders.
const CONNECTION_DEFAULTS={ipp:['631','/ipp/print'],socket:['9100',''],lpd:['515','/queue']};
function updateConnectionFields(){
  const [port,path]=CONNECTION_DEFAULTS[$('#add-printer-connection').value];
  $('#add-printer-port').placeholder=port;$('#add-printer-path').placeholder=path;
  $('#add-printer-path-field').classList.toggle('hidden',!path);
}
function manualPrinterTarget(){
  const connection=$('#add-printer-connection').value,path=$('#add-printer-path').value.trim();
  return {connection,address:$('#add-printer-address').value.trim(),port:Number($('#add-printer-port').value)||null,path:connection!=='socket'&&path?path:null};
}
$('#add-printer-connection').onchange=updateConnectionFields;
$('#add-printer-address').oninput=()=>{addPrinter.checked=null;};
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
let driverTimer;
$('#driver-model').oninput=()=>{clearTimeout(driverTimer);driverTimer=setTimeout(()=>loadDrivers().catch(problem),300);};
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
    ...(source==='manual'?manualPrinterTarget():{device_uri:source})};
  button.disabled=true;
  try{
    const started=await api(`${serverPath($('#add-printer-server').value)}/printers`,{method:'POST',body});
    const {result}=await followTask(started.task,{onUpdate:task=>setPrintFeedback(feedback,taskText(task),'info')});
    setPrintFeedback(feedback,`${result.queue} is ready${result.state?.driver?` (${result.state.driver})`:''}. Use Test page on its card to check it.`,'ok');
    $('#add-printer-form').reset();addPrinter.found=[];addPrinter.checked=null;$('#found-printers').innerHTML='';$('#find-printers-status').textContent='';
    $('#manual-printer-details').open=false;updateConnectionFields();updatePrinterForm();
    await refreshDeviceManager(true);await refreshPrintQueues(true);
  }catch(error){
    setPrintFeedback(feedback,error.message,'error');
    const how=(source==='manual'?CONNECTION_NAMES[$('#add-printer-connection').value]+', ':'')
      +(driver==='everywhere'?'driverless':`the driver “${[...$('#driver-choice').options].find(o=>o.value===driver)?.textContent||driver}”`);
    offerHardwareReport(feedback,{...(source==='manual'?{address:$('#add-printer-address').value.trim()}:{device_uri:source}),
      name:body.name,problem:`Adding it as a printer (${how}) failed: ${error.message}`});
  }
  finally{button.disabled=false;}
};
// Driverless printing needs PDF or PWG raster; some printers that offer only
// AirPrint's raster print it as garbage. Ask the printer first, and offer a driver.
async function checkDriverless(source,feedback){
  const target=source==='manual'?($('#add-printer-connection').value==='ipp'?manualPrinterTarget():null)
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
// Check a network address: Folio asks the device what it offers (it only asks;
// nothing is printed or scanned), then fills in the scanner or printer form.
let checkedAddress=null;
const PROTOCOL_NAMES={eSCL:'eSCL / AirScan',WSD:'WSD'},CONNECTION_NAMES={ipp:'IPP',socket:'JetDirect',lpd:'LPD'};
$('#address-check-form').onsubmit=async event=>{
  event.preventDefault();
  const button=$('#address-check-submit'),out=$('#address-check-result');
  button.disabled=true;button.textContent='Checking…';out.innerHTML='<p class="inline-note">Asking the device. This takes a few seconds.</p>';
  try{
    checkedAddress=await api('/api/admin/devices/check-address',{method:'POST',body:{address:$('#address-check-input').value.trim(),server:$('#add-printer-server').value||null}});
    out.innerHTML=addressCheckHtml(checkedAddress);
    out.querySelectorAll('[data-use-scanner]').forEach(b=>b.onclick=()=>useCheckedScanner(checkedAddress.scanners[Number(b.dataset.useScanner)]));
    out.querySelectorAll('[data-use-printer]').forEach(b=>b.onclick=()=>useCheckedPrinter().catch(problem));
    out.querySelectorAll('[data-address-report]').forEach(b=>b.onclick=()=>openHardwareReport({address:checkedAddress.address}).catch(problem));
  }catch(error){out.innerHTML=`<p class="print-feedback error">${html(error.message)}</p>`;}
  finally{button.disabled=false;button.textContent='Check';}
};
function addressCheckHtml(c){
  const p=c.printing,plan=p.recommended,canAddPrinter=!$('#add-printer-form').classList.contains('hidden');
  const where=html(c.address)+(c.address!==c.ip?` · ${html(c.ip)}`:'');
  const heading=c.make_model?`<h4>${html(c.make_model)}<small>${where}</small></h4>`:`<h4>${where}</h4>`;
  if(!c.scanners.length&&!p.ipp&&!p.jetdirect&&!p.lpd)
    return `${heading}<p class="print-feedback error">Nothing answered at this address: no printing (IPP, JetDirect, LPD) and no scanning (eSCL, WSD). Check the address, and that the device is switched on and reachable from Folio.</p>`;
  const scanners=c.scanners.map((s,i)=>`<li><strong>${PROTOCOL_NAMES[s.protocol]}</strong> · port ${s.port} · <code>${html(s.path)}</code>${s.found_by==='known address'?'<br><small>The scanner didn’t describe itself, but answered at the address its brand uses.</small>':''}<br><button type="button" class="small-link" data-use-scanner="${i}">Add as scanner</button></li>`).join('');
  const printing=[
    p.ipp&&`<li><strong>IPP</strong> · port ${p.ipp.port} · <code>${html(p.ipp.path)}</code><br><small>${p.ipp.full?'Prints driverless':'Can’t print driverless reliably (no PDF or PWG raster)'}${p.ipp.encrypted_only?' · encrypted connections only':''}</small></li>`,
    p.jetdirect&&`<li><strong>JetDirect</strong> · port ${p.jetdirect}</li>`,
    p.lpd&&`<li><strong>LPD</strong> · port ${p.lpd}</li>`].filter(Boolean).join('');
  const driver=!plan?'':plan.driver==='everywhere'?'driverless':p.suggested?`the driver “${html(p.suggested.name)}”`:'a driver you choose';
  const add=!plan?'':canAddPrinter?'<button type="button" class="small-link" data-use-printer>Add as printer</button>':'<p class="inline-note">To add it, first connect a print server’s administrator login under <strong>Print servers</strong>.</p>';
  return `${heading}<div class="address-check-grid">
    <section><span class="overline">Scanning</span>${scanners?`<ul>${scanners}</ul>`:'<p class="inline-note">No scanning found (eSCL or WSD).</p>'}</section>
    <section><span class="overline">Printing</span>${printing?`<ul>${printing}</ul>`:'<p class="inline-note">No printing found (IPP, JetDirect or LPD).</p>'}
      ${plan?`<p class="inline-note">Suggested: ${CONNECTION_NAMES[plan.connection]}, ${driver}.</p>${add}`:''}</section></div>
    <p class="inline-note address-report">Whether it works with Folio or not, a <button type="button" class="small-link" data-address-report>hardware report</button> helps others with the same device.</p>`;
}
// Scanner names allow letters, numbers, spaces, dot, dash and underscore.
function deviceName(text){return (text||'').replace(/[^\w .-]+/g,' ').replace(/\s+/g,' ').trim().replace(/^[^A-Za-z0-9]+/,'').slice(0,75);}
function useCheckedScanner(s){
  clearScannerEditor();
  $('#device-scanner-name').value=deviceName(checkedAddress.make_model||s.name)||'Network scanner';
  $('#device-scanner-ip').value=checkedAddress.ip;$('#device-scanner-protocol').value=s.protocol;updateScannerPlaceholders();
  $('#device-scanner-port').value=s.port;$('#device-scanner-path').value=s.path;
  $('#scanner-device-form').scrollIntoView({behavior:'smooth',block:'center'});$('#device-scanner-name').focus({preventScroll:true});
  toast('Filled in from the check. Check the name, then press Add scanner.');
}
async function useCheckedPrinter(){
  const c=checkedAddress,plan=c.printing.recommended;
  document.querySelector('input[name=printer-way][value=manual]').checked=true;
  $('#add-printer-address').value=c.address;$('#add-printer-connection').value=plan.connection;updateConnectionFields();
  $('#add-printer-port').value=plan.port||'';$('#add-printer-path').value=plan.path||'';
  $('#manual-printer-details').open=Boolean(plan.port||plan.path);
  $('#add-printer-name').value=suggestName(c.make_model||'Printer');
  addPrinter.checked={make_model:c.make_model};
  document.querySelector(`input[name=driver-kind][value=${plan.driver==='everywhere'?'everywhere':'ppd'}]`).checked=true;
  $('#add-printer-form').scrollIntoView({behavior:'smooth',block:'start'});
  await updatePrinterForm();
  if(plan.driver&&plan.driver!=='everywhere'&&[...$('#driver-choice').options].some(o=>o.value===plan.driver))$('#driver-choice').value=plan.driver;
  $('#add-printer-name').focus({preventScroll:true});
  toast(plan.driver==='everywhere'||plan.driver?'Filled in from the check. Check the name, then press Add printer.':'Filled in from the check. Choose a driver in step 3, then press Add printer.');
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
  if(action==='report'){await openHardwareReport({printer:id});return;}
  if(action==='test'){button.disabled=true;try{const receipt=await api(`${printerPath(id)}/test-page`,{method:'POST'});toast(`Test page sent${receipt.job_id?' as '+receipt.job_id:''}.`);}finally{button.disabled=false;}return;}
  if(action==='pause'){const pausing=button.textContent==='Pause';await api(`${printerPath(id)}/pause`,{method:'POST',body:{paused:pausing}});toast(pausing?'Printer paused; new jobs wait until it is resumed.':'Printer resumed.');await refreshDeviceManager(true);return;}
  if(action==='remove'){
    const queue=id.split('|').pop();
    if(!confirm(`Remove ${queue} from CUPS? Waiting jobs on it are cancelled. This cannot be undone.`))return;
    await api(printerPath(id),{method:'DELETE'});toast(`${queue} removed.`);await refreshDeviceManager(true);await refreshPrintQueues(true);
  }
}
