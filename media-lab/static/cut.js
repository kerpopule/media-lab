/* Cut — Media Lab timeline editor (vanilla, no build step).
   The browser never edits a private copy: every change is one transaction against
   /api/cut/projects/{id}/commands with the project's current revision, and the view
   re-reads the project afterwards. Sparky proposals show up as an exact diff with
   Approve / Reject. Playback seeks the real source under the playhead. */
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PID=new URLSearchParams(location.search).get('project');
const api=async(path,body,method)=>{
  const r=await fetch(path,{method:method||(body?'POST':'GET'),headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
  let d={};try{d=await r.json();}catch(e){}
  if(r.status===401){location.href='/gate';throw new Error('locked');}
  if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
  return d;
};
let toastT;
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.classList.toggle('bad',!!bad);t.classList.add('on');clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('on'),bad?3800:2200);}

/* ======================= project picker ======================= */
async function picker(){
  $('#picker').hidden=false;$('#editor').hidden=true;
  let d;try{d=await api('/api/cut/projects');}catch(e){$('#projlist').innerHTML=`<div class="empty">${esc(e.message)}</div>`;return;}
  const rows=d.projects||[];
  $('#projlist').innerHTML=rows.length?rows.map(p=>`<a class="proj" href="/cut?project=${encodeURIComponent(p.project_id)}">
      ${p.poster?`<img src="${esc(p.poster)}" alt="">`:`<div class="ph">✂️</div>`}
      <div class="t"><b>${esc(p.title)}</b><small>${p.clip_count||0} clips · ${fmt((p.duration_seconds||0)*24,24)} · rev ${p.revision}${p.media_state==='missing_media'?' · <span style="color:var(--red)">missing media</span>':''}</small></div>
      <span style="color:var(--t-3)">›</span></a>`).join('')
    :`<div class="empty">No cuts yet. Pick something below, or tap ✂️ on any gallery item in the studio.</div>`;
  let gal=[];try{gal=await api('/api/gallery');}catch(e){}
  const picks=[];
  const grid=$('#pickgrid');
  const draw=()=>{grid.innerHTML=gal.filter(x=>['video','image','music'].includes(x.kind)).slice(0,60).map(x=>{
      const on=picks.includes(x.id);
      return `<div class="it${on?' picked':''}" data-id="${esc(x.id)}"><img loading="lazy" src="${esc(x.poster||x.url)}"><div class="p">${esc(x.title||x.prompt||x.id)}</div>
        <div class="k">${esc(x.kind)}</div><div class="pk">${on?'✓':''}</div></div>`;}).join('')||`<div class="empty">The gallery is empty.</div>`;
    $('#pickgo').disabled=!picks.length;$('#pickgo').textContent=picks.length?`✂️ Cut these together (${picks.length})`:'✂️ Cut these together';};
  grid.onclick=e=>{const it=e.target.closest('.it');if(!it)return;const id=it.dataset.id;const i=picks.indexOf(id);if(i>=0)picks.splice(i,1);else picks.push(id);draw();};
  $('#pickgo').onclick=async()=>{$('#pickgo').disabled=true;try{const d=await api('/api/cut/projects',{job_ids:picks});location.href='/cut?project='+encodeURIComponent(d.project_id);}catch(e){toast(e.message,true);$('#pickgo').disabled=false;}};
  draw();
}

/* ======================= editor state ======================= */
let P=null, SEL=null, PH=0, ZOOM=6, TOOL='trim', PLAYING=false, RAF=0, LASTT=0, GAL=null, RENDERS=[], POLL=0, BUSY=false;
const fps=()=>P?P.settings.fps:24;
const A=id=>(P.assets||[]).find(a=>a.id===id);
const vtrack=()=>P.timeline.tracks.find(t=>t.type==='video');
const clips=()=>P.timeline.tracks.flatMap(t=>t.clips.map(c=>({...c,_track:t})));
const clip=id=>clips().find(c=>c.id===id);
const v1clips=()=>[...vtrack().clips].sort((a,b)=>a.start_frame-b.start_frame||a.id.localeCompare(b.id));
const at=f=>v1clips().find(c=>f>=c.start_frame&&f<c.start_frame+c.duration_frames);
const nextOf=c=>v1clips().find(n=>n.start_frame===c.start_frame+c.duration_frames);
const prevOf=c=>v1clips().find(n=>n.start_frame+n.duration_frames===c.start_frame);
const trans=(a,b)=>P.timeline.transitions.find(t=>t.from_clip_id===a&&t.to_clip_id===b);
function fmt(frames,f){f=f||fps();const s=Math.max(0,frames)/f;return `${String(Math.floor(s/60)).padStart(2,'0')}:${(s%60).toFixed(2).padStart(5,'0')}`;}
const secs=fr=>(fr/fps()).toFixed(2)+'s';
function tlWidth(){return Math.max(P.duration_frames*ZOOM+240,$('#tlscroll').clientWidth);}

async function load(){
  P=await api('/api/cut/projects/'+encodeURIComponent(PID));
  $('#ptitle').textContent=P.title;$('#rev').textContent='rev '+P.revision;document.title=`Cut · ${P.title}`;
  $('#undo').disabled=!P.history_state.undo_stack.length;$('#redo').disabled=!P.history_state.redo_stack.length;
  if(SEL&&!clip(SEL))SEL=null;
  PH=Math.min(PH,Math.max(0,P.duration_frames));
  drawTimeline();drawBins();panel();seek(PH,true);
  loadPending();loadRenders();
}
async function tx(commands,label){
  if(BUSY)return null;BUSY=true;
  try{
    const r=await api(`/api/cut/projects/${encodeURIComponent(PID)}/commands`,{commands,transaction_id:crypto.randomUUID(),expected_revision:P.revision});
    await load();if(label)toast(label);return r;
  }catch(e){
    toast(e.message,true);
    if(/revision conflict/.test(e.message)){try{await load();}catch(x){}}
    return null;
  }finally{BUSY=false;}
}
const cmd=(type,payload)=>({id:`ui-${type.replace('.','-')}-${crypto.randomUUID().slice(0,8)}`,type,payload:payload||{}});

