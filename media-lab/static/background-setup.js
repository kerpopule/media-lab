/* Same-origin admin session only. Never place access codes in URLs or storage. */
'use strict';
const el=id=>document.getElementById(id);
let plan=null, current=null, sending=false, polling=false, statusError='';
/* Independent hosts sign in at /api/setup/session; the legacy app keeps /api/gate. */
let independentSession=null;
const gib=bytes=>(bytes/1024**3).toFixed(1)+' GiB';
async function api(path,body,method){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),20000);
  const write=Boolean(body)||(method&&method!=='GET');
  try{
    /* The custom header is deliberately not CORS-allowed: it is the same-origin proof for writes. */
    const response=await fetch(path,{method:method||(body?'POST':'GET'),credentials:'same-origin',redirect:'error',signal:controller.signal,
      headers:{...(body?{'Content-Type':'application/json'}:{}),...(write?{'X-Setup-Request':'1'}:{})},body:body?JSON.stringify(body):undefined});
    const data=await response.json().catch(()=>({}));
    if(!response.ok){if(response.status===401||response.status===403)el('login').hidden=false;
      const error=new Error(data.detail||data.error||'The server could not complete this request.');error.status=response.status;throw error;}
    return data;
  }finally{clearTimeout(timer);}
}
function controls(){
  el('removal').hidden=!['qualified','removing'].includes(current?.recordedStatus);
  el('remove').textContent=current?.recordedStatus==='removing'?'Resume removal':'Remove model files';
  el('remove').disabled=!plan||sending||current?.runningHere||current?.desiredEnabled||current?.hostActive||current?.externallyControlled;
  el('install').disabled=!plan||sending||current?.runningHere||plan.blockedReasons.length>0||['qualified','removing'].includes(current?.recordedStatus);
  el('install').textContent=current?.recordedStatus==='removing'?'Finish removal before reinstalling':current?.recordedStatus==='qualified'?'Model qualified':current?.runtimeRemoved?'Reinstall and test model':'Install and test model';
  el('refresh').disabled=sending||current?.runningHere;
  el('enable').hidden=current?.recordedStatus!=='qualified'||Boolean(current?.hostReady);
  el('enable').disabled=!plan||sending||current?.runningHere||current?.externallyControlled;
  el('disable').hidden=!current?.desiredEnabled&&!current?.hostActive;
  el('disable').disabled=!plan||sending||current?.runningHere||current?.externallyControlled;
}
async function status(){
  if(polling)return;polling=true;
  try{
    current=await api('/api/setup/background/status');el('progress').hidden=false;
    const stages={preflight:'Checking this server', 'download-and-verify':'Downloading and verifying model files',runtime:'Installing isolated dependencies',
      'dependency-inventory':'Recording dependency notices',qualification:'Testing real background removal',complete:'Installation complete'};
    el('status').textContent=current.runningHere?(current.operation==='remove'?'Removing model files; keeping your creations…':current.operation==='enable'?'Checking qualification and starting the model…':current.operation==='disable'?'Finishing current work and disabling the model…':stages[current.stage]||'Starting setup…'):
      current.hostReady?'Background removal is ready for connected devices.':
      current.recordedStatus==='qualified'?'Model installed and qualification recorded.':current.recordedStatus==='removed'?'Model removed. Your creations are retained.':
      current.recordedStatus==='removing'?'Model removal was interrupted. Resume removal to finish cleaning up model files. Your creations are retained.':
      current.recordedStatus==='not-installed'?'Model is not installed yet.':current.recordedStatus==='failed'?'Setup could not finish '+(stages[current.stage]||'this step')+'. Refresh the plan to review a retry.':current.error||'Setup is not running in this app process. Refresh the plan to review a retry.';
    el('next').textContent=current.externallyControlled?'Activation is controlled by this server’s configuration.':
      current.hostReady?'Enabled. This choice is saved for server restarts.':
      current.desiredEnabled?'Enable requested. The model is available only after the host passes its checks.':
      current.recordedStatus==='removing'?'Use Resume removal below. You can reinstall after removal finishes.':
      current.recordedStatus==='qualified'?'Installed and disabled. Enable the model to let paired devices create cutouts.':
      'Installation will refuse if another host or job is using this runtime.';
    const nextError=current.error||current.hostError||'';
    if(nextError||el('error').textContent===statusError)el('error').textContent=nextError;
    statusError=nextError;
    controls();
  }catch(error){statusError=error.message;el('error').textContent=statusError;}finally{polling=false;}
}
async function review(){
  el('error').textContent='';plan=null;controls();
  try{
    plan=await api('/api/setup/background/plan');el('login').hidden=true;el('plan').hidden=false;
    const total=plan.totalMemoryBytes, available=plan.availableMemoryBytes, required=plan.memoryRequiredBytes;
    const measured=Number.isFinite(total)&&total>0&&Number.isFinite(available)&&available>=0&&available<=total;
    el('memorySummary').textContent=measured?gib(available)+' available of '+gib(total)+' total':gib(available)+' available now';
    el('memoryMeter').hidden=!measured;
    if(measured){el('memoryMeter').max=total;el('memoryMeter').value=available;el('memoryMeter').setAttribute('aria-valuetext',gib(available)+' available of '+gib(total));}
    el('memoryFit').textContent=available>=required?gib(required)+' must be available for the installation test. There is enough available right now.':gib(required)+' must be available for the installation test. Free another '+gib(required-available)+' by closing other apps or stopping models you are not using.';
    el('diskSummary').textContent=gib(plan.freeDiskBytes)+' free · '+gib(plan.workspaceReserveBytes)+' installation reserve required.';
    const facts=[['Model',plan.model],['Model license',plan.modelLicense],['Model files',gib(plan.modelDownloadBytes)],
      ['Workspace reserve',gib(plan.workspaceReserveBytes)],['Required available memory',gib(plan.memoryRequiredBytes)],['Available memory now',gib(plan.availableMemoryBytes)]];
    el('facts').replaceChildren(...facts.flatMap(([key,value])=>{const dt=document.createElement('dt');dt.textContent=key;const dd=document.createElement('dd');dd.textContent=value;return[dt,dd];}));
    el('blocked').replaceChildren(...plan.blockedReasons.map(reason=>{const li=document.createElement('li');li.textContent=reason;return li;}));
    el('technical').textContent='Model revision: '+plan.revision+'\nRuntime: '+plan.installationRoot+'\nCreations: '+plan.artifactRoot;
    await status();controls();
  }catch(error){el('error').textContent=error.message;}
}
el('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();el('signIn').disabled=true;el('error').textContent='';
  try{
    if(independentSession===false){const result=await api('/api/gate',{code:el('code').value});if(result.role!=='admin')throw new Error('Use the administrator code for model setup.');}
    else{await api('/api/setup/session',{code:el('code').value});independentSession=true;}
    el('signOut').hidden=!independentSession;await review();void packs();
  }catch(error){el('error').textContent=error.message;}finally{el('code').value='';el('signIn').disabled=false;}
});
el('signOut').addEventListener('click',async()=>{
  el('error').textContent='';
  try{await api('/api/setup/session',null,'DELETE');}catch(error){el('error').textContent=error.message;return;}
  plan=null;current=null;el('plan').hidden=true;el('progress').hidden=true;el('removal').hidden=true;el('packs').hidden=true;el('signOut').hidden=true;el('login').hidden=false;
});
async function packs(){
  if(independentSession!==true)return;
  let data;
  try{data=await api('/api/setup/packs');}catch(error){if(error.status===404)return;el('error').textContent=error.message;return;}
  el('packs').hidden=false;
  const names={speech:'Speech (Chatterbox English, CPU)',model3d:'Draft 3D assets (TripoSR, CPU)',music:'Music (ACE-Step, GPU)',video:'Video (Wan2.2 TI2V-5B, GPU)',image:'Images (Z-Image-Turbo, GPU)'};
  el('packList').replaceChildren(...Object.entries(data.packs).map(([id,pack])=>{
    const li=document.createElement('li');
    const state=!pack.configured?'Not configured on this server.':pack.ready?'On and ready for connected devices.':pack.enabled?(pack.active?'Starting or checking its files…':pack.error||'On, but not running. Check its configuration.'):'Off.';
    const text=document.createElement('span');text.textContent=names[id]+' — '+state;li.append(text);
    if(pack.configured){
      const button=document.createElement('button');button.className='secondary';button.textContent=pack.enabled?'Turn off after current work':'Turn on';
      button.disabled=sending||data.runningHere;
      button.addEventListener('click',async()=>{if(sending)return;sending=true;el('error').textContent='';
        try{await api('/api/setup/packs/activation',{pack:id,enabled:!pack.enabled});}catch(error){el('error').textContent=error.message;}
        finally{sending=false;setTimeout(()=>void packs(),1500);}});
      li.append(button);
    }
    return li;}));
}
async function boot(){
  try{
    const session=await api('/api/setup/session');
    independentSession=true;el('signOut').hidden=!session.signedIn;
    if(!session.signedIn){el('login').hidden=false;return;}
    void packs();
  }catch(error){
    if(error.status===404)independentSession=false;  /* legacy app: /api/gate session */
    else{el('error').textContent=error.message;el('login').hidden=false;return;}
  }
  await review();
}
el('install').addEventListener('click',async()=>{
  if(!plan||sending)return;sending=true;controls();el('error').textContent='';
  try{await api('/api/setup/background/install',{planId:plan.planId,reinstall:Boolean(current?.runtimeRemoved)});await status();}
  catch(error){el('error').textContent=error.message;}finally{sending=false;controls();}
});
async function activation(enabled){
  if(!plan||sending)return;sending=true;controls();el('error').textContent='';
  try{await api('/api/setup/background/activation',{planId:plan.planId,enabled});await status();}
  catch(error){el('error').textContent=error.message;}finally{sending=false;controls();}
}
el('remove').addEventListener('click',async()=>{
  if(!plan||sending)return;sending=true;controls();el('error').textContent='';
  try{await api('/api/setup/background/remove',{planId:plan.planId});await status();}
  catch(error){el('error').textContent=error.message;}finally{sending=false;controls();}
});
el('enable').addEventListener('click',()=>activation(true));
el('disable').addEventListener('click',()=>activation(false));
el('refresh').addEventListener('click',review);
setInterval(()=>{if(plan&&!document.hidden){void status();void packs();}},3000);
void boot();
