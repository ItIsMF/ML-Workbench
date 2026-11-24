// Actual page functions with a small DOM fixture; real-browser checks are separate.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const elements = new Map();
const decode = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
function element(id) {
 if (!elements.has(id)) elements.set(id, {
  value: '', hidden: false, dataset: {}, options: [], details: [], markup: '', textContent: '',
  scrollHeight: 0, scrollTop: 0, clientHeight: 0,
  set innerHTML(html) {
   this.markup = html;
   this.details = [...html.matchAll(/<details[^>]*data-group="([^"]*)"[^>]*>/g)].map(m => ({dataset: {group: decode(m[1])}, open: m[0].includes(' open')}));
  },
  get innerHTML() { return this.markup; },
  querySelectorAll() { return this.details; },
  replaceChildren(...items) { this.options = items; this.value = items[0]?.value || ''; },
  getBoundingClientRect() { return {width: 400, height: 200}; },
  getContext() { const noop = () => {}; return {scale: noop, beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop, fillText: noop, arc: noop, fill: noop}; },
  showModal() { this.open = true; },
  close() { this.open = false; }
 });
 return elements.get(id);
}
let saved, archiveReq, prefs = {groups: {}, task_id: '', attempt_id: ''}, serverState;
const requests = [], listeners = new Map();
let releaseWrite;
function page() {
 elements.clear(); listeners.clear();
 const scope = vm.createContext({
  document: {getElementById: element, addEventListener(name, fn) { listeners.set(name, fn); }},
  window: {addEventListener() {}, devicePixelRatio: 1},
  Option: function (text, value) { return {text, value}; },
  setTimeout() {}, setInterval() {},
  fetch: async (url, request) => {
   requests.push(url);
   if (url === '/api/view' && request?.method === 'POST') {
    if (releaseWrite) await new Promise(resolve => { releaseWrite = resolve; });
    prefs = JSON.parse(request.body);
    return {ok: true, json: async () => ({ok: true})};
   }
   if (url === '/api/view') return {ok: true, json: async () => JSON.parse(JSON.stringify(prefs))};
   if (url === '/api/state') return {ok: true, json: async () => serverState};
   if (url === '/api/watch-dirs') {
    const body = JSON.parse(request.body);
    serverState.watch_dirs = body.watch_dirs;
    return {ok: true, json: async () => ({ok: true, watch_dirs: body.watch_dirs})};
   }
   if (url === '/api/archive') {
    archiveReq = JSON.parse(request.body);
    const archivedSet = new Set(serverState.tasks.filter(t => t.archived).map(t => t.id));
    for (const id of archiveReq.task_ids) {
     if (archiveReq.archived) archivedSet.add(id); else archivedSet.delete(id);
    }
    serverState.tasks = serverState.tasks.map(t => ({...t, archived: archivedSet.has(t.id)}));
    return {ok: true, json: async () => ({ok: true, archived: [...archivedSet]})};
   }
   if (url.includes('/monitor?')) return {ok: true, json: async () => ({revision: 1, view: {}})};
   saved = {url, value: JSON.parse(request.body)};
   return {ok: true, json: async () => ({id: 'pre'})};
  }
 });
 const run = code => vm.runInContext(code, scope);
 run(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8').replace('refresh();setInterval(refresh,1200);', ''));
 return run;
}

let run = page();
run(`current.tasks=[
 {id:'formal',project:'Project A',name:'Formal A',group:'formal',kind:'experiment',last_activity:10},
 {id:'pre1',project:'Project A',name:'Pre Old',group:'preflight',kind:'experiment',argv:['python.exe'],resume_argv:[],cwd:'C:/QA',last_activity:20},
 {id:'pre2',project:'Project A',name:'Pre Zh',group:'前检',kind:'experiment',last_activity:25},
 {id:'route',project:'Project A / Route State',name:'Route Probe',group:'preflight',kind:'experiment',last_activity:30},
 {id:'custom',project:'Project A',name:'Custom Group',group:'Route Process',kind:'experiment',last_activity:15},
 {id:'legacy',project:'Project A',name:'Legacy A',kind:'training',last_activity:5},
 {id:'colab',project:'GT-Backend',name:'Colab Run',kind:'experiment',external:true,source_label:'colab',last_activity:100},
 {id:'other',project:'Project <B>',name:'<script>',group:'preflight',kind:'inference',last_activity:1},
 {id:'arch1',project:'Old / Legacy',name:'Old Done',group:'formal',kind:'experiment',archived:true,last_activity:2}
];current.attempts=[
 {id:'ext_colab1',task_id:'colab',status:'running',created:100,external:true,source_label:'colab',spec:{name:'Colab Run',project:'GT-Backend'}}
];taskId='pre1';attemptId='';render();`);

const html = element('tasks').innerHTML;
assert.equal((html.match(/data-task=/g) || []).length, 9);
// 'preflight' and '前检' must merge into the same '前检' subgroup under 'Project A'.
assert.match(html, /class="sub-group task-group"[^>]*data-group="\[2,&quot;Project A&quot;,&quot;前检&quot;\]"[^>]*>.*前检.*<span class="summary-count">2<\/span>/);
// 'Project A / Route State' folds into top project 'Project A' as subgroup 'Route State' with extraTag '前检'.
assert.match(html, /class="sub-group task-group"[^>]*data-group="\[2,&quot;Project A&quot;,&quot;Route State&quot;\]"/);
assert.ok(html.includes('前检 · 实验'));
// Free-text subgroup 'Route Process' and unlabeled '未分组' render properly.
assert.match(html, /class="sub-group task-group"[^>]*data-group="\[2,&quot;Project A&quot;,&quot;Route Process&quot;\]"/);
assert.match(html, /class="sub-group task-group"[^>]*data-group="\[2,&quot;Project A&quot;,&quot;&quot;\]"[^>]*>.*未分组/);
// Live project GT-Backend rises to top and has status dot; unlabeled single-group project skips redundant 未分组 wrapper.
assert.ok(html.indexOf('GT-Backend') < html.indexOf('Project A'));
assert.match(html, /class="project-node task-group"[^>]*data-group="\[1,&quot;GT-Backend&quot;\]"[^>]*><summary><span class="status-dot running"><\/span>.*<button[^>]*data-task="colab"/);
// Active runs widget shows live Colab attempt.
assert.equal(element('active-runs').hidden, false);
assert.match(element('active-runs').innerHTML, /活跃运行.*Colab Run.*运行中 · colab · GT-Backend/);
// Archive drawer appears at bottom and escapes XSS strings.
assert.match(html, /class="archive-drawer task-group"[^>]*>.*已归档.*Old.*Old Done/);
assert.ok(html.includes('Project &lt;B&gt;') && html.includes('&lt;script&gt;') && !html.includes('<script>'));
// All new details start collapsed.
assert.ok(element('tasks').details.every(d => !d.open), 'all new groups start collapsed');

// Opening Project A preserves state when a new project is prepended, without leaking to New.
const projADetail = element('tasks').details.find(d => d.dataset.group === JSON.stringify([1, 'Project A']));
projADetail.open = true;
run("current.tasks.unshift({id:'new',project:'New',name:'New task',group:'formal',kind:'experiment',last_activity:999});render()");
const states = new Map(element('tasks').details.map(d => [d.dataset.group, d.open]));
assert.equal(states.get(JSON.stringify([1, 'Project A'])), true);
assert.equal(states.get(JSON.stringify([1, 'New'])), false);

// Source filter switches cleanly between all / local / external.
assert.match(element('source-filter').innerHTML, /全部 9.*本机 8.*外部 1/);
listeners.get('click')({target: {closest: () => ({dataset: {filter: 'external'}})}});
assert.equal((element('tasks').innerHTML.match(/data-task=/g) || []).length, 1);
assert.ok(element('tasks').innerHTML.includes('data-task="colab"'));
listeners.get('click')({target: {closest: () => ({dataset: {filter: 'all'}})}});

// Form suggestions + free-text editing keep stored group when editing and default to '' for new task.
run("openForm(current.tasks.find(t=>t.id==='pre1'))");
assert.equal(element('task-group').value, 'preflight');
assert.ok(element('group-suggestions').innerHTML.includes('Route Process'));
assert.ok(element('project-suggestions').innerHTML.includes('Project A'));
element('task-group').value = 'New Custom';
run('selectAttempt=()=>{};refresh=async()=>{};');
(async () => {
 await element('task-form').onsubmit({preventDefault() {}});
 assert.equal(saved.url, '/api/tasks');
 assert.equal(saved.value.group, 'New Custom');
 assert.equal(saved.value.id, 'pre1');
 assert.equal(element('task-dialog').open, false);
 run('openForm()');
 assert.equal(element('task-group').value, '');

 // Full refresh, toggle persistence, exact attempt restore, ordered writes, fallback & archive actions.
 serverState = {
  tasks: [
   {id: 'a', project: 'A', name: 'A', group: 'formal', archived: false, last_activity: 20},
   {id: 'b', project: 'B', name: 'B', group: 'preflight', archived: false, last_activity: 10},
   {id: 'empty', project: 'C', name: 'C', archived: false, last_activity: 5},
   {id: 'arch', project: 'Old', name: 'Archived Task', group: '', archived: true, last_activity: 1}
  ],
  attempts: [
   {id: 'a-new', task_id: 'a', status: 'running', created: 20, spec: {name: 'A', project: 'A', group: 'formal', cwd: 'C:/QA', argv: ['python.exe'], resume_argv: []}},
   {id: 'b-new', task_id: 'b', status: 'completed', created: 10, spec: {name: 'B', project: 'B', group: 'preflight', cwd: 'C:/QA', argv: ['python.exe'], resume_argv: []}},
   {id: 'a-old', task_id: 'a', status: 'completed', created: 5, spec: {name: 'A', project: 'A', group: 'formal', cwd: 'C:/QA', argv: ['python.exe'], resume_argv: []}}
  ],
  token: 'test'
 };
 run = page(); run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()'); await run('viewWrite');
 assert.ok(element('tasks').details.every(d => !d.open));
 const group = element('tasks').details.find(d => d.dataset.group === JSON.stringify([2, 'A', '正式实验']));
 Object.assign(group, {open: true, isConnected: true, matches: () => true});
 listeners.get('toggle')({target: group}); await run('viewWrite');
 assert.equal(prefs.groups[JSON.stringify([2, 'A', '正式实验'])], true);
 run("selectAttempt('a-old')"); await run('viewWrite');
 const before = requests.length; await run('refresh()'); await run('viewWrite');
 assert.ok(!requests.slice(before).includes('/api/view'), 'ordinary polling does not reread or rewrite preferences');

 run = page(); run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()'); await run('viewWrite');
 assert.equal(run('taskId'), 'a'); assert.equal(run('attemptId'), 'a-old', 'reopen restores exact older attempt');
 assert.equal(element('tasks').details.find(d => d.dataset.group === JSON.stringify([2, 'A', '正式实验'])).open, true);
 assert.equal(element('tasks').details.find(d => d.dataset.group === JSON.stringify([2, 'B', '前检'])).open, false);

 // Programmatic restore and detached details must not persist spurious toggles.
 const count = requests.length;
 listeners.get('toggle')({target: {...group, isConnected: true, matches: () => true}});
 listeners.get('toggle')({target: {...group, open: false, isConnected: false, matches: () => true}});
 await run('viewWrite'); assert.equal(requests.length, count);

 // Delay one save, then rapidly switch twice. The final saved selection must win.
 releaseWrite = true;
 run("taskId='b';selectAttempt('b-new')"); await new Promise(setImmediate);
 run("taskId='a';selectAttempt('a-new')");
 const release = releaseWrite; releaseWrite = null; release(); await run('viewWrite');
 assert.equal(prefs.task_id, 'a'); assert.equal(prefs.attempt_id, 'a-new');

 // Invalid cross-task attempt falls back to task's own latest attempt.
 prefs = {...prefs, task_id: 'b', attempt_id: 'a-old'};
 run = page(); run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()'); await run('viewWrite');
 assert.equal(run('attemptId'), 'b-new', 'never restore an attempt from a different task');

 // Empty task stays selected with empty attemptId.
 run("taskId='empty';selectAttempt('')"); await run('viewWrite');
 run = page(); run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()'); assert.equal(run('taskId'), 'empty'); assert.equal(run('attemptId'), '');

 // Removed task falls back to live/first task.
 prefs = {...prefs, task_id: 'removed', attempt_id: 'gone'};
 run = page(); run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()'); await run('viewWrite');
 assert.equal(run('taskId'), 'a'); assert.equal(run('attemptId'), 'a-new');

 // Clicking active-run jump opens target project/subgroup and resets filter.
 run = page();
 await run('refresh()');
 run("sourceFilter='external';render();");
 assert.ok(!element('tasks').innerHTML.includes('data-task="a"'));
 await listeners.get('click')({target: {closest: () => ({dataset: {jumpTask: 'b', jumpAttempt: 'b-new'}})}});
 assert.equal(run('sourceFilter'), 'all');
 assert.equal(run('taskId'), 'b');
 assert.equal(run('attemptId'), 'b-new');
 assert.equal(element('tasks').details.find(d => d.dataset.group === JSON.stringify([1, 'B'])).open, true);
 assert.equal(element('tasks').details.find(d => d.dataset.group === JSON.stringify([2, 'B', '前检'])).open, true);

 // Archive button prevents default toggle propagation and calls /api/archive.
 let prevented = false, stopped = false;
 await listeners.get('click')({
  preventDefault() { prevented = true; },
  stopPropagation() { stopped = true; },
  target: {closest: () => ({dataset: {action: 'archive-tasks', archived: 'true', taskIds: '["b"]'}, disabled: false})}
 });
 assert.ok(prevented && stopped);
 assert.deepEqual(archiveReq, {task_ids: ['b'], archived: true});
 assert.equal(serverState.tasks.find(t => t.id === 'b').archived, true);
 assert.match(element('tasks').innerHTML, /class="archive-drawer task-group"[^>]*>.*<span class="summary-count">2<\/span>/);

 // Completed external task with older lost attempt must not stay in #active-runs and displays '正常结束'.
 run(`current.tasks=[{id:'ext_done',project:'GT-Backend',name:'Done Ext',kind:'experiment',external:true,source_label:'colab'}];
  current.attempts=[
   {id:'ext_new',task_id:'ext_done',status:'completed',created:200,external:true,spec:{name:'Done Ext',project:'GT-Backend'}},
   {id:'ext_old_lost',task_id:'ext_done',status:'lost',created:100,external:true,spec:{name:'Done Ext',project:'GT-Backend'}}
  ];render();`);
 assert.equal(element('active-runs').hidden, true);
 assert.match(element('tasks').innerHTML, /实验 · colab · 正常结束/);

 // Watch-dir modal adds & removes directories via /api/watch-dirs.
 serverState.watch_dirs = [];
 run = page();
 await run('refresh()');
 element('connection').onclick();
 assert.equal(element('watch-dialog').open, true);
 element('watch-input').value = 'D:/sync_watch';
 await element('watch-form').onsubmit({preventDefault() {}});
 assert.deepEqual(serverState.watch_dirs, ['D:/sync_watch']);
 assert.match(element('watch-list').innerHTML, /D:\/sync_watch/);
 await listeners.get('click')({target: {closest: () => ({dataset: {action: 'remove-watch', watchPath: 'D:/sync_watch'}, disabled: false})}});
 assert.deepEqual(serverState.watch_dirs, []);

 assert.ok(!requests.some(url => /\/api\/(start|stop|resume)/.test(url)), 'restoring a view never executes tasks');
 console.log('Grouping UI passed: project/subgroup tree, free-text groups, active pinning, source filter, archive drawer, watch-dir modal, jump restore and read-only restore.');
})().catch(error => { console.error(error); process.exitCode = 1; });