/* ======================= timeline drawing ======================= */
function drawTimeline(){
  const f=fps(), W=tlWidth();
  const inner=$('#tlinner');inner.style.width=W+'px';
  // ruler: one labelled tick per N seconds so labels never crowd
  const secPx=f*ZOOM;const step=secPx>90?1:secPx>45?2:secPx>18?5:secPx>9?10:30;
  let ticks='';const total=Math.ceil(W/secPx);
  for(let s=0;s<=total;s++){const x=s*secPx;if(s%step===0)ticks+=`<div class="tick" style="left:${x}px">${fmt(s*f)}</div>`;else if(secPx>18)ticks+=`<div class="tick minor" style="left:${x}px"></div>`;}
  $('#ruler').innerHTML=ticks;
  const posters=new Map();for(const a of P.assets||[]){posters.set(a.id,a.poster||(a.kind==='image'?a.source.path:null));}
  let html='';
  for(const t of P.timeline.tracks){
    const kind=t.type==='video'?'video':t.type==='music'?'music':t.type==='captions'?'captions':'audio';
    if(kind==='audio'&&!t.clips.length)continue;
    html+=`<div class="track ${kind}" data-track="${esc(t.id)}"><span class="tn">${esc(t.name)}</span>`;
    const items=kind==='captions'?P.timeline.captions.items.map(c=>({id:c.id,start_frame:c.start_frame,duration_frames:c.end_frame-c.start_frame,label:c.text,_cap:true})):t.clips;
    for(const c of items){
      const a=c.asset_id?A(c.asset_id):null;const miss=a&&a.source&&a.source.exists===false;
      const grade=P.timeline.color[c.id];
      const poster=a?posters.get(a.id):null;
      html+=`<div class="clip${SEL===c.id?' sel':''}${c.media_kind==='image'?' image':''}${miss?' missing':''}" data-id="${esc(c.id)}" data-cap="${c._cap?1:0}" style="left:${c.start_frame*ZOOM}px;width:${Math.max(30,c.duration_frames*ZOOM)}px">
        ${poster&&!c._cap?`<div class="thumb" style="background-image:url('${esc(poster)}')"></div>`:''}
        <div class="lbl">${c._cap?'💬 ':''}${esc(c.label||c.id)}</div>
        ${a&&a.has_audio&&kind==='video'?`<div class="wave${(c.audio||{}).muted?' mute':''}"></div>`:''}
        ${grade?`<div class="grade">🎨 ${esc(grade.preset)}</div>`:''}
        <div class="dur">${secs(c.duration_frames)}</div>
        <div class="hd l"></div><div class="hd r"></div></div>`;
    }
    if(kind==='video')for(const tr of P.timeline.transitions){const to=t.clips.find(c=>c.id===tr.to_clip_id);if(to)html+=`<div class="trans" data-from="${esc(tr.from_clip_id)}" data-to="${esc(tr.to_clip_id)}" title="${esc(tr.kind)} ${tr.duration_frames}f" style="left:${to.start_frame*ZOOM}px">◐</div>`;}
    html+=`</div>`;
  }
  $('#tracks').innerHTML=html;
  $('#dur').textContent=fmt(P.duration_frames);
  placePlayhead();
}
function placePlayhead(){$('#playhead').style.left=(PH*ZOOM)+'px';$('#tc').textContent=fmt(PH);}
function ensureVisible(){const sc=$('#tlscroll');const x=PH*ZOOM;if(x<sc.scrollLeft+20||x>sc.scrollLeft+sc.clientWidth-40)sc.scrollLeft=Math.max(0,x-sc.clientWidth*0.3);}

