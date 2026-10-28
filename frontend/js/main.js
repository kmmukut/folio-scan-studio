'use strict';
// Navigation between the main views, and starting Folio (loaded last).
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
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
$('#workspace-tab').onclick=()=>{workspace(false);if(state.role==='admin')loadChecklist().catch(()=>{});};
$('#print-tab').onclick=()=>showPrintDesk().catch(problem);
$('#devices-tab').onclick=()=>openDeviceManager().catch(problem);
$('#stats-tab').onclick=()=>openStats().catch(problem);
$('#stats-period').onchange=()=>loadStats().catch(problem);
$('#stats-save').onclick=()=>saveStatsSettings().catch(problem);
$('#stats-clear').onclick=()=>clearStats().catch(problem);
$('#devices-refresh').onclick=()=>refreshDeviceManager(true).catch(problem);
$('#diagnostics-run').onclick=()=>runDiagnostics().catch(problem);
// Installable as an app (0.20.1). Browsers allow the service worker only over
// HTTPS (or on the computer Folio runs on); over plain http Folio works as before.
if('serviceWorker' in navigator && window.isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
boot().catch(problem);
