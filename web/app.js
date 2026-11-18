'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {starting:'启动中',running:'运行中',stopping:'停止请求中',completed:'正常结束',failed:'失败',stopped:'协作停止',terminated:'强制终止',interrupted:'执行器中断'};
const groupLabels = {preflight:'前检',formal:'正式实验','':'未分组'};
const active = status => ['starting','running','stopping'].includes(status);
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
const timeLabel = n => n ? new Date(n * 1000).toLocaleString('zh-CN',{hour12:false}) : '—';
const duration = n => `${Math.floor(n / 3600)}:${String(Math.floor(n / 60) % 60).padStart(2,'0')}:${String(Math.floor(n) % 60).padStart(2,'0')}`;
let current = {tasks:[], attempts:[]}, token = '', taskId = '', attemptId = '';
let view = {groups:{},task_id:'',attempt_id:''}, viewLoaded = false, viewWrite = Promise.resolve();
const outputCache = new Map();
let stages = new Map(), metrics = new Map(), artifacts = new Map(), lastEvent = 0, errors = 0, checkpoint = '', polling = false;

async function api(path, value, keepalive=false) {
 const response = await fetch(path, value === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-MLWB-Token':token},body:JSON.stringify(value),keepalive});
 const data = await response.json();
 if (!response.ok) throw new Error(data.error || response.statusText);
 return data;
}
const htmlCache = new Map();
function putHtml(id, html) {
 const element = $(id);
 if (htmlCache.get(id) === html) return;
 htmlCache.set(id, html);
 const expanded = new Map([...element.querySelectorAll('details')].map((d,i) => [d.dataset.group ?? i,d.open]));
 element.innerHTML = html;
 element.querySelectorAll('details').forEach((d,i) => { d.open = expanded.get(d.dataset.group ?? i) ?? d.open; });
}
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function persistView() {
 const snapshot = {...view,groups:{...view.groups}};
 // Preserve action order when a user switches faster than requests finish.
 viewWrite = viewWrite.then(() => api('/api/view',snapshot,true)).catch(error => notice(`界面状态未保存：${error.message}`));
 return viewWrite;
}
function rememberSelection() {
 if (!viewLoaded || (view.task_id === taskId && view.attempt_id === attemptId)) return;
 view.task_id = taskId; view.attempt_id = attemptId;
 persistView();
}
function selected() { return current.attempts.find(a => a.id === attemptId); }
function restoreOutput(entry) {
 const view = entry?.view;
 stages = new Map(Object.entries(view?.stages || {})); metrics = new Map(Object.entries(view?.metrics || {}));
 artifacts = new Map(Object.entries(view?.artifacts || {})); lastEvent = view?.last_event || 0;
 errors = view?.errors || 0; checkpoint = view?.checkpoint || '';
 const box = $('log'), follow = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
 const tail = view?.event_tail || [];
 const eventsOnly = !view?.log && tail.length > 0;
 $('log-size').textContent = eventsOnly ? '脚本无文本输出 · 显示最近 32 条结构化事件' : 'stdout / stderr · 显示最近 49,152 字符';
 const text = view?.log || (eventsOnly ? tail.map(([t, line]) => timeLabel(t) + '  ' + line).join('\n') :
  !attemptId ? '选择一次执行以查看日志。' : !view ? '正在读取本次输出…' : '尚无文本输出或结构化事件。');
 if (box.textContent !== text) box.textContent = text;
 if (follow) box.scrollTop = box.scrollHeight;
 $('worker-log').textContent = view?.worker_log || '没有执行器错误日志。';
 const selection = entry?.metric || $('metric').value;
 if (JSON.stringify([...$('metric').options].map(o => o.value)) !== JSON.stringify([...metrics.keys()])) {
  $('metric').replaceChildren(...[...metrics.keys()].map(key => new Option(key,key)));
 }
 if (metrics.has(selection)) $('metric').value = selection;
 const signature = attemptId + JSON.stringify([...artifacts.values()]);
 if ($('artifacts').dataset.signature !== signature) {
  $('artifacts').dataset.signature = signature;
  putHtml('artifacts', [...artifacts.values()].map(a => {
   const url = `/api/attempt/${attemptId}/artifact?path=` + encodeURIComponent(a.path);
   const image = /\.(png|jpg|jpeg|webp|gif)$/i.test(a.path);
   return `<div class="artifact">${image ? `<a href="${esc(url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(url)}" alt="${esc(a.label || a.path)}"></a>` : ''}<a href="${esc(url)}" ${image ? 'target="_blank" rel="noopener"' : 'download'}>${esc(a.label || a.path)} ↗</a></div>`;
  }).join('') || '<p class="empty">尚无登记产物。</p>');
 }
}
function selectAttempt(id) {
 if (id === attemptId) { rememberSelection(); return; }
 const previous = outputCache.get(attemptId);
 if (previous) previous.metric = $('metric').value;
 attemptId = id;
 htmlCache.clear();
 restoreOutput(outputCache.get(id));
 render();
 rememberSelection();
}

function renderTaskGroups() {
 const projects = new Map();
 for (const task of current.tasks) {
  if (!projects.has(task.project)) projects.set(task.project,[]);
  projects.get(task.project).push(task);
 }
 return [...projects].map(([project,tasks]) => `<section class="project-group"><h3>${esc(project)}</h3>${Object.entries(groupLabels).map(([group,label]) => {
  const members = tasks.filter(t => (t.group || '') === group);
  if (!members.length) return '';
  const key = JSON.stringify([project,group]);
  return `<details class="task-group" data-group="${esc(key)}"${view.groups[key] ? ' open' : ''}><summary>${label}<span>${members.length}</span></summary>${members.map(t => `<button class="task-nav ${t.id === taskId ? 'selected' : ''}" data-task="${esc(t.id)}" ${t.id === taskId ? 'aria-current="true"' : ''}>${esc(t.name)}<small>${esc({experiment:'实验',training:'训练',inference:'推理',demo:'演示'}[t.kind])}</small></button>`).join('')}</details>`;
 }).join('')}</section>`).join('') || '<p class="empty">添加任务，开始组织你的运行。</p>';
}
function render() {
 putHtml('tasks', renderTaskGroups());
 const task = current.tasks.find(t => t.id === taskId);
 const busy = current.attempts.some(a => active(a.status));
 putHtml('task-card', task ? `<div class="task-top"><div><div class="eyebrow">${esc(task.project)} / ${groupLabels[task.group || '']}</div><h2>${esc(task.name)}</h2><div class="task-detail">${esc(task.cwd)}</div></div><div><button data-action="edit">编辑</button> <button class="primary" data-action="start" ${busy ? 'disabled' : ''}>启动新运行 ↗</button></div></div><div class="command">${esc(JSON.stringify(task.argv))}</div>${task.kind === 'demo' ? '<p class="muted">独立 CPU 合成演示，不使用 ML 模型或研究数据。</p>' : ''}` : '<div class="task-top"><div><h2>让下一次运行有迹可循。</h2><p class="muted">登记一个任务，或添加 CPU 演示体验完整流程。</p></div><button class="primary" data-action="add">添加任务</button></div>');
 const history = current.attempts.filter(a => !taskId || a.task_id === taskId);
 $('run-count').textContent = history.length;
 putHtml('history', history.map(a => `<button class="history-item ${a.id === attemptId ? 'selected' : ''}" data-attempt="${a.id}"><div class="row"><span>${a.parent ? '恢复尝试' : '新运行'}</span>${badge(a.status)}</div><small>${timeLabel(a.created)}</small><small>${a.id.slice(0,8)} · ${esc(a.spec.name)}</small></button>`).join('') || '<p class="empty">尚无执行记录。</p>');
 const row = selected();
 $('attempt-label').textContent = row ? row.id.slice(0,8) + (outputCache.get(row.id)?.more ? ' · 同步新增记录中' : '') : '尚未选择';
 if (!row) {
  putHtml('summary', '<p class="empty">启动任务后，这里会显示阶段、耗时和执行状态。</p>');
  $('actions').replaceChildren(); $('stages').replaceChildren(); drawChart(); return;
 }
 const elapsed = Math.max(0, (row.ended || current.server_time) - (row.started || row.created));
 putHtml('summary', `<div class="stats"><div class="stat"><label>执行状态</label>${badge(row.status)}<small>退出码 ${row.exit_code ?? '—'}</small></div><div class="stat"><label>本次墙钟耗时</label><strong>${duration(elapsed)}</strong><small>含系统休眠时间</small></div><div class="stat"><label>最近事件</label><strong>${lastEvent ? `${Math.max(0,Math.floor(current.server_time-lastEvent))} 秒前` : '尚未上报'}</strong><small>${lastEvent ? timeLabel(lastEvent) : '普通日志仍可查看'}</small></div></div>${row.error ? `<p class="error-text">${esc(row.error)}</p>` : ''}${checkpoint ? `<p class="muted">脚本上报检查点：${esc(checkpoint)}（内容由脚本校验）</p>` : ''}${errors ? `<p class="error-text">有 ${errors} 处事件或指标未能展示（格式无效或超出数量上限）；原始事件仍保留。</p>` : ''}<details><summary>本次实际命令与身份</summary><div class="command">${esc(JSON.stringify(row.parent ? row.spec.resume_argv : row.spec.argv))}\n工作目录：${esc(row.spec.cwd)}\n逻辑运行：${row.run_id}\n前次尝试：${row.parent || '无'}</div></details>`);
 let actions = `<a href="/api/attempt/${row.id}/export" download="attempt-${row.id.slice(0,8)}.json">导出执行记录 ↗</a>`;
 if (active(row.status)) actions = `${row.spec.cooperative_stop ? `<button data-action="stop" ${row.status === 'stopping' ? 'disabled' : ''}>请求安全停止</button>` : ''}<button class="danger" data-action="force">强制终止</button> ` + actions;
 else if (row.spec.resume_argv.length && ['failed','stopped','terminated','interrupted'].includes(row.status) && !current.attempts.some(a => a.parent === row.id)) actions = `<button class="primary" data-action="resume" ${busy ? 'disabled' : ''}>从检查点恢复</button> ` + actions;
 putHtml('actions', actions);
 putHtml('stages', [...stages.values()].map(s => `<div class="stage-row"><span>${esc(s.stage)}</span><div class="track"><progress ${s.total !== null ? `value="${s.done}" max="${s.total || 1}"` : ''} aria-label="${esc(s.stage)}进度"></progress></div><span class="stage-count">${s.done}/${s.total ?? '?'} ${esc(s.unit || '')}</span></div>`).join('') || '<p class="empty">尚无结构化阶段信息；不估算未知进度。</p>');
 drawChart();
}
async function readOutput() {
 const id = attemptId; if (!id) return;
 const previous = outputCache.get(id);
 const result = await api(`/api/attempt/${id}/monitor?since=${previous?.revision ?? -1}`);
 const entry = {revision:result.revision, view:result.view || previous?.view, metric:previous?.metric, more:result.more};
 outputCache.set(id,entry);
 if (id !== attemptId) return;
 if (!result.unchanged) restoreOutput(entry);
 if (result.more) setTimeout(refresh,0);
}

function drawChart() {
 const canvas = $('chart'), bounds = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
 canvas.width = bounds.width * dpr; canvas.height = bounds.height * dpr;
 const ctx = canvas.getContext('2d'); ctx.scale(dpr,dpr);
 const points = metrics.get($('metric').value) || [];
 $('chart-empty').hidden = points.length > 0;
 if (!points.length) { $('metric-value').textContent = ''; return; }
 const w = bounds.width, h = bounds.height, left = 52, right = 18, top = 15, bottom = 28;
 let lo = Math.min(...points.map(p => p.v)), hi = Math.max(...points.map(p => p.v));
 const pad = (hi - lo) * .1 || Math.max(Math.abs(hi) * .05,.01); lo -= pad; hi += pad;
 ctx.font = '10px Segoe UI'; ctx.textAlign = 'right'; ctx.fillStyle = '#84938e'; ctx.strokeStyle = '#edf1ee';
 for (let i=0;i<5;i++) { const y=top+(h-top-bottom)*i/4; ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();ctx.fillText((hi-(hi-lo)*i/4).toPrecision(3),left-9,y+3); }
 const minT = points[0].t, span = points.at(-1).t - minT || 1;
 const x = p => left + (p.t-minT)/span*(w-left-right), y = p => top+(hi-p.v)/(hi-lo)*(h-top-bottom);
 ctx.beginPath();points.forEach((p,i) => i ? ctx.lineTo(x(p),y(p)) : ctx.moveTo(x(p),y(p)));ctx.lineWidth=2;ctx.strokeStyle='#198c78';ctx.stroke();
 const last=points.at(-1);ctx.beginPath();ctx.arc(x(last),y(last),3,0,Math.PI*2);ctx.fillStyle='#198c78';ctx.fill();
 ctx.fillStyle='#84938e';ctx.textAlign='left';ctx.fillText('0 s',left,h-7);ctx.textAlign='right';ctx.fillText(`${(last.t-minT).toFixed(1)} s`,w-right,h-7);
 $('metric-value').textContent = `最新 ${last.v.toPrecision(5)} · ${points.length} 个已上报点 · 横轴为显示窗口内时间`;
}
async function refresh() {
 if (polling) return; polling=true;
 try {
  current = await api('/api/state'); token=current.token;
  if (!viewLoaded) { view=await api('/api/view'); taskId=view.task_id; }
  if (!current.tasks.some(t=>t.id===taskId)) taskId=current.tasks[0]?.id || '';
  const wanted = viewLoaded ? attemptId : view.attempt_id;
  viewLoaded=true;
  selectAttempt(current.attempts.find(a=>a.id===wanted && a.task_id===taskId)?.id || current.attempts.find(a=>a.task_id===taskId)?.id || '');
  await readOutput(); render(); $('connection').textContent='● 本机已连接';
 } catch(error) { $('connection').textContent='连接中断'; notice(error.message); }
 finally { polling=false; }
}
function openForm(task) {
 $('task-id').value=task?.id || ''; $('project').value=task?.project || ''; $('name').value=task?.name || '';
 $('cwd').value=task?.cwd || ''; $('kind').value=task?.kind || 'experiment';
 $('task-group').value=task?.group || '';
 $('argv').value=task ? JSON.stringify(task.argv,null,2) : ''; $('resume-argv').value=task?.resume_argv.length ? JSON.stringify(task.resume_argv,null,2) : '';
 $('artifact-root').value=task?.artifact_root || ''; $('cooperative').checked=task?.cooperative_stop || false;
 $('form-error').textContent=''; $('form-title').textContent=task ? '编辑任务定义' : '添加任务'; $('task-dialog').showModal();
}
$('add-task').onclick=()=>openForm(); $('close-dialog').onclick=()=>$('task-dialog').close();
$('add-demo').onclick=async()=>{try {const value=await api('/api/demo',{});taskId=value.id;selectAttempt('');notice('已添加 CPU 演示。点击“启动新运行”后才会执行。');await refresh();}catch(error){notice(error.message);}};
$('task-form').onsubmit=async event=>{
 event.preventDefault();
 try {
  const value={id:$('task-id').value || undefined,name:$('name').value,project:$('project').value,cwd:$('cwd').value,kind:$('kind').value,group:$('task-group').value,argv:JSON.parse($('argv').value),resume_argv:JSON.parse($('resume-argv').value || '[]'),artifact_root:$('artifact-root').value,cooperative_stop:$('cooperative').checked};
  taskId=(await api('/api/tasks',value)).id;selectAttempt('');$('task-dialog').close();notice('任务已保存。已有执行的配置保持原样。');await refresh();
 } catch(error) { $('form-error').textContent=error.message; }
};
document.addEventListener('toggle',event=>{
 const group=event.target;
 if (!viewLoaded || !group.isConnected || !group.matches('details.task-group')) return;
 const key=group.dataset.group;
 if ((view.groups[key] || false) === group.open) return;
 view.groups[key]=group.open;
 persistView();
},true);
document.addEventListener('click',async event=>{
 const button=event.target.closest('button'); if (!button) return;
 if (button.dataset.task) {taskId=button.dataset.task;selectAttempt(current.attempts.find(a=>a.task_id===taskId)?.id || '');render();await refresh();return;}
 if (button.dataset.attempt) {selectAttempt(button.dataset.attempt);await refresh();return;}
 const action=button.dataset.action; if (!action) return;
 try {
  if (action==='add') return openForm();
  if (action==='edit') return openForm(current.tasks.find(t=>t.id===taskId));
  button.disabled=true;
  if (action==='force' && !confirm('强制终止会清理本次任务进程树，可能丢失尚未保存的工作。确定继续？')) return;
  if (action==='start') selectAttempt((await api('/api/start',{task_id:taskId})).id);
  if (action==='resume') selectAttempt((await api('/api/resume',{id:attemptId})).id);
  if (action==='stop' || action==='force') await api('/api/stop',{id:attemptId,force:action==='force'});
  notice('');await refresh();
 } catch(error) { notice(error.message); }
 finally {button.disabled=false;}
});
$('metric').onchange=()=>{const entry=outputCache.get(attemptId);if(entry) entry.metric=$('metric').value;drawChart();}; window.addEventListener('resize',drawChart);
refresh();setInterval(refresh,1200);