/* ======================= player ======================= */
const vid=$('#vid'), img=$('#img');
let CUR=null; // asset id currently loaded in the monitor
function seek(frame,force){
  PH=Math.max(0,Math.min(frame,Math.max(0,P.duration_frames)));placePlayhead();
  const c=at(PH), no=$('#nomedia');
  const cap=P.timeline.captions.items.find(i=>PH>=i.start_frame&&PH<i.end_frame);
  $('#capov').style.display=cap?'block':'none';$('#capov').innerHTML=cap?`<span>${esc(cap.text)}</span>`:'';
  if(!c){vid.classList.remove('on');img.classList.remove('on');no.className='nomedia';no.textContent=P.duration_frames?'Gap — nothing plays here':'Add a clip to start';no.style.display='flex';$('#screen').style.filter='';return;}
  const a=A(c.asset_id);
  $('#screen').style.filter=cssGrade(P.timeline.color[c.id]);
  if(!a||a.source.exists===false){vid.classList.remove('on');img.classList.remove('on');no.className='nomedia missing';no.textContent=`Missing media: ${a?a.source.path:c.id}`;no.style.display='flex';return;}
  no.style.display='none';
  const t=(c.trim_in_frame+(PH-c.start_frame))/fps();
  if(a.kind==='image'){vid.classList.remove('on');if(CUR!==a.id){img.src=a.source.path;CUR=a.id;}img.classList.add('on');if(!vid.paused)vid.pause();}
  else{img.classList.remove('on');vid.classList.add('on');
    if(CUR!==a.id){vid.src=a.source.path;CUR=a.id;vid.load();}
    if(force||Math.abs(vid.currentTime-t)>0.08){try{vid.currentTime=t;}catch(e){}}
    vid.muted=!!(c.audio&&c.audio.muted);vid.volume=Math.max(0,Math.min(1,Math.pow(10,((c.audio||{}).gain_db||0)/20)));
  }
}
function cssGrade(g){if(!g)return '';const i=g.intensity;const m={warm:`sepia(${.25*i}) saturate(${1+.12*i})`,cool:`hue-rotate(${-8*i}deg) saturate(${1+.06*i}) brightness(${1+.02*i})`,punchy:`contrast(${1+.18*i}) saturate(${1+.28*i})`,soft:`contrast(${1-.1*i}) brightness(${1+.03*i}) saturate(${1-.08*i})`,bw:`grayscale(${i}) contrast(1.08)`,vintage:`sepia(.4) contrast(.9) saturate(.85)`,dramatic:`contrast(1.35)`};return m[g.preset]||'';}
vid.addEventListener('error',()=>{const no=$('#nomedia');no.className='nomedia missing';no.textContent='This video could not be played';no.style.display='flex';});
function play(){
  if(!P.duration_frames)return;
  if(PH>=P.duration_frames)PH=0;
  PLAYING=true;$('#play').textContent='❚❚';LASTT=performance.now();
  const c=at(PH);seek(PH,true);if(c&&A(c.asset_id)&&A(c.asset_id).kind==='video')vid.play().catch(()=>{});
  cancelAnimationFrame(RAF);RAF=requestAnimationFrame(tick);
}
function pause(){PLAYING=false;$('#play').textContent='▶';vid.pause();cancelAnimationFrame(RAF);}
function tick(now){
  if(!PLAYING)return;
  const c=at(PH);
  if(!c){PH+= (now-LASTT)/1000*fps();}
  else{const a=A(c.asset_id);
    if(a&&a.kind==='video'&&vid.classList.contains('on')&&!vid.paused){PH=c.start_frame+(vid.currentTime*fps()-c.trim_in_frame);}
    else PH+=(now-LASTT)/1000*fps();
    if(PH>=c.start_frame+c.duration_frames){PH=c.start_frame+c.duration_frames;const n=at(PH);seek(PH,true);if(n&&A(n.asset_id)&&A(n.asset_id).kind==='video')vid.play().catch(()=>{});}
    else{placePlayhead();const cap=P.timeline.captions.items.find(i=>PH>=i.start_frame&&PH<i.end_frame);$('#capov').style.display=cap?'block':'none';if(cap)$('#capov').innerHTML=`<span>${esc(cap.text)}</span>`;}
  }
  LASTT=now;ensureVisible();
  if(PH>=P.duration_frames){PH=P.duration_frames;pause();seek(PH,true);return;}
  RAF=requestAnimationFrame(tick);
}
$('#play').onclick=()=>PLAYING?pause():play();
$('#zoomin').onclick=()=>setZoom(ZOOM*1.5);$('#zoomout').onclick=()=>setZoom(ZOOM/1.5);
$('#zoomfit').onclick=()=>setZoom(($('#tlscroll').clientWidth-60)/Math.max(1,P.duration_frames));
function setZoom(z,anchorFrame){z=Math.max(0.4,Math.min(60,z));const sc=$('#tlscroll');const af=anchorFrame??PH;const off=af*ZOOM-sc.scrollLeft;ZOOM=z;drawTimeline();sc.scrollLeft=Math.max(0,af*ZOOM-off);}

