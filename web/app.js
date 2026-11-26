'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {starting:'启动中',running:'运行中',stopping:'停止请求中',lost:'心跳超时',completed:'正常结束',failed:'失败',stopped:'协作停止',terminated:'强制终止',interrupted:'执行器中断'};
const liveStatuses = new Set(['starting','running','stopping','lost']);
const presetGroupOrder = {'前检':0,'正式实验':1,'':99};
const groupDisplay = raw => {
 const text = String(raw ?? '').trim();
 if (text === 'preflight') return '前检';
 if (text === 'formal') return '正式实验';
 return text;
};
const active = status => ['starting','running','stopping'].includes(status);
const liveRank = status => (status === 'running' || status === 'starting') ? 3 : status === 'stopping' ? 2 : status === 'lost' ? 1 : 0;
const dotClass = status => status === 'starting' ? 'running' : status;
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
const timeLabel = n => n ? new Date(n * 1000).toLocaleString('zh-CN',{hour12:false}) : '—';
const duration = n => `${Math.floor(n / 3600)}:${String(Math.floor(n / 60) % 60).padStart(2,'0')}:${String(Math.floor(n) % 60).padStart(2,'0')}`;
let current = {tasks:[], attempts:[], project_roots:{}}, token = '', taskId = '', attemptId = '', sourceFilter = 'all';
let view = {groups:{},task_id:'',attempt_id:''}, viewLoaded = false, viewWrite = Promise.resolve();
const outputCache = new Map();
const fetchingIds = new Set();
let pendingFetch = null;
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
 const expanded = new Map([...element.querySelectorAll('details')].map((d,i) => [d.dataset?.group ?? i,d.open]));
 element.innerHTML = html;
 element.querySelectorAll('details').forEach((d,i) => {
  const key = d.dataset?.group;
  if (key !== undefined && Object.prototype.hasOwnProperty.call(view.groups, key)) {
   d.open = view.groups[key];
  } else {
   d.open = expanded.get(key ?? i) ?? d.open;
  }
 });
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

