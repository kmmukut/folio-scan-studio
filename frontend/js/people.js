'use strict';
// People & access: accounts, invitations, page quotas and job limits.
// (Folio's page loads these files in order: core, auth, workbench, print, people, stats, devices, admin, main.)
// null (the default) >= 0 is true in JavaScript, so test for a number.
const ownLimit=u=>typeof u.monthly_pages==='number'&&u.monthly_pages>=0;
const ownJobs=u=>typeof u.job_limit==='number'&&u.job_limit>=1;
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
      ${u.role==='admin'?'':`<div class="person-quota"><label>Jobs at once <select class="person-jobs-mode"><option value="default" ${u.job_limit===null?'selected':''}>Default</option><option value="none" ${u.job_limit===-1?'selected':''}>No limit</option><option value="custom" ${ownJobs(u)?'selected':''}>Their own</option></select></label><input class="person-jobs-limit ${ownJobs(u)?'':'hidden'}" type="number" min="1" max="100" value="${ownJobs(u)?u.job_limit:''}" aria-label="Jobs at once"><small>Exports, prints and sends waiting or running${u.jobs.limit===null?'':`: at most ${u.jobs.limit}`}</small></div>`}
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
        const jobsMode=row.querySelector('.person-jobs-mode')?.value;
        if(jobsMode){
          const jobs=jobsMode==='default'?null:jobsMode==='none'?-1:Number(row.querySelector('.person-jobs-limit').value||0);
          await api(`/api/admin/users/${encodeURIComponent(username)}/job-limit`,{method:'PUT',body:{job_limit:jobs}});
        }
        toast('Updated '+username);await loadUsers();
      }catch(e){problem(e);}
    });
    $$('.person-quota-mode').forEach(select=>select.onchange=()=>select.closest('.person').querySelector('.person-quota-pages').classList.toggle('hidden',select.value!=='custom'));
    $$('.person-jobs-mode').forEach(select=>select.onchange=()=>select.closest('.person').querySelector('.person-jobs-limit').classList.toggle('hidden',select.value!=='custom'));
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
$('#users-tab').onclick=async()=>{if(state.role!=='admin')return;workspace(false,true);await Promise.all([loadUsers(),loadQuotaDefault(),loadJobLimitDefault()]);};
async function loadJobLimitDefault(){$('#job-limit-default').value=(await api('/api/admin/job-limit-default')).job_limit;}
$('#job-limit-form').onsubmit=async event=>{
  event.preventDefault();
  try{
    const {job_limit}=await api('/api/admin/job-limit-default',{method:'PUT',body:{job_limit:Number($('#job-limit-default').value)}});
    toast(`Everyone may have ${job_limit} ${job_limit===1?'job':'jobs'} waiting or running at once, unless they have their own number.`);
    await Promise.all([loadUsers(),loadJobLimitDefault()]);
  }catch(e){problem(e);}
};
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