/* ======================= timeline gestures ======================= */
(function(){
  const sc=$('#tlscroll');
  const pts=new Map();let mode=null,drag=null,pinch=null,pressT=0,moved=false;
  const frameAt=x=>{const r=$('#tlinner').getBoundingClientRect();return Math.round((x-r.left)/ZOOM);};
  const snap=(f,ignore)=>{const cand=[0,PH,...v1clips().filter(c=>c.id!==ignore).flatMap(c=>[c.start_frame,c.start_frame+c.duration_frames])];let best=f;for(const c of cand)if(Math.abs(c-f)*ZOOM<8)best=c;return Math.max(0,best);};
  sc.addEventListener('pointerdown',e=>{
    if(e.button&&e.button!==0)return;
    // a pointer whose "up" never reached us (lost capture, re-rendered target) must
    // not haunt the next gesture as a phantom second finger
    for(const [id,pt] of pts)if(performance.now()-pt.t>4000)pts.delete(id);
    if(mode==='pinch'&&pts.size<2){mode=null;pinch=null;}
    pts.set(e.pointerId,{x:e.clientX,y:e.clientY,t:performance.now()});
    if(pts.size===2){clearTimeout(pressT);mode='pinch';const [a,b]=[...pts.values()];pinch={d:Math.hypot(a.x-b.x,a.y-b.y),z:ZOOM,f:frameAt((a.x+b.x)/2)};drag=null;return;}
    const tr=e.target.closest('.trans');if(tr){SEL=tr.dataset.to;setTool('transition');drawTimeline();return;}
    const el=e.target.closest('.clip');moved=false;
    if(el){
      const id=el.dataset.id, cap=el.dataset.cap==='1';
      const c=cap?null:clip(id);
      if(SEL!==id){SEL=id;drawTimeline();panel();}
      const hd=e.target.closest('.hd');
      if(hd&&!cap){mode='trim';drag={id,side:hd.classList.contains('l')?'l':'r',c,x0:e.clientX,el:$(`.clip[data-id="${CSS.escape(id)}"]`)};try{sc.setPointerCapture(e.pointerId);}catch(x){}return;}
      if(cap){mode='captap';return;}
      mode='press';drag={id,c,x0:e.clientX,f0:frameAt(e.clientX)};
      clearTimeout(pressT);pressT=setTimeout(()=>{if(mode==='press'&&!moved){mode='move';drag.el=$(`.clip[data-id="${CSS.escape(id)}"]`);drag.el.classList.add('drag');navigator.vibrate&&navigator.vibrate(12);}},330);
      try{sc.setPointerCapture(e.pointerId);}catch(x){}return;
    }
    mode='scrub';if(PLAYING)pause();seek(frameAt(e.clientX),true);try{sc.setPointerCapture(e.pointerId);}catch(x){}
  });
  sc.addEventListener('pointermove',e=>{
    if(!pts.has(e.pointerId))return;pts.set(e.pointerId,{x:e.clientX,y:e.clientY,t:performance.now()});
    if(mode==='pinch'&&pts.size===2){const [a,b]=[...pts.values()];const d=Math.hypot(a.x-b.x,a.y-b.y);setZoom(pinch.z*d/pinch.d,pinch.f);return;}
    if(mode==='scrub'){seek(frameAt(e.clientX),true);return;}
    if(!drag)return;
    const dx=e.clientX-drag.x0;if(Math.abs(dx)>6)moved=true;
    if(mode==='press'&&moved){clearTimeout(pressT);mode='pan';}
    if(mode==='pan'){sc.scrollLeft-=e.movementX;return;}
    if(mode==='trim'){
      const c=drag.c;let df=Math.round(dx/ZOOM);
      if(drag.side==='l'){df=Math.max(-c.trim_in_frame,Math.min(c.duration_frames-1,df));drag.newIn=c.trim_in_frame+df;drag.el.style.left=(c.start_frame+df)*ZOOM+'px';drag.el.style.width=Math.max(30,(c.duration_frames-df)*ZOOM)+'px';}
      else{const lim=c.source_duration_frames?c.source_duration_frames-c.trim_out_frame:1e9;df=Math.max(-(c.duration_frames-1),Math.min(lim,df));drag.newOut=c.trim_out_frame+df;drag.el.style.width=Math.max(30,(c.duration_frames+df)*ZOOM)+'px';}
      drag.el.querySelector('.dur').textContent=secs((drag.newOut??c.trim_out_frame)-(drag.newIn??c.trim_in_frame));
      return;
    }
    if(mode==='move'){const f=snap(drag.f0+Math.round(dx/ZOOM)-(drag.f0-drag.c.start_frame),drag.id);drag.newStart=f;drag.el.style.left=f*ZOOM+'px';}
  });
  const up=async e=>{
    pts.delete(e.pointerId);clearTimeout(pressT);
    if(mode==='pinch'){if(pts.size<2){mode=null;pinch=null;}return;}
    const m=mode,d=drag;mode=null;drag=null;
    if(m==='trim'&&d&&(d.newIn!==undefined||d.newOut!==undefined)){
      const c=d.c;const tin=d.newIn??c.trim_in_frame,tout=d.newOut??c.trim_out_frame;
      const cmds=[cmd('clip.trim',{clip_id:c.id,trim_in_frames:tin,trim_out_frames:tout})];
      if(d.side==='l'&&tin!==c.trim_in_frame)cmds.push(cmd('clip.move',{clip_id:c.id,track_id:c._track.id,start_frame:c.start_frame+(tin-c.trim_in_frame)}));
      await tx(cmds,`Trimmed to ${secs(tout-tin)}`);return;
    }
    if(m==='trim'){drawTimeline();return;}
    if(m==='move'&&d){d.el&&d.el.classList.remove('drag');if(d.newStart!==undefined&&d.newStart!==d.c.start_frame){await tx([cmd('clip.move',{clip_id:d.id,track_id:d.c._track.id,start_frame:d.newStart})],'Moved');}else drawTimeline();return;}
    if(m==='press'&&d&&!moved){seek(Math.max(d.c.start_frame,Math.min(d.c.start_frame+d.c.duration_frames-1,d.f0)),true);}
    if(m==='captap'){setTool('captions');}
  };
  sc.addEventListener('pointerup',up);sc.addEventListener('pointercancel',up);sc.addEventListener('lostpointercapture',e=>{if(mode&&mode!=='pinch')up(e);});
  sc.addEventListener('wheel',e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();setZoom(ZOOM*(e.deltaY<0?1.12:0.89),frameAt(e.clientX));}},{passive:false});
  document.addEventListener('keydown',e=>{
    if(e.target.matches('input,textarea'))return;
    if(e.code==='Space'){e.preventDefault();PLAYING?pause():play();}
    else if(e.key==='ArrowLeft'){seek(PH-(e.shiftKey?fps():1),true);}
    else if(e.key==='ArrowRight'){seek(PH+(e.shiftKey?fps():1),true);}
    else if(e.key==='s'&&SEL){doSplit();}
    else if((e.key==='Delete'||e.key==='Backspace')&&SEL){removeSel();}
    else if((e.metaKey||e.ctrlKey)&&e.key==='z'){e.preventDefault();e.shiftKey?redo():undo();}
  });
})();

/* ======================= history ======================= */
const undo=()=>tx([cmd('undo')],'Undone');const redo=()=>tx([cmd('redo')],'Redone');
$('#undo').onclick=undo;$('#redo').onclick=redo;

/* ======================= bins (desktop) ======================= */
async function drawBins(){
  $('#assetlist').innerHTML=(P.assets||[]).map(a=>`<div class="asset" data-asset="${esc(a.id)}">${a.poster||a.kind==='image'?`<img src="${esc(a.poster||a.source.path)}" alt="">`:`<div class="ph">${a.kind==='music'?'🎵':'🎬'}</div>`}
    <div class="t">${esc(a.title)}<br><small>${esc(a.kind)}${a.duration_seconds?' · '+a.duration_seconds.toFixed(1)+'s':''}${a.source.exists===false?' · <span style="color:var(--red)">missing</span>':''}</small></div></div>`).join('')||'<div class="empty">Nothing yet</div>';
  $('#assetlist').onclick=e=>{const el=e.target.closest('.asset');if(!el)return;const a=A(el.dataset.asset);if(a)addJob(a.job_id);};
  if(!GAL){try{GAL=await api('/api/gallery');}catch(e){GAL=[];}}
  const inProject=new Set((P.assets||[]).map(a=>a.job_id));
  const rows=GAL.filter(x=>['video','image','music'].includes(x.kind)).slice(0,40);
  const html=rows.map(x=>`<div class="it" data-job="${esc(x.id)}"><img loading="lazy" src="${esc(x.poster||x.url)}"><div class="p">${inProject.has(x.id)?'✓ ':''}${esc(x.title||x.prompt||x.id)}</div><div class="k">${esc(x.kind)}</div></div>`).join('')||'<div class="empty">The gallery is empty.</div>';
  $('#galgrid').innerHTML=html;$('#galgrid').onclick=galClick;
  if(TOOL==='add'){const g=$('#panelgal');if(g){g.innerHTML=html;g.onclick=galClick;}}
}
function galClick(e){const el=e.target.closest('.it');if(el)addJob(el.dataset.job);}
async function addJob(job){await tx([cmd('clip.add',{job_id:job,start_frame:null})],'Added to the end');}

