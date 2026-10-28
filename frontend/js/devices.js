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