function classifyTask(task) {
 const parts = String(task.project || '').split('/').map(s => s.trim()).filter(Boolean);
 const groupText = groupDisplay(task.group);
 if (parts.length >= 2) {
  return {topProject: parts[0], subGroup: parts.slice(1).join(' / '), extraTag: groupText};
 }
 return {topProject: parts[0] || '未命名项目', subGroup: groupText, extraTag: ''};
}
function buildTaskMeta() {
 const meta = new Map();
 for (const task of current.tasks) {
  meta.set(task.id, {
   latest: null,
   activeAttempt: null,
   liveAttempt: null,
   isLive: false,
   isActive: false,
   liveRank: 0,
   lastActivity: Number.isFinite(task.last_activity) ? task.last_activity : 0,
   info: classifyTask(task),
  });
 }
 for (const a of current.attempts) {
  const entry = meta.get(a.task_id);
  if (!entry) continue;
  if (!entry.latest || (a.created || 0) > (entry.latest.created || 0)) entry.latest = a;
  if (active(a.status) && (!entry.activeAttempt || (a.created || 0) > (entry.activeAttempt.created || 0))) {
   entry.activeAttempt = a;
  }
  if ((a.created || 0) > entry.lastActivity) entry.lastActivity = a.created || 0;
 }
 for (const entry of meta.values()) {
  if (entry.activeAttempt) entry.liveAttempt = entry.activeAttempt;
  else if (entry.latest && entry.latest.status === 'lost') entry.liveAttempt = entry.latest;
  else entry.liveAttempt = null;
  entry.liveRank = entry.liveAttempt ? liveRank(entry.liveAttempt.status) : 0;
  entry.isLive = Boolean(entry.liveAttempt);
  entry.isActive = Boolean(entry.activeAttempt);
 }
 return meta;
}
function renderActiveRuns(meta) {
 const box = $('active-runs');
 const taskById = new Map(current.tasks.map(t => [t.id, t]));
 const live = current.attempts.filter(a => {
  if (taskById.get(a.task_id)?.archived) return false;
  return active(a.status) || (a.status === 'lost' && meta.get(a.task_id)?.liveAttempt?.id === a.id);
 }).sort((a, b) => (liveRank(b.status) - liveRank(a.status)) || ((b.created || 0) - (a.created || 0)));
 if (!live.length) {
  box.hidden = true;
  putHtml('active-runs', '');
  return;
 }
 box.hidden = false;
 const rows = live.map(a => {
  const task = taskById.get(a.task_id);
  const info = meta.get(a.task_id)?.info || classifyTask(task || a.spec || {});
  const name = task?.name || a.spec?.name || a.id.slice(0, 8);
  const src = (a.external || task?.external) ? (a.source_label || task?.source_label || '外部') : '本机';
  const sel = (a.task_id === taskId && a.id === attemptId) ? ' selected' : '';
  return `<button type="button" class="active-run-item${sel}" data-jump-task="${esc(a.task_id)}" data-jump-attempt="${esc(a.id)}"><span class="active-run-title"><span class="status-dot ${esc(dotClass(a.status))}"></span><span>${esc(name)}</span></span><small>${esc(labels[a.status] || a.status)} · ${esc(src)} · ${esc(info.topProject)}</small></button>`;
 }).join('');
 putHtml('active-runs', `<div class="active-runs-head"><span>活跃运行</span><span>${live.length}</span></div>${rows}`);
}
function renderSourceFilter(meta) {
 const nonArchived = current.tasks.filter(t => !t.archived);
 const extTasks = nonArchived.filter(t => Boolean(t.external));
 const extLiveRank = extTasks.reduce((best, t) => Math.max(best, meta.get(t.id)?.liveRank || 0), 0);
 const extDot = extLiveRank >= 2 ? 'running' : extLiveRank === 1 ? 'lost' : '';
 const items = [
  ['all', `全部 ${nonArchived.length}`, ''],
  ['local', `本机 ${nonArchived.length - extTasks.length}`, ''],
  ['external', `外部 ${extTasks.length}`, extDot],
 ];
 putHtml('source-filter', items.map(([key, label, dot]) =>
  `<button type="button" role="tab" aria-selected="${sourceFilter === key}" class="filter-btn${sourceFilter === key ? ' active' : ''}" data-filter="${key}">${dot ? `<span class="status-dot ${dot}"></span>` : ''}${esc(label)}</button>`
 ).join(''));
}
const subgroupRank = label => Object.prototype.hasOwnProperty.call(presetGroupOrder, label) ? presetGroupOrder[label] : 2;
function taskSubtitle(task, info, entry) {
 const parts = [];
 if (info.extraTag) parts.push(info.extraTag);
 parts.push({experiment:'实验',training:'训练',inference:'推理',demo:'演示'}[task.kind] || task.kind || '任务');
 if (task.external) parts.push(task.source_label || '外部');
 if (entry?.isLive) parts.push(labels[entry.liveAttempt.status] || entry.liveAttempt.status);
 else if (task.external && entry?.latest?.status) parts.push(labels[entry.latest.status] || entry.latest.status);
 return parts.join(' · ');
}
function renderTaskButton(task, info, entry) {
 const dot = entry?.isLive ? `<span class="status-dot ${esc(dotClass(entry.liveAttempt.status))}"></span>` : '';
 return `<button class="task-nav ${task.id === taskId ? 'selected' : ''}" data-task="${esc(task.id)}" ${task.id === taskId ? 'aria-current="true"' : ''}><span class="task-title">${dot}<span class="summary-label">${esc(task.name)}</span></span><small>${esc(taskSubtitle(task, info, entry))}</small></button>`;
}
function archiveButton(taskIds, archived) {
 if (!taskIds.length) return '';
 const label = archived ? '归档' : '恢复';
 return `<button type="button" class="mini-action" data-action="archive-tasks" data-archived="${archived ? 'true' : 'false'}" data-task-ids="${esc(JSON.stringify(taskIds))}" title="${label}">${label}</button>`;
}
function renderTaskGroups(meta = buildTaskMeta()) {
 const filtered = current.tasks.filter(t => sourceFilter === 'all' ? true : sourceFilter === 'external' ? Boolean(t.external) : !t.external);
 const activeList = filtered.filter(t => !t.archived);
 const archivedList = filtered.filter(t => t.archived);

 const projectMap = new Map();
 activeList.forEach((task, idx) => {
  const entry = meta.get(task.id) || {isLive: false, isActive: false, liveRank: 0, lastActivity: 0, info: classifyTask(task), liveAttempt: null};
  const {topProject, subGroup} = entry.info;
  if (!projectMap.has(topProject)) projectMap.set(topProject, {name: topProject, order: idx, liveRank: 0, liveStatus: '', maxActivity: 0, groups: new Map(), tasks: []});
  const proj = projectMap.get(topProject);
  if (entry.liveRank > proj.liveRank) { proj.liveRank = entry.liveRank; proj.liveStatus = entry.liveAttempt.status; }
  if (entry.lastActivity > proj.maxActivity) proj.maxActivity = entry.lastActivity;
  proj.tasks.push({task, entry, info: entry.info, order: idx});
  if (!proj.groups.has(subGroup)) proj.groups.set(subGroup, {name: subGroup, order: idx, liveRank: 0, liveStatus: '', maxActivity: 0, items: []});
  const grp = proj.groups.get(subGroup);
  if (entry.liveRank > grp.liveRank) { grp.liveRank = entry.liveRank; grp.liveStatus = entry.liveAttempt.status; }
  if (entry.lastActivity > grp.maxActivity) grp.maxActivity = entry.lastActivity;
  grp.items.push({task, entry, info: entry.info, order: idx});
 });

 const projects = [...projectMap.values()].sort((a, b) =>
  (b.liveRank - a.liveRank) || (b.maxActivity - a.maxActivity) || (a.order - b.order)
 );

 const activeHtml = projects.map(proj => {
  const projKey = JSON.stringify([1, proj.name]);
  const groups = [...proj.groups.values()].sort((a, b) =>
   (b.liveRank - a.liveRank) || (b.maxActivity - a.maxActivity) || (subgroupRank(a.name) - subgroupRank(b.name)) || (a.order - b.order)
  );
  const flatUnlabeled = groups.length === 1 && groups[0].name === '';
  let body = '';
  if (flatUnlabeled) {
   const sorted = [...groups[0].items].sort((a, b) =>
    (b.entry.liveRank - a.entry.liveRank) || (b.entry.lastActivity - a.entry.lastActivity) || (a.order - b.order)
   );
   body = sorted.map(it => renderTaskButton(it.task, it.info, it.entry)).join('');
  } else {
   body = groups.map(grp => {
    const sorted = [...grp.items].sort((a, b) =>
     (b.entry.liveRank - a.entry.liveRank) || (b.entry.lastActivity - a.entry.lastActivity) || (a.order - b.order)
    );
    const subKey = JSON.stringify([2, proj.name, grp.name]);
    const archIds = sorted.filter(it => !it.entry.isActive).map(it => it.task.id);
    const subDot = grp.liveStatus ? `<span class="status-dot ${esc(dotClass(grp.liveStatus))}"></span>` : '';
    return `<details class="sub-group task-group" data-group="${esc(subKey)}"${view.groups[subKey] ? ' open' : ''}><summary>${subDot}<span class="summary-label">${esc(grp.name || '未分组')}</span>${archiveButton(archIds, true)}<span class="summary-count">${sorted.length}</span></summary>${sorted.map(it => renderTaskButton(it.task, it.info, it.entry)).join('')}</details>`;
   }).join('');
  }
  const projArchIds = proj.tasks.filter(it => !it.entry.isActive).map(it => it.task.id);
  const projDot = proj.liveStatus ? `<span class="status-dot ${esc(dotClass(proj.liveStatus))}"></span>` : '';
  return `<details class="project-node task-group" data-group="${esc(projKey)}"${view.groups[projKey] ? ' open' : ''}><summary>${projDot}<span class="summary-label">${esc(proj.name)}</span>${archiveButton(projArchIds, true)}<span class="summary-count">${proj.tasks.length}</span></summary>${body}</details>`;
 }).join('');

 let archivedHtml = '';
 if (archivedList.length) {
  const archMap = new Map();
  archivedList.forEach((task, idx) => {
   const entry = meta.get(task.id) || {isLive: false, isActive: false, liveRank: 0, lastActivity: 0, info: classifyTask(task), liveAttempt: null};
   const topProject = entry.info.topProject;
   if (!archMap.has(topProject)) archMap.set(topProject, {name: topProject, order: idx, maxActivity: 0, items: []});
   const bucket = archMap.get(topProject);
   if (entry.lastActivity > bucket.maxActivity) bucket.maxActivity = entry.lastActivity;
   bucket.items.push({task, entry, info: entry.info, order: idx});
  });
  const archProjects = [...archMap.values()].sort((a, b) => (b.maxActivity - a.maxActivity) || (a.order - b.order));
  const drawerKey = JSON.stringify([0]);
  const drawerBody = archProjects.map(bucket => {
   const apKey = JSON.stringify([3, bucket.name]);
   const sorted = [...bucket.items].sort((a, b) => (b.entry.lastActivity - a.entry.lastActivity) || (a.order - b.order));
   return `<details class="archived-project task-group" data-group="${esc(apKey)}"${view.groups[apKey] ? ' open' : ''}><summary><span class="summary-label">${esc(bucket.name)}</span>${archiveButton(sorted.map(it => it.task.id), false)}<span class="summary-count">${sorted.length}</span></summary>${sorted.map(it => renderTaskButton(it.task, it.info, it.entry)).join('')}</details>`;
  }).join('');
  archivedHtml = `<details class="archive-drawer task-group" data-group="${esc(drawerKey)}"${view.groups[drawerKey] ? ' open' : ''}><summary><span class="summary-label">已归档</span><span class="summary-count">${archivedList.length}</span></summary>${drawerBody}</details>`;
 }

 if (!activeHtml && !archivedHtml) {
  return sourceFilter === 'all' ? '<p class="empty">添加任务，开始组织你的运行。</p>' : '<p class="empty">当前来源筛选下暂无任务。</p>';
 }
 return activeHtml + archivedHtml;
}
function render() {
 const meta = buildTaskMeta();
 renderActiveRuns(meta);
 renderSourceFilter(meta);
 putHtml('tasks', renderTaskGroups(meta));
 const task = current.tasks.find(t => t.id === taskId);
 const taskEntry = task ? meta.get(task.id) : null;
 const busy = current.attempts.some(a => !a.external && active(a.status));
 let taskCardHtml = '';
 if (!task) {
  taskCardHtml = '<div class="task-top"><div><h2>让下一次运行有迹可循。</h2><p class="muted">登记一个任务，或添加 CPU 演示体验完整流程。</p></div><button class="primary" data-action="add">添加任务</button></div>';
 } else {
  const {topProject, subGroup, extraTag} = taskEntry?.info || classifyTask(task);
  const archBtnLabel = task.archived ? '取消归档' : '归档';
  const archDisabled = !task.archived && taskEntry?.isActive ? 'disabled' : '';
  const archBtn = `<button type="button" data-action="archive-tasks" data-archived="${task.archived ? 'false' : 'true'}" data-task-ids="${esc(JSON.stringify([task.id]))}" ${archDisabled}>${archBtnLabel}</button>`;
  const archBadge = task.archived ? ' <span class="badge archived">已归档</span>' : '';
  const extraCrumb = extraTag ? ` · ${esc(extraTag)}` : '';
  const crumb = `${esc(topProject)} / ${esc(subGroup || '未分组')}${extraCrumb}`;
  if (task.external) {
   taskCardHtml = `<div class="task-top"><div><div class="eyebrow">${crumb} <span class="badge external">外部观测 · ${esc(task.source_label || '外部')}</span>${archBadge}</div><h2>${esc(task.name)}</h2><div class="task-detail">同步目录：${esc(task.watch_dir || '')}${task.cwd ? ` · 远端目录：${esc(task.cwd)}` : ''}</div></div><div class="task-actions">${archBtn}</div></div><p class="muted">外部/云端运行 · 仅观测与协作停止，不占用本机任务槽</p>${task.argv?.length ? `<div class="command">${esc(JSON.stringify(task.argv))}</div>` : ''}`;
  } else {
   taskCardHtml = `<div class="task-top"><div><div class="eyebrow">${crumb}${archBadge}</div><h2>${esc(task.name)}</h2><div class="task-detail">${esc(task.cwd)}</div></div><div class="task-actions">${archBtn} <button data-action="edit">编辑</button> <button class="primary" data-action="start" ${busy ? 'disabled' : ''}>启动新运行 ↗</button></div></div><div class="command">${esc(JSON.stringify(task.argv))}</div>${task.kind === 'demo' ? '<p class="muted">独立 CPU 合成演示，不使用 ML 模型或研究数据。</p>' : ''}`;
  }
 }
 putHtml('task-card', taskCardHtml);
 const history = current.attempts.filter(a => !taskId || a.task_id === taskId);
 $('run-count').textContent = history.length;
 const runLabel = a => a.external ? `${a.parent ? '恢复尝试' : '外部运行'} · ${esc(a.source_label || '外部')}` : (a.parent ? '恢复尝试' : '新运行');
 putHtml('history', history.map(a => `<button class="history-item ${a.id === attemptId ? 'selected' : ''}" data-attempt="${esc(a.id)}"><div class="row"><span>${runLabel(a)}</span>${badge(a.status)}</div><small>${timeLabel(a.created)}</small><small>${esc(a.id.slice(0,8))} · ${esc(a.spec.name)}</small></button>`).join('') || '<p class="empty">尚无执行记录。</p>');
 const row = selected();
 $('attempt-label').textContent = row ? row.id.slice(0,8) + (outputCache.get(row.id)?.more ? ' · 同步新增记录中' : '') : '尚未选择';
 if (!row) {
  putHtml('summary', '<p class="empty">启动任务后，这里会显示阶段、耗时和执行状态。</p>');
  $('actions').replaceChildren(); $('stages').replaceChildren(); drawChart(); return;
 }
 const elapsed = Math.max(0, (row.ended || current.server_time) - (row.started || row.created));
 const rtEntries = row.external && row.runtime ? Object.entries(row.runtime) : [];
 const rtHtml = rtEntries.length ? `<p class="muted">远端运行信息：${rtEntries.map(([k, v]) => `${esc(k)}=${esc(typeof v === 'object' && v !== null ? JSON.stringify(v) : v)}`).join(' · ')}</p>` : '';
 const hbText = row.heartbeat ? `${Math.max(0, Math.floor(current.server_time - row.heartbeat))} 秒前（${timeLabel(row.heartbeat)}）` : '尚未上报';
 const fetchedHtml = row.external && row.fetched
  ? `<p class="muted">本地已拉取：${esc(row.fetched.dest)}${row.fetched.source_out_rel ? ` → ${esc(row.fetched.source_out_rel)}` : ''} · ${row.fetched.file_count} 文件 · ${timeLabel(row.fetched.fetched_at)}</p>`
  : '';
 const hbHtml = row.external ? `<p class="muted">外部来源：${esc(row.source_label || '外部')} · 最近心跳：${hbText}${row.status === 'lost' ? ` · 超过 ${row.heartbeat_timeout || 180} 秒未更新` : ''}</p>${rtHtml}${fetchedHtml}` : '';
 const idTitle = row.external ? '本次外部运行身份' : '本次实际命令与身份';
 const idBody = row.external
  ? `同步路径：${esc(row.attempt_dir || '')}\n来源：${esc(row.source_label || '外部')}\n远端命令：${esc(JSON.stringify(row.spec.argv || []))}\n远端工作目录：${esc(row.spec.cwd || '')}\n逻辑运行：${esc(row.run_id)}\n前次尝试：${esc(row.parent || '无')}`
  : `${esc(JSON.stringify(row.parent ? row.spec.resume_argv : row.spec.argv))}\n工作目录：${esc(row.spec.cwd)}\n逻辑运行：${row.run_id}\n前次尝试：${row.parent || '无'}`;
 putHtml('summary', `<div class="stats"><div class="stat"><label>执行状态</label>${badge(row.status)}<small>退出码 ${row.exit_code ?? '—'}</small></div><div class="stat"><label>本次墙钟耗时</label><strong>${duration(elapsed)}</strong><small>含系统休眠时间</small></div><div class="stat"><label>最近事件</label><strong>${lastEvent ? `${Math.max(0,Math.floor(current.server_time-lastEvent))} 秒前` : '尚未上报'}</strong><small>${lastEvent ? timeLabel(lastEvent) : '普通日志仍可查看'}</small></div></div>${hbHtml}${row.error ? `<p class="error-text">${esc(row.error)}</p>` : ''}${checkpoint ? `<p class="muted">脚本上报检查点：${esc(checkpoint)}（内容由脚本校验）</p>` : ''}${errors ? `<p class="error-text">有 ${errors} 处事件或指标未能展示（格式无效或超出数量上限）；原始事件仍保留。</p>` : ''}<details><summary>${idTitle}</summary><div class="command">${idBody}</div></details>`);
 let actions = `<a href="/api/attempt/${esc(row.id)}/export" download="attempt-${esc(row.id.slice(0,8))}.json">导出执行记录 ↗</a>`;
 if (row.external) {
  if (row.archive_available) {
   const isFetching = fetchingIds.has(row.id);
   const boundRoot = (current.project_roots || {})[row.spec.project] || '';
   const fetchLabel = isFetching ? '正在校验并拉取…' : (row.fetched ? '重新校验拉取' : '拉取到本地');
   const rootTitle = boundRoot ? `当前绑定目录：${boundRoot}（点击修改）` : '设置该项目本地根目录';
   actions = `<button class="primary" data-action="fetch-run" ${isFetching ? 'disabled' : ''}>${fetchLabel}</button> <button type="button" data-action="bind-fetch-root" title="${esc(rootTitle)}" ${isFetching ? 'disabled' : ''}>拉取目录…</button> ` + actions;
  }
  if (['starting','running','stopping','lost'].includes(row.status) && row.spec.cooperative_stop) {
   actions = `<button data-action="stop" ${row.status === 'stopping' ? 'disabled' : ''}>请求安全停止</button> ` + actions;
  }
 } else if (active(row.status)) actions = `${row.spec.cooperative_stop ? `<button data-action="stop" ${row.status === 'stopping' ? 'disabled' : ''}>请求安全停止</button>` : ''}<button class="danger" data-action="force">强制终止</button> ` + actions;
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
function renderWatchList() {
 const dirs = Array.isArray(current.watch_dirs) ? current.watch_dirs : [];
 putHtml('watch-list', dirs.map(d =>
  `<div class="watch-item"><code>${esc(d)}</code><button type="button" class="danger" data-action="remove-watch" data-watch-path="${esc(d)}">移除</button></div>`
 ).join('') || '<p class="empty">尚未配置外部观测目录。</p>');
}
async function refresh() {
 if (polling) return; polling=true;
 try {
  current = await api('/api/state'); token=current.token;
  if (!viewLoaded) { view=await api('/api/view'); taskId=view.task_id; }
  if (!current.tasks.some(t=>t.id===taskId)) {
   const visibleIds = new Set(current.tasks.filter(t => !t.archived).map(t => t.id));
   const live = current.attempts.find(a => visibleIds.has(a.task_id) && liveStatuses.has(a.status));
   taskId = live?.task_id || current.tasks.find(t => !t.archived)?.id || current.tasks[0]?.id || '';
  }
  const wanted = viewLoaded ? attemptId : view.attempt_id;
  viewLoaded=true;
  selectAttempt(current.attempts.find(a=>a.id===wanted && a.task_id===taskId)?.id || current.attempts.find(a=>a.task_id===taskId)?.id || '');
  await readOutput(); render();
  if ($('watch-dialog').open) renderWatchList();
  const watchCount = Array.isArray(current.watch_dirs) ? current.watch_dirs.length : 0;
  $('connection').textContent = watchCount ? `● 本机已连接 · 外部监测 ${watchCount} 目录` : '● 本机已连接';
 } catch(error) { $('connection').textContent='连接中断'; notice(error.message); }
 finally { polling=false; }
}
function openForm(task) {
 const projects = [], groups = ['前检', '正式实验'];
 const seenP = new Set(), seenG = new Set(groups);
 for (const t of current.tasks) {
  const {topProject, subGroup} = classifyTask(t);
  const rawProj = String(t.project || '').trim();
  for (const p of [topProject, rawProj]) {
   if (p && !seenP.has(p)) { seenP.add(p); projects.push(p); }
  }
  const g = groupDisplay(t.group) || subGroup;
  if (g && !seenG.has(g)) { seenG.add(g); groups.push(g); }
 }
 putHtml('project-suggestions', projects.map(p => `<option value="${esc(p)}"></option>`).join(''));
 putHtml('group-suggestions', groups.map(g => `<option value="${esc(g)}"></option>`).join(''));
 $('task-id').value=task?.id || ''; $('project').value=task?.project || ''; $('name').value=task?.name || '';
 $('cwd').value=task?.cwd || ''; $('kind').value=task?.kind || 'experiment';
 $('task-group').value=task?.group || '';
 $('argv').value=task ? JSON.stringify(task.argv,null,2) : ''; $('resume-argv').value=task?.resume_argv?.length ? JSON.stringify(task.resume_argv,null,2) : '';
 $('artifact-root').value=task?.artifact_root || ''; $('cooperative').checked=task?.cooperative_stop || false;
 $('form-error').textContent=''; $('form-title').textContent=task ? '编辑任务定义' : '添加任务'; $('task-dialog').showModal();
}
$('add-task').onclick=()=>openForm(); $('close-dialog').onclick=()=>$('task-dialog').close();
$('connection').onclick=()=>{renderWatchList();$('watch-error').textContent='';$('watch-dialog').showModal();};
$('close-watch-dialog').onclick=()=>$('watch-dialog').close();
function openFetchDialog({attemptId: aid, project, suggested, sourceOutRel, bindOnly=false}) {
 pendingFetch = {attemptId: aid, project, bindOnly};
 $('fetch-dialog-title').textContent = bindOnly ? '修改本地项目根目录' : '绑定本地项目根目录';
 $('fetch-project-label').textContent = `外部项目：${project}`;
 $('fetch-root-input').value = suggested || (current.project_roots || {})[project] || '';
 $('fetch-hint').textContent = sourceOutRel ? `归档内含相对路径 ${sourceOutRel}，请填写归档解压目标的项目根目录；后续同项目运行将直接沿用此目录。` : '请填写归档解压的目标根目录；后续同项目运行将直接沿用此目录。';
 $('fetch-error').textContent = '';
 $('fetch-submit').textContent = bindOnly ? '保存绑定' : '保存并拉取';
 $('fetch-dialog').showModal();
}
async function executeFetch(aid, opts={}) {
 if (!aid || fetchingIds.has(aid)) return;
 fetchingIds.add(aid);
 render();
 try {
  const res = await api('/api/fetch', {id: aid, ...opts});
  if (res.needs_root) {
   openFetchDialog({attemptId: aid, project: res.project, suggested: res.suggested_root, sourceOutRel: res.source_out_rel});
   return;
  }
  if (res.conflict) {
   const sample = (res.conflicts || []).slice(0, 5).join('\n');
   const msg = `本地已有 ${res.conflict_count} 个同名文件且内容与归档不一致（将跳过 ${res.skipped_count} 个已校验一致文件）：\n${sample}${res.conflict_count > 5 ? '\n…' : ''}\n\n确认覆盖这些本地文件吗？`;
   if (!confirm(msg)) {
    notice('已取消拉取：保留本地已有文件，未覆盖冲突文件。');
    return;
   }
   const over = await api('/api/fetch', {id: aid, ...opts, overwrite: true});
   if (over.ok) {
    notice(`SHA256 校验通过并已拉取到 ${over.dest}：新增 ${over.extracted_count}、覆盖 ${over.overwritten_count}、跳过 ${over.skipped_count}。`);
    await refresh();
   }
   return;
  }
  if (res.ok) {
   notice(`SHA256 校验通过并已拉取到 ${res.dest}：新增 ${res.extracted_count}、跳过已校验 ${res.skipped_count}。`);
   await refresh();
  }
 } catch(error) {
  notice(`拉取失败：${error.message}`);
 } finally {
  fetchingIds.delete(aid);
  render();
 }
}
$('close-fetch-dialog').onclick=()=>{pendingFetch=null;$('fetch-dialog').close();};
$('fetch-form').onsubmit=async event=>{
 event.preventDefault();
 if (!pendingFetch) return;
 const targetRoot = $('fetch-root-input').value.trim();
 if (!targetRoot) { $('fetch-error').textContent = '请填写本地项目根目录'; return; }
 const ctx = pendingFetch;
 $('fetch-error').textContent = '';
 try {
  await api('/api/project-roots', {project: ctx.project, local_root: targetRoot});
  pendingFetch = null;
  $('fetch-dialog').close();
  await refresh();
  if (ctx.bindOnly) {
   notice(`已将项目 ${ctx.project} 绑定到本地目录 ${targetRoot}。`);
  } else {
   await executeFetch(ctx.attemptId, {});
  }
 } catch(error) {
  $('fetch-error').textContent = error.message;
 }
};
$('watch-form').onsubmit=async event=>{
 event.preventDefault();
 try {
  const nextPath=$('watch-input').value.trim();
  const dirs=[...(Array.isArray(current.watch_dirs)?current.watch_dirs:[])];
  if (nextPath && !dirs.includes(nextPath)) dirs.push(nextPath);
  await api('/api/watch-dirs',{watch_dirs:dirs});
  $('watch-input').value=''; $('watch-error').textContent='';
  await refresh(); renderWatchList();
 } catch(error) { $('watch-error').textContent=error.message; }
};
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
 if (!viewLoaded || !group.isConnected || !group.matches('details[data-group]')) return;
 const key=group.dataset.group;
 if ((view.groups[key] || false) === group.open) return;
 view.groups[key]=group.open;
 persistView();
},true);
document.addEventListener('click',async event=>{
 const button=event.target.closest('button'); if (!button) return;
 if (button.dataset.filter) {
  sourceFilter = button.dataset.filter;
  render();
  return;
 }
 if (button.dataset.jumpTask) {
  const jTask = button.dataset.jumpTask;
  const jAttempt = button.dataset.jumpAttempt || '';
  if (sourceFilter !== 'all') sourceFilter = 'all';
  const target = current.tasks.find(t => t.id === jTask);
  if (target) {
   const info = classifyTask(target);
   const keys = [JSON.stringify([1, info.topProject])];
   const hasNamedSub = current.tasks.some(t => !t.archived && classifyTask(t).topProject === info.topProject && classifyTask(t).subGroup);
   if (info.subGroup || hasNamedSub) keys.push(JSON.stringify([2, info.topProject, info.subGroup || '']));
   if (target.archived) keys.push(JSON.stringify([0]), JSON.stringify([3, info.topProject]));
   let changed = false;
   for (const k of keys) {
    if (!view.groups[k]) { view.groups[k] = true; changed = true; }
   }
   if (changed && viewLoaded) persistView();
  }
  taskId = jTask;
  selectAttempt(jAttempt || current.attempts.find(a => a.task_id === jTask)?.id || '');
  await refresh();
  return;
 }
 if (button.dataset.task) {taskId=button.dataset.task;selectAttempt(current.attempts.find(a=>a.task_id===taskId)?.id || '');render();await refresh();return;}
 if (button.dataset.attempt) {selectAttempt(button.dataset.attempt);await refresh();return;}
 const action=button.dataset.action; if (!action) return;
 if (action==='archive-tasks') {
  event.preventDefault();
  event.stopPropagation();
  button.disabled=true;
  try {
   const taskIds=JSON.parse(button.dataset.taskIds || '[]');
   const archived=button.dataset.archived === 'true';
   await api('/api/archive',{task_ids:taskIds,archived});
   notice('');await refresh();
  } catch(error) { notice(error.message); }
  finally {button.disabled=false;}
  return;
 }
 if (action==='remove-watch') {
  button.disabled=true;
  try {
   const target=button.dataset.watchPath || '';
   const dirs=(Array.isArray(current.watch_dirs)?current.watch_dirs:[]).filter(d=>d!==target);
   await api('/api/watch-dirs',{watch_dirs:dirs});
   $('watch-error').textContent='';
   await refresh(); renderWatchList();
  } catch(error) { $('watch-error').textContent=error.message; }
  finally {button.disabled=false;}
  return;
 }
 try {
  if (action==='add') return openForm();
  if (action==='edit') return openForm(current.tasks.find(t=>t.id===taskId));
  if (action==='fetch-run') return executeFetch(attemptId);
  if (action==='bind-fetch-root') {
   const row = selected();
   if (row) openFetchDialog({attemptId: row.id, project: row.spec.project, suggested: (current.project_roots || {})[row.spec.project] || '', sourceOutRel: row.fetched?.source_out_rel || row.runtime?.source_out_rel || '', bindOnly: true});
   return;
  }
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