/* ======================= tools panel ======================= */
$('#toolrow').onclick=e=>{const b=e.target.closest('.tool');if(b)setTool(b.dataset.tool);};
function setTool(t){TOOL=t;$$('.tool').forEach(b=>b.classList.toggle('on',b.dataset.tool===t));panel();}
function selClip(){return SEL?clip(SEL):null;}
function needSel(what){const c=selClip();if(!c)return `<div class="hint" style="margin-top:0">Tap a clip on the timeline first${what?' to '+what:''}.</div>`;return '';}
function panel(){
  const p=$('#panel');const c=selClip();const f=fps();
  const head=c?`<div class="kv"><span>${esc(c.label||c.id)}</span><b>${fmt(c.start_frame)} → ${fmt(c.start_frame+c.duration_frames)}</b></div>`:'';
  if(TOOL==='add'){p.innerHTML=`<div class="label">Add from the gallery</div><div class="hint" style="margin:0 0 10px">Tap an item to add it at the end of the timeline. Pictures stay ${P.settings.still_seconds||4}s.</div><div id="panelgal" class="gal small"></div>`;drawBins();return;}
  if(TOOL==='trim'){
    if(!c){p.innerHTML=`<div class="label">Trim</div>`+needSel('trim it');return;}
    const lim=c.source_duration_frames;
    p.innerHTML=`<div class="label">Trim · ${esc(c.label||c.id)}</div>${head}
      <div class="kv"><span>Starts inside the source at</span><b id="tin">${fmt(c.trim_in_frame)}</b></div>
      <div class="stepper"><button class="ib" data-d="-${f}" data-k="in">−1s</button><button class="ib" data-d="-1" data-k="in">−1f</button><span class="v">In</span><button class="ib" data-d="1" data-k="in">+1f</button><button class="ib" data-d="${f}" data-k="in">+1s</button></div>
      <div class="kv"><span>Ends inside the source at</span><b id="tout">${fmt(c.trim_out_frame)}</b></div>
      <div class="stepper"><button class="ib" data-d="-${f}" data-k="out">−1s</button><button class="ib" data-d="-1" data-k="out">−1f</button><span class="v">Out</span><button class="ib" data-d="1" data-k="out">+1f</button><button class="ib" data-d="${f}" data-k="out">+1s</button></div>
      <div class="hint">Length ${secs(c.duration_frames)}${lim?` · source is ${secs(lim)}`:' · a picture can be any length'}. Or drag the clip's edges.</div>`;
    let tin=c.trim_in_frame,tout=c.trim_out_frame,timer;
    p.querySelectorAll('.stepper .ib').forEach(b=>b.onclick=()=>{const d=+b.dataset.d;if(b.dataset.k==='in')tin=Math.max(0,Math.min(tout-1,tin+d));else tout=Math.max(tin+1,Math.min(lim||1e9,tout+d));
      $('#tin').textContent=fmt(tin);$('#tout').textContent=fmt(tout);clearTimeout(timer);timer=setTimeout(()=>tx([cmd('clip.trim',{clip_id:c.id,trim_in_frames:tin,trim_out_frames:tout})],`Trimmed to ${secs(tout-tin)}`),600);});
    return;
  }
  if(TOOL==='split'){
    const target=c&&PH>c.start_frame&&PH<c.start_frame+c.duration_frames?c:at(PH);
    p.innerHTML=`<div class="label">Split</div>${target?`<div class="kv"><span>${esc(target.label||target.id)}</span><b>at ${fmt(PH)}</b></div><button class="go" id="dosplit">✂️ Split here</button><div class="hint">Makes two clips you can trim or move on their own. Scrub the playhead to choose where.</div>`:`<div class="hint" style="margin-top:0">Put the playhead inside a clip, then split.</div>`}`;
    const b=$('#dosplit');if(b)b.onclick=()=>doSplit(target);return;
  }
  if(TOOL==='move'){
    if(!c){p.innerHTML=`<div class="label">Move</div>`+needSel('move it');return;}
    const n=nextOf(c),pv=prevOf(c);
    p.innerHTML=`<div class="label">Move · ${esc(c.label||c.id)}</div>${head}
      <div class="stepper"><button class="ib" data-d="-${f}">−1s</button><button class="ib" data-d="-1">−1f</button><span class="v">Nudge</span><button class="ib" data-d="1">+1f</button><button class="ib" data-d="${f}">+1s</button></div>
      <div class="row"><button class="btn2" id="mv_start">⇤ Start</button><button class="btn2" id="mv_ph">To playhead</button><button class="btn2" id="mv_end">End ⇥</button></div>
      <div class="row">${pv?`<button class="btn2" id="mv_before">⇠ Swap back</button>`:''}${n?`<button class="btn2" id="mv_after">Swap forward ⇢</button>`:''}<button class="btn2" id="mv_del" style="color:var(--red)">🗑 Remove</button></div>
      <div class="hint">Or press and hold a clip, then drag it. Clips snap to each other.</div>`;
    const mv=s=>tx([cmd('clip.move',{clip_id:c.id,track_id:c._track.id,start_frame:Math.max(0,s)})],'Moved');
    p.querySelectorAll('.stepper .ib').forEach(b=>b.onclick=()=>mv(c.start_frame+ +b.dataset.d));
    $('#mv_start').onclick=()=>mv(0);$('#mv_ph').onclick=()=>mv(PH);
    $('#mv_end').onclick=()=>{const end=Math.max(...c._track.clips.filter(x=>x.id!==c.id).map(x=>x.start_frame+x.duration_frames),0);mv(end);};
    if(n)$('#mv_after').onclick=()=>tx([cmd('clip.move',{clip_id:n.id,track_id:c._track.id,start_frame:c.start_frame}),cmd('clip.move',{clip_id:c.id,track_id:c._track.id,start_frame:c.start_frame+n.duration_frames})],'Swapped');
    if(pv)$('#mv_before').onclick=()=>tx([cmd('clip.move',{clip_id:c.id,track_id:c._track.id,start_frame:pv.start_frame}),cmd('clip.move',{clip_id:pv.id,track_id:c._track.id,start_frame:pv.start_frame+c.duration_frames})],'Swapped');
    $('#mv_del').onclick=removeSel;return;
  }
  if(TOOL==='transition'){
    if(!c||c._track.type!=='video'){p.innerHTML=`<div class="label">Dissolve</div>`+needSel('put a dissolve in front of it');return;}
    const pv=prevOf(c);const n=nextOf(c);
    const pair=pv?[pv,c]:n?[c,n]:null;
    if(!pair){p.innerHTML=`<div class="label">Dissolve</div><div class="hint" style="margin-top:0">A dissolve needs two clips touching each other. Move a clip right up against this one first.</div>`;return;}
    const cur=trans(pair[0].id,pair[1].id);const kinds=['dissolve','fadeblack','fadewhite','wipeleft','wiperight','slideleft','slideright'];
    const maxF=Math.min(pair[0].duration_frames,pair[1].duration_frames)-1;
    p.innerHTML=`<div class="label">Dissolve · ${esc(pair[0].label||pair[0].id)} → ${esc(pair[1].label||pair[1].id)}</div>
      <div class="chips" id="trkinds">${kinds.map(k=>`<div class="chip${(cur?cur.kind:'dissolve')===k?' sel':''}" data-k="${k}">${k}</div>`).join('')}</div>
      <div class="label">Length</div><div class="chips" id="trlen">${[Math.round(f/4),Math.round(f/2),f,f*2].filter(x=>x<maxF).map(x=>`<div class="chip${(cur?cur.duration_frames:Math.round(f/2))===x?' sel':''}" data-f="${x}">${secs(x)}</div>`).join('')}</div>
      <div class="row"><button class="btn2 gold" id="tr_set">${cur?'Update':'Add'} dissolve</button>${cur?`<button class="btn2" id="tr_rm">Remove</button>`:''}</div>
      <div class="hint">The two clips overlap for the length of the dissolve, so the finished video is that much shorter.</div>`;
    let kind=cur?cur.kind:'dissolve',len=cur?cur.duration_frames:Math.round(f/2);
    $('#trkinds').onclick=e=>{const ch=e.target.closest('.chip');if(!ch)return;kind=ch.dataset.k;$$('#trkinds .chip').forEach(x=>x.classList.toggle('sel',x===ch));};
    $('#trlen').onclick=e=>{const ch=e.target.closest('.chip');if(!ch)return;len=+ch.dataset.f;$$('#trlen .chip').forEach(x=>x.classList.toggle('sel',x===ch));};
    $('#tr_set').onclick=()=>tx([cmd('transition.set',{from_clip_id:pair[0].id,to_clip_id:pair[1].id,kind,duration_frames:len})],'Dissolve set');
    if(cur)$('#tr_rm').onclick=()=>tx([cmd('transition.remove',{from_clip_id:pair[0].id,to_clip_id:pair[1].id})],'Dissolve removed');
    return;
  }
  if(TOOL==='captions'){
    const items=P.timeline.captions.items;
    p.innerHTML=`<div class="label">Captions</div>
      <input type="text" id="captext" placeholder="Type what should appear on screen…" maxlength="300">
      <div class="row"><button class="btn2 gold" id="capadd">＋ Show for 3s at ${fmt(PH)}</button>${c&&c._track.type==='video'?`<button class="btn2" id="capclip">＋ Over this clip</button>`:''}</div>
      <div class="rlist">${items.map(i=>`<div class="capitem" data-id="${esc(i.id)}"><div class="t">${esc(i.text)}</div><small>${fmt(i.start_frame)}–${fmt(i.end_frame)}</small><button class="ib" data-act="edit" title="Edit">✎</button><button class="ib" data-act="rm" title="Remove">🗑</button></div>`).join('')||'<div class="hint">No captions yet. They are burned into the render and previewed on the monitor.</div>'}</div>`;
    $('#capadd').onclick=()=>{const t=$('#captext').value.trim();if(!t)return toast('Type the caption first',true);tx([cmd('caption.add',{text:t,start_frame:PH,end_frame:PH+3*f})],'Caption added');};
    const cc=$('#capclip');if(cc)cc.onclick=()=>{const t=$('#captext').value.trim();if(!t)return toast('Type the caption first',true);tx([cmd('caption.add',{text:t,start_frame:c.start_frame,end_frame:c.start_frame+c.duration_frames})],'Caption added');};
    p.querySelector('.rlist').onclick=e=>{const b=e.target.closest('button');if(!b)return;const id=b.closest('.capitem').dataset.id;const it=items.find(x=>x.id===id);
      if(b.dataset.act==='rm')tx([cmd('caption.remove',{caption_id:id})],'Caption removed');
      else{const t=prompt('Caption text',it.text);if(t&&t.trim())tx([cmd('caption.edit',{caption_id:id,text:t.trim()})],'Caption updated');}};
    return;
  }
  if(TOOL==='audio'){
    const mx=P.timeline.mix;const ca=c?(c.audio||{gain_db:0,muted:false}):null;const a=c?A(c.asset_id):null;
    p.innerHTML=`<div class="label">Audio</div>
      ${c&&a&&(a.has_audio||a.kind==='music')?`<div class="kv"><span>${esc(c.label||c.id)}</span><b id="cg">${ca.gain_db>0?'+':''}${ca.gain_db} dB${ca.muted?' · muted':''}</b></div>
        <input type="range" class="slider" id="cgain" min="-30" max="12" step="1" value="${ca.gain_db}">
        <div class="row"><button class="btn2" id="cmute">${ca.muted?'🔈 Unmute clip':'🔇 Mute clip'}</button></div>`:c?`<div class="hint" style="margin-top:0">This clip has no sound of its own.</div>`:''}
      <div class="label">Clip sound</div><div class="kv"><span>Gain</span><b id="dg">${mx.dialogue.gain_db} dB</b></div><input type="range" class="slider" id="dgain" min="-30" max="12" step="1" value="${mx.dialogue.gain_db}">
      <div class="chips"><div class="chip${mx.dialogue.normalize?' sel':''}" id="dnorm">${mx.dialogue.normalize?'✓ ':''}Even out loudness <small>${mx.dialogue.target_lufs} LUFS</small></div></div>
      <div class="label">Music</div><div class="kv"><span>Gain</span><b id="mg">${(mx.music||{}).gain_db??-12} dB</b></div><input type="range" class="slider" id="mgain" min="-40" max="6" step="1" value="${(mx.music||{}).gain_db??-12}">
      <div class="label">Everything</div><div class="kv"><span>Master</span><b id="ag">${(mx.master||{}).gain_db??0} dB</b></div><input type="range" class="slider" id="again" min="-20" max="6" step="1" value="${(mx.master||{}).gain_db??0}">`;
    const bind=(id,lab,fn)=>{const el=$('#'+id);if(!el)return;el.oninput=()=>{$('#'+lab).textContent=(el.value>0?'+':'')+el.value+' dB';};el.onchange=()=>fn(+el.value);};
    bind('cgain','cg',v=>tx([cmd('audio.mix',{target:'clip',clip_id:c.id,gain_db:v})],'Clip gain set'));
    bind('dgain','dg',v=>tx([cmd('audio.mix',{target:'dialogue',gain_db:v})],'Clip sound set'));
    bind('mgain','mg',v=>tx([cmd('audio.mix',{target:'music',gain_db:v})],'Music set'));
    bind('again','ag',v=>tx([cmd('audio.mix',{target:'master',gain_db:v})],'Master set'));
    const cm=$('#cmute');if(cm)cm.onclick=()=>tx([cmd('audio.mix',{target:'clip',clip_id:c.id,muted:!ca.muted})],ca.muted?'Unmuted':'Muted');
    $('#dnorm').onclick=()=>tx([cmd('audio.mix',{target:'dialogue',normalize:!mx.dialogue.normalize,target_lufs:-16})],'Loudness updated');
    return;
  }
  if(TOOL==='color'){
    if(!c||c._track.type!=='video'){p.innerHTML=`<div class="label">Color</div>`+needSel('give it a look');return;}
    const cur=P.timeline.color[c.id]||{preset:'neutral',intensity:1};
    const looks=[['neutral','Natural'],['warm','Warm'],['cool','Cool'],['punchy','Punchy'],['soft','Soft'],['bw','Black & white'],['vintage','Vintage'],['dramatic','Dramatic']];
    p.innerHTML=`<div class="label">Color · ${esc(c.label||c.id)}</div>
      <div class="chips" id="looks">${looks.map(([k,l])=>`<div class="chip${cur.preset===k?' sel':''}" data-k="${k}">${l}</div>`).join('')}</div>
      <div class="kv" style="margin-top:8px"><span>Strength</span><b id="ci">${Math.round(cur.intensity*100)}%</b></div><input type="range" class="slider" id="cint" min="10" max="100" step="10" value="${Math.round(cur.intensity*100)}">
      <div class="hint">The monitor shows a close preview; the render applies the real grade.</div>`;
    $('#looks').onclick=e=>{const ch=e.target.closest('.chip');if(!ch)return;tx([cmd('color.apply',{clip_id:c.id,preset:ch.dataset.k,intensity:(+$('#cint').value)/100})],'Look applied');};
    $('#cint').oninput=()=>{$('#ci').textContent=$('#cint').value+'%';};
    $('#cint').onchange=()=>{if(cur.preset!=='neutral')tx([cmd('color.apply',{clip_id:c.id,preset:cur.preset,intensity:(+$('#cint').value)/100})],'Strength set');};
    return;
  }
  if(TOOL==='render'){renderPanel();return;}
}
async function doSplit(target){
  const c=target||(SEL&&clip(SEL))||at(PH);
  if(!c||PH<=c.start_frame||PH>=c.start_frame+c.duration_frames)return toast('Put the playhead inside a clip first',true);
  const r=await tx([cmd('clip.split',{clip_id:c.id,at_frame:PH})],'Split');
  if(r){const out=(r.outputs||[]).find(o=>o.new_clip_id);if(out){SEL=out.new_clip_id;drawTimeline();panel();}}
}
async function removeSel(){const c=selClip();if(!c)return;if(!confirm(`Remove "${c.label||c.id}" from the timeline? (Undo brings it back.)`))return;await tx([cmd('clip.remove',{clip_id:c.id})],'Removed');}

