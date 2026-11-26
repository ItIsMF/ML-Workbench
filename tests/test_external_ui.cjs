// External watch UI state checks against actual web/app.js in a minimal DOM fixture.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const elements = new Map();
const noop = () => {};
const ctx2d = { scale: noop, beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop, fillText: noop, arc: noop, fill: noop };
const decode = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

function element(id) {
 if (!elements.has(id)) {
  elements.set(id, {
   innerHTML: '',
   markup: '',
   textContent: '',
   value: '',
   hidden: false,
   scrollHeight: 0,
   scrollTop: 0,
   clientHeight: 0,
   dataset: {},
   options: [],
   details: [],
   get innerHTML() { return this.markup; },
   set innerHTML(html) {
    this.markup = html;
    this.details = [...html.matchAll(/<details[^>]*data-group="([^"]*)"[^>]*>/g)].map(m => ({ dataset: { group: decode(m[1]) }, open: m[0].includes(' open') }));
   },
   querySelectorAll() { return this.details; },
   replaceChildren(...items) { this.options = items; this.value = items[0]?.value || ''; },
   getBoundingClientRect() { return { width: 400, height: 200 }; },
   getContext() { return ctx2d; },
   showModal() { this.open = true; },
   close() { this.open = false; }
  });
 }
 return elements.get(id);
}

const serverState = {
 token: 'tok',
 server_time: 10010,
 watch_dirs: ['G:/sync'],
 tasks: [
  { id: 'ext_t1', project: 'Geo', name: 'Remote Train', kind: 'training', group: 'formal', cwd: '/content', argv: ['python', 'train.py'], resume_argv: [], cooperative_stop: true, external: true, source_label: 'Colab A100', watch_dir: 'G:/sync' },
  { id: 't_local', project: 'Geo', name: 'Local Demo', kind: 'demo', group: 'preflight', cwd: 'D:/demo', argv: ['python.exe'], resume_argv: ['python.exe', '--resume'], cooperative_stop: true },
  { id: 'ext_t2', project: 'Geo', name: 'Lost Train', kind: 'experiment', group: '', cwd: '/tmp', argv: [], resume_argv: [], cooperative_stop: true, external: true, source_label: 'Cloud', watch_dir: 'G:/sync' }
 ],
 attempts: [
  { id: 'ext_colab1', run_id: 'ext_colab1', parent: null, task_id: 'ext_t1', status: 'running', created: 10000, started: 10000, ended: null, exit_code: null, error: '', external: true, attempt_dir: 'G:/sync/colab1', heartbeat: 9990, heartbeat_timeout: 180, source_label: 'Colab A100', runtime: { platform: 'colab', html: '<img src=x onerror=alert(1)>' }, spec: { name: 'Remote Train', project: 'Geo', group: 'formal', kind: 'training', cwd: '/content', argv: ['python', 'train.py'], resume_argv: [], cooperative_stop: true } },
  { id: 'loc1', run_id: 'loc1', parent: null, task_id: 't_local', status: 'stopped', created: 9000, started: 9000, ended: 9100, exit_code: 75, error: '', spec: { name: 'Local Demo', project: 'Geo', group: 'preflight', kind: 'demo', cwd: 'D:/demo', argv: ['python.exe'], resume_argv: ['python.exe', '--resume'], cooperative_stop: true } },
  { id: 'ext_lost1', run_id: 'ext_lost1', parent: null, task_id: 'ext_t2', status: 'lost', created: 8000, started: 8000, ended: null, exit_code: null, error: '', external: true, attempt_dir: 'G:/sync/lost1', heartbeat: 7000, heartbeat_timeout: 180, source_label: 'Cloud', runtime: {}, spec: { name: 'Lost Train', project: 'Geo', group: '', kind: 'experiment', cwd: '/tmp', argv: [], resume_argv: [], cooperative_stop: true } }
 ]
};

const scope = vm.createContext({
 document: { getElementById: element, addEventListener: noop },
 window: { addEventListener: noop, devicePixelRatio: 1 },
 Option: function (text, value) { return { text, value }; },
 setTimeout: noop,
 setInterval: noop,
 fetch: async url => {
  if (url === '/api/state') return { ok: true, json: async () => serverState };
  if (url === '/api/view') return { ok: true, json: async () => ({ groups: {}, task_id: 't_local', attempt_id: 'loc1' }) };
  if (url.includes('/monitor?')) return { ok: true, json: async () => ({ revision: 1, more: false, unchanged: false, view: { stages: {}, metrics: {}, artifacts: {}, event_tail: [], last_event: 0, errors: 0, checkpoint: '', log: '', worker_log: '' } }) };
  return { ok: true, json: async () => ({ ok: true }) };
 }
});

vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8').replace('refresh();setInterval(refresh,1200);', ''), scope);
const run = code => vm.runInContext(code, scope);

(async () => {
 await run('refresh()');
 assert.equal(element('connection').textContent, '● 本机已连接 · 外部监测 1 目录');
 assert.match(element('tasks').innerHTML, /Colab A100/);
 assert.equal(element('active-runs').hidden, false);
 assert.match(element('active-runs').innerHTML, /Remote Train.*Lost Train/);
 assert.match(element('source-filter').innerHTML, /全部 3.*本机 1.*外部 2/);

 // Running external attempt must NOT lock local start or resume buttons.
 assert.match(element('task-card').innerHTML, /data-action="start" >/);
 assert.match(element('actions').innerHTML, /data-action="resume" >/);

 // External running task card and actions must omit edit/start/force/resume and escape runtime.
 run("taskId = 'ext_t1'; selectAttempt('ext_colab1')");
 assert.match(element('task-card').innerHTML, /外部观测/);
 assert.match(element('task-card').innerHTML, /不占用本机任务槽/);
 assert.ok(!element('task-card').innerHTML.includes('data-action="edit"'));
 assert.ok(!element('task-card').innerHTML.includes('data-action="start"'));
 assert.match(element('actions').innerHTML, /data-action="stop"/);
 assert.ok(!element('actions').innerHTML.includes('data-action="force"'));
 assert.ok(!element('actions').innerHTML.includes('data-action="resume"'));
 assert.match(element('summary').innerHTML, /本次外部运行身份/);
 assert.ok(element('summary').innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
 assert.ok(!element('summary').innerHTML.includes('<img src=x onerror=alert(1)>'));

 // Heartbeat-timeout external run renders 'lost' badge, allows cooperative stop only, and permits archiving.
 run("taskId = 'ext_t2'; selectAttempt('ext_lost1')");
 assert.match(element('summary').innerHTML, /class="badge lost">心跳超时</);
 assert.match(element('task-card').innerHTML, /data-action="archive-tasks" data-archived="true"[^>]*>归档</);
 assert.ok(!/data-action="archive-tasks"[^>]*disabled/.test(element('task-card').innerHTML));
 assert.match(element('actions').innerHTML, /data-action="stop"/);
 assert.ok(!element('actions').innerHTML.includes('data-action="force"'));

 // Archiving the lost external task removes it from #active-runs; replacing it with a newer completed attempt surfaces 正常结束 cleanly.
 run("current.tasks=current.tasks.map(t=>t.id==='ext_t2'?{...t,archived:true}:t);render()");
 assert.ok(!element('active-runs').innerHTML.includes('Lost Train'));
 run("current.tasks=current.tasks.map(t=>t.id==='ext_t2'?{...t,archived:false}:t);current.attempts.unshift({id:'ext_done2',run_id:'ext_done2',parent:null,task_id:'ext_t2',status:'completed',created:10020,started:10010,ended:10020,exit_code:0,error:'',external:true,source_label:'Cloud',runtime:{},spec:current.tasks[2]});render()");
 assert.ok(!element('active-runs').innerHTML.includes('Lost Train'));
 assert.match(element('tasks').innerHTML, /实验 · Cloud · 正常结束/);

 // Archive-ready external attempt renders '拉取到本地' -> after fetch renders '重新校验拉取' and escaped receipt in #summary.
 run("taskId='ext_t2';selectAttempt('ext_done2');current.attempts[0].archive_available=true;current.project_roots={'Geo':'D:/Projects/Geo'};render()");
 assert.match(element('actions').innerHTML, /data-action="fetch-run"[^>]*>拉取到本地</);
 assert.match(element('actions').innerHTML, /data-action="bind-fetch-root"[^>]*>拉取目录…</);
 run("current.attempts[0].fetched={dest:'D:/Projects/<Geo>',source_out_rel:'results/out',file_count:5,fetched_at:10010};render()");
 assert.match(element('actions').innerHTML, /data-action="fetch-run"[^>]*>重新校验拉取</);
 assert.ok(element('summary').innerHTML.includes('本地已拉取：D:/Projects/&lt;Geo&gt; → results/out · 5 文件'));

 console.log('External UI checks passed: independent local slot, read-only external task card, cooperative-only stop, lost badge, runtime escaping, fetch controls.');
})().catch(error => { console.error(error); process.exitCode = 1; });
