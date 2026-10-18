'use strict';
let token='', project=null, currentRun=null, currentPlan=null, timer=null;
const $=id=>document.getElementById(id);
function notice(message,error=false){$('notice').textContent=message;$('notice').className=error?'error':'';}
async function api(path,options={}){
 const headers={Authorization:'Bearer '+token,...(options.headers||{})};
 if(options.body && typeof options.body!=='string' && !(options.body instanceof Blob)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
 const response=await fetch(path,{...options,headers,cache:'no-store'});
 if(!response.ok){let body;try{body=await response.json();}catch{body={detail:'HTTP '+response.status};}throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));}
 return response.status===204?null:response.json();
}
function action(id,fn){$(id).onclick=async()=>{const button=$(id);button.disabled=true;try{await fn();}catch(e){notice(e.message,true);}finally{button.disabled=false;}};}
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
async function projects(){const items=await api('/projects');$('projects').replaceChildren();for(const p of items){const b=node('button',p.name,'item');b.onclick=()=>selectProject(p).catch(e=>notice(e.message,true));$('projects').append(b);}}
async function selectProject(p){project=p;currentRun=null;$('run-card').hidden=true;$('project-title').textContent=p.name;$('project-actions').hidden=false;await refreshProject();}
async function refreshProject(){if(!project)return;const [snapshots,runs]=await Promise.all([api(`/projects/${project.id}/snapshots`),api(`/projects/${project.id}/runs`)]);$('snapshots').replaceChildren();for(const s of snapshots){const option=node('option',s.id.slice(0,8)+' · '+s.digest.slice(0,12));option.value=s.id;$('snapshots').append(option);}$('runs').replaceChildren();for(const r of runs){const b=node('button',r.id.slice(0,8)+' · '+r.status,'item');b.onclick=()=>selectRun(r.id).catch(e=>notice(e.message,true));$('runs').append(b);}}
async function selectRun(id){currentRun=await api('/runs/'+id);$('run-card').hidden=false;$('run-status').textContent=currentRun.status;$('run-reason').textContent=currentRun.reason||currentRun.last_error||'';$('step-list').replaceChildren();for(const s of currentRun.steps||[]){$('step-list').append(node('p',`${s.kind} · ${s.status} · попытки ${s.attempts_used}/5`));}
 $('usage').textContent=JSON.stringify(currentRun.usage||[],null,2);$('resume').disabled=currentRun.status!=='NEEDS_INPUT';$('cancel').disabled=['READY','FAILED','CANCELLED'].includes(currentRun.status);
 currentPlan=null;try{currentPlan=await api(`/runs/${id}/plan`);$('plan').textContent=JSON.stringify(currentPlan,null,2);}catch{$('plan').textContent='План ещё не подготовлен.';}
 $('approve').disabled=currentRun.status!=='AWAITING_PLAN_APPROVAL';$('reject').disabled=$('approve').disabled;
 const [events,artifacts]=await Promise.all([api(`/runs/${id}/events`),api(`/runs/${id}/artifacts`)]);$('events').textContent=JSON.stringify(events,null,2);$('artifacts').replaceChildren();
 for(const a of artifacts){const b=node('button',`${a.kind} · ${a.manifest?.status || 'свидетельство'} · ${a.sha256.slice(0,12)}`,'secondary');b.onclick=async()=>{try{const r=await fetch(`/runs/${id}/artifacts/${a.id}`,{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw new Error('Не удалось получить пакет');const url=URL.createObjectURL(await r.blob());const link=node('a');link.href=url;link.download=`v2c-${a.id}.zip`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){notice(e.message,true);}};$('artifacts').append(b);}}
action('connect',async()=>{token=$('token').value;await projects();$('token').value='';$('login').hidden=true;$('workspace').hidden=false;notice('Подключено.');if(timer)clearInterval(timer);timer=setInterval(()=>{if(currentRun)selectRun(currentRun.id).catch(e=>notice(e.message,true));},5000);});
action('logout',async()=>{token='';project=null;currentRun=null;clearInterval(timer);$('workspace').hidden=true;$('login').hidden=false;notice('Сеанс завершён.');});
action('create-project',async()=>{const p=await api('/projects',{method:'POST',body:{name:$('project-name').value}});$('project-name').value='';await projects();await selectProject(p);});
action('import',async()=>{if(!project)throw new Error('Выберите проект');const file=$('archive').files[0];if(!file)throw new Error('Выберите архив');await api(`/projects/${project.id}/imports`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});notice('Снимок зафиксирован.');await refreshProject();});
action('import-repos',async()=>{const repositories=JSON.parse($('repositories').value);await api(`/projects/${project.id}/imports`,{method:'POST',body:{repositories}});notice('Комплект репозиториев зафиксирован.');await refreshProject();});
action('start',async()=>{if(!$('snapshots').value)throw new Error('Загрузите исходный проект');const r=await api(`/projects/${project.id}/runs`,{method:'POST',body:{snapshot_id:$('snapshots').value,idempotency_key:crypto.randomUUID()}});await refreshProject();await selectRun(r.id);});
action('refresh',async()=>{if(currentRun)await selectRun(currentRun.id);});
action('resume',async()=>{await api(`/runs/${currentRun.id}/resume`,{method:'POST'});await selectRun(currentRun.id);});
action('cancel',async()=>{await api(`/runs/${currentRun.id}/cancel`,{method:'POST'});notice('Остановка запрошена. Статус изменится после подтверждения.');await selectRun(currentRun.id);});
async function decide(approved){if(!currentPlan)throw new Error('План не подготовлен');await api(`/runs/${currentRun.id}/decisions`,{method:'POST',body:{snapshot_id:currentPlan.snapshot_id,plan_revision:currentPlan.revision,approved}});await selectRun(currentRun.id);}
action('approve',()=>decide(true));action('reject',()=>decide(false));
action('acceptance',async()=>{await api(`/runs/${currentRun.id}/acceptance`,{method:'POST',body:{task:$('accept-task').value,checks:JSON.parse($('accept-checks').value)}});notice('Контрольное задание принято.');await selectRun(currentRun.id);});
async function human(passed){if(!currentRun.final_digest)throw new Error('Конечная версия ещё не подготовлена');await api(`/runs/${currentRun.id}/handoff`,{method:'POST',body:{final_digest:currentRun.final_digest,passed,evidence:$('handoff-notes').value}});await selectRun(currentRun.id);}
action('handoff-pass',()=>human(true));action('handoff-fail',()=>human(false));