/* ======================= render ======================= */
let RQ='preview',RFMT='mp4',RCAP=true,RPOLL=0;
function renderPanel(){
  const p=$('#panel');const approved=P.approval.master_render_approved===true;
  const qual=[['preview','Preview','fast, small'],['medium','Medium','good'],['high','High','sharp'],['master','Master','needs approval']];
  const live=RENDERS.find(r=>r.status==='queued'||r.status==='running');
  p.innerHTML=`<div class="label">Render</div>
    <div class="chips" id="rq">${qual.map(([k,l,s])=>`<div class="chip${RQ===k?' sel':''}" data-k="${k}">${l} <small>${s}</small></div>`).join('')}</div>
    <div class="chips" style="margin-top:8px"><div class="chip${RFMT==='mp4'?' sel':''}" data-fmt="mp4">MP4</div><div class="chip${RFMT==='webm'?' sel':''}" data-fmt="webm">WebM</div><div class="chip${RCAP?' sel':''}" id="rcap">${RCAP?'✓ ':''}Burn captions</div></div>
    ${RQ==='master'?`<div class="chips" style="margin-top:8px"><div class="chip${approved?' sel':''}" id="rapprove">${approved?'✓ Approved for master':'Approve this cut for master'}</div></div>`:''}
    <button class="go" id="rgo" ${live?'disabled':''}>${live?'Rendering…':`🎬 Render ${RQ}`}</button>
    ${live?`<div class="prog"><i style="width:${Math.round((live.progress||0)*100)}%"></i></div><div class="hint">${esc(live.stage)} · ${Math.round((live.progress||0)*100)}%</div>`:`<div class="hint">Renders on the CPU, never the GPU queue. The finished file lands in the gallery as a new item you can re-cut.</div>`}
    <div class="rlist">${RENDERS.filter(r=>r.status==='done'||r.status==='error').slice(0,6).map(r=>`<div class="rline"><span class="st ${r.status}">${r.status}</span><span style="flex:1">${esc((r.request||{}).quality)} · rev ${r.revision}${r.receipt?` · ${(r.receipt.bytes/1048576).toFixed(1)} MB`:''}${r.message?` · <span style="color:var(--red)">${esc(r.message)}</span>`:''}</span>${r.url?`<a class="btn2 gold" style="flex:0 0 auto;padding:8px 12px" href="/?job=${encodeURIComponent(r.gallery_job_id)}">Open in studio ›</a>`:''}</div>`).join('')}</div>`;
  $('#rq').onclick=e=>{const ch=e.target.closest('.chip');if(!ch)return;RQ=ch.dataset.k;renderPanel();};
  p.querySelectorAll('[data-fmt]').forEach(ch=>ch.onclick=()=>{RFMT=ch.dataset.fmt;renderPanel();});
  $('#rcap').onclick=()=>{RCAP=!RCAP;renderPanel();};
  const ap=$('#rapprove');if(ap)ap.onclick=()=>tx([cmd('project.approve',{master_render_approved:!approved})],approved?'Master approval removed':'Approved for master');
  $('#rgo').onclick=async()=>{
    if(RQ==='master'&&!approved)return toast('Approve the cut for master first',true);
    if(RQ==='master'&&!confirm('Render the MASTER of this cut now?'))return;
    try{await api(`/api/cut/projects/${encodeURIComponent(PID)}/render`,{quality:RQ,format:RFMT,burn_captions:RCAP,explicit_approval:RQ==='master'});toast('Render started');await loadRenders();}
    catch(e){toast(e.message,true);}
  };
}
async function loadRenders(){
  try{const d=await api(`/api/cut/projects/${encodeURIComponent(PID)}/renders`);RENDERS=d.renders||[];}catch(e){RENDERS=[];}
  if(TOOL==='render')renderPanel();
  clearTimeout(RPOLL);
  if(RENDERS.some(r=>r.status==='queued'||r.status==='running'))RPOLL=setTimeout(loadRenders,1500);
  else if(RENDERS.length&&RENDERS[0].status==='done'&&RENDERS[0]._fresh!==false){/* noop */}
}

