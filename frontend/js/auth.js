'use strict';
// Signing in, the first administrator, "Forgot your password?", links from emails, and Your account.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
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
$('#logout-button').onclick=signOut;
$('#top-logout').onclick=signOut;
