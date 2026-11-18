// Actual page functions with a small DOM fixture; real-browser checks are separate.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const elements = new Map();
const decode = s => s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function element(id) {
 if (!elements.has(id)) elements.set(id, {
  value:'',dataset:{},options:[],details:[],markup:'',
  set innerHTML(html) {
   this.markup=html;
   this.details=[...html.matchAll(/<details[^>]*data-group="([^"]*)"[^>]*>/g)].map(m=>({dataset:{group:decode(m[1])},open:m[0].includes(' open')}));
  },
  get innerHTML(){return this.markup;},querySelectorAll(){return this.details;},
  replaceChildren(...items){this.options=items;this.value=items[0]?.value || '';},
  showModal(){this.open=true;},close(){this.open=false;}
 });
 return elements.get(id);
}
let saved, prefs={groups:{},task_id:'',attempt_id:''}, serverState;
const requests=[],listeners=new Map();
let releaseWrite;
function page() {
 elements.clear();listeners.clear();
 const scope=vm.createContext({
  document:{getElementById:element,addEventListener(name,fn){listeners.set(name,fn);}},window:{addEventListener(){}},
  Option:function(text,value){return {text,value};},setTimeout(){},setInterval(){},
  fetch:async(url,request)=>{
   requests.push(url);
   if (url==='/api/view' && request?.method==='POST') {
    if (releaseWrite) await new Promise(resolve=>{releaseWrite=resolve;});
    prefs=JSON.parse(request.body);return {ok:true,json:async()=>({ok:true})};
   }
   if (url==='/api/view') return {ok:true,json:async()=>JSON.parse(JSON.stringify(prefs))};
   if (url==='/api/state') return {ok:true,json:async()=>serverState};
   if (url.includes('/monitor?')) return {ok:true,json:async()=>({revision:1,view:{}})};
   saved={url,value:JSON.parse(request.body)};return {ok:true,json:async()=>({id:'pre'})};
  }
 });
 const run=code=>vm.runInContext(code,scope);
 run(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8').replace('refresh();setInterval(refresh,1200);',''));
 return run;
}
let run=page();
run(`current.tasks=[
 {id:'formal',project:'Project A',name:'Formal A',group:'formal',kind:'experiment'},
 {id:'pre',project:'Project A',name:'Pre A',group:'preflight',kind:'experiment',argv:['python.exe'],resume_argv:[],cwd:'C:/QA'},
 {id:'legacy',project:'Project A',name:'Legacy A',kind:'training'},
 {id:'other',project:'Project <B>',name:'<script>',group:'preflight',kind:'inference'}
];taskId='pre';`);
const html=run('renderTaskGroups()');
assert.equal((html.match(/data-task=/g)||[]).length,4);
assert.match(html, /前检<span>1<\/span><\/summary><button[^>]*data-task="pre"[^>]*aria-current="true"/);
assert.match(html, /正式实验<span>1<\/span><\/summary><button[^>]*data-task="formal"/);
assert.match(html, /未分组<span>1<\/span><\/summary><button[^>]*data-task="legacy"/);
assert.ok(html.indexOf('data-task="pre"')<html.indexOf('data-task="formal"'));
assert.ok(html.includes('Project &lt;B&gt;') && html.includes('&lt;script&gt;') && !html.includes('<script>'));
run("putHtml('tasks',renderTaskGroups())");
assert.ok(element('tasks').details.every(d=>!d.open),'all new groups start collapsed');
element('tasks').details[0].open=true;
// A new project inserted ahead must not transfer A's collapse state to B.
run("current.tasks.unshift({id:'new',project:'New',name:'New task',group:'formal',kind:'experiment'});putHtml('tasks',renderTaskGroups())");
const states=new Map(element('tasks').details.map(d=>[d.dataset.group,d.open]));
assert.equal(states.get('["Project A","preflight"]'),true);
assert.equal(states.get('["New","formal"]'),false);
run("openForm(current.tasks.find(t=>t.id==='pre'))");
assert.equal(element('task-group').value,'preflight');
element('task-group').value='formal';
run('selectAttempt=()=>{};refresh=async()=>{};');
(async()=>{
 await element('task-form').onsubmit({preventDefault(){}});
 assert.equal(saved.url,'/api/tasks');assert.equal(saved.value.group,'formal');assert.equal(saved.value.id,'pre');
 assert.equal(element('task-dialog').open,false);
 run('openForm()');assert.equal(element('task-group').value,'');
 serverState={tasks:[{id:'a',project:'A',name:'A',group:'formal'},{id:'b',project:'B',name:'B',group:'preflight'},{id:'empty',project:'C',name:'C'}],attempts:[{id:'a-new',task_id:'a'},{id:'b-new',task_id:'b'},{id:'a-old',task_id:'a'}],token:'test'};
 run=page();run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()');await run('viewWrite');
 assert.ok(element('tasks').details.every(d=>!d.open));
 const group=element('tasks').details[0];Object.assign(group,{open:true,isConnected:true,matches:()=>true});
 listeners.get('toggle')({target:group});await run('viewWrite');
 assert.equal(prefs.groups['["A","formal"]'],true);
 run("selectAttempt('a-old')");await run('viewWrite');
 const before=requests.length;await run('refresh()');await run('viewWrite');
 assert.ok(!requests.slice(before).includes('/api/view'),'ordinary polling does not reread or rewrite preferences');
 run=page();run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()');await run('viewWrite');
 assert.equal(run('taskId'),'a');assert.equal(run('attemptId'),'a-old','reopen restores exact older attempt');
 assert.equal(element('tasks').details[0].open,true);
 assert.equal(element('tasks').details[1].open,false);
 // Programmatic restore and detached details must not persist spurious toggles.
 const count=requests.length;
 listeners.get('toggle')({target:{...element('tasks').details[0],isConnected:true,matches:()=>true}});
 listeners.get('toggle')({target:{...element('tasks').details[0],open:false,isConnected:false,matches:()=>true}});
 await run('viewWrite');assert.equal(requests.length,count);
 // Delay one save, then rapidly switch twice. The final saved selection must win.
 releaseWrite=true;
 run("taskId='b';selectAttempt('b-new')");await new Promise(setImmediate);
 run("taskId='a';selectAttempt('a-new')");
 const release=releaseWrite;releaseWrite=null;release();await run('viewWrite');
 assert.equal(prefs.task_id,'a');assert.equal(prefs.attempt_id,'a-new');
 prefs={...prefs,task_id:'b',attempt_id:'a-old'};
 run=page();run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()');await run('viewWrite');assert.equal(run('attemptId'),'b-new','never restore an attempt from a different task');
 run("taskId='empty';selectAttempt('')");await run('viewWrite');
 run=page();run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()');assert.equal(run('taskId'),'empty');assert.equal(run('attemptId'),'');
 prefs={...prefs,task_id:'removed',attempt_id:'gone'};
 run=page();run("render=()=>putHtml('tasks',renderTaskGroups())");
 await run('refresh()');await run('viewWrite');assert.equal(run('taskId'),'a');assert.equal(run('attemptId'),'a-new');
 assert.ok(!requests.some(url=>/\/api\/(start|stop|resume)/.test(url)),'restoring a view never executes tasks');
 console.log('Grouping UI passed: grouping/editing, collapsed defaults, reopen, exact attempt, invalid selection, empty task, ordered saves and read-only restore.');
})().catch(error=>{console.error(error);process.exitCode=1;});