/* ======================= Sparky proposals ======================= */
let PENDING=[];
async function loadPending(){
  try{const d=await api(`/api/cut/projects/${encodeURIComponent(PID)}/pending`);PENDING=d.pending||[];}catch(e){PENDING=[];}
  $('#pending').innerHTML=PENDING.map(pr=>`<div class="card prop" data-tx="${esc(pr.transaction_id)}">
    <div class="kv" style="border:0;padding:0"><span class="who">✨ Sparky proposes ${pr.commands.length} change${pr.commands.length===1?'':'s'}</span><small style="color:var(--t-3)">on rev ${pr.base_revision}</small></div>
    <div class="chips" style="margin-top:8px">${pr.commands.map(c=>`<span class="chip" style="cursor:default">${esc(c.type)}</span>`).join('')}</div>
    <div class="diff">${(pr.diff||[]).slice(0,80).map(d=>`<div><span class="op">${esc(d.op)}</span> ${esc(d.path)}${d.before!==undefined?` <span class="b">− ${esc(JSON.stringify(d.before)).slice(0,160)}</span>`:''}${d.after!==undefined?` <span class="a">+ ${esc(JSON.stringify(d.after)).slice(0,160)}</span>`:''}</div>`).join('')||'no change'}</div>
    <div class="row"><button class="btn2 gold" data-act="approve">✓ Approve exact diff</button><button class="btn2" data-act="reject">✕ Reject</button></div></div>`).join('');
  $('#pending').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const id=b.closest('.prop').dataset.tx;
    try{await api(`/api/cut/projects/${encodeURIComponent(PID)}/review/${encodeURIComponent(id)}`,{approve:b.dataset.act==='approve'});toast(b.dataset.act==='approve'?'Applied':'Rejected');await load();}catch(x){toast(x.message,true);await load();}};
}
async function pollRevision(){
  try{const d=await api('/api/cut/projects/'+encodeURIComponent(PID));if(P&&d.revision!==P.revision&&!BUSY){await load();toast('Updated from another editor');}}catch(e){}
  try{const d=await api(`/api/cut/projects/${encodeURIComponent(PID)}/pending`);if((d.pending||[]).length!==PENDING.length)await loadPending();}catch(e){}
  POLL=setTimeout(pollRevision,6000);
}

/* ======================= boot ======================= */
(async()=>{
  if(!PID){document.getElementById('app').dataset.view='picker';return picker();}
  $('#editor').hidden=false;document.getElementById('app').dataset.view='editor';
  try{await load();}catch(e){$('#ptitle').textContent='Could not open this cut';toast(e.message,true);return;}
  setTool(window.innerWidth>=900?'trim':'trim');
  if(!SEL&&v1clips().length){SEL=v1clips()[0].id;drawTimeline();panel();}
  pollRevision();
})();
