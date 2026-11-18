// Run the real page state/reading functions against a small DOM fixture, not a browser substitute.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const elements = new Map();
function element(id) {
 if (!elements.has(id)) elements.set(id, {
  innerHTML:'', textContent:'', dataset:{}, options:[], value:'', scrollHeight:0, scrollTop:0, clientHeight:0,
  querySelectorAll:()=>[], replaceChildren(...items){this.options=items; this.value=items[0]?.value || '';}
 });
 return elements.get(id);
}
const snapshots = new Map();
const requests = [];
let deferred;
const scope = vm.createContext({
 document:{getElementById:element,addEventListener(){}},window:{addEventListener(){}},
 Option:function(text,value){return {text,value};},setTimeout(){},setInterval(){},console,
 fetch:async url => {
  requests.push(url);
  if (deferred) return deferred;
  const id=url.match(/attempt\/([^/]+)/)[1],value=snapshots.get(id);
  return {ok:true,json:async()=>value};
 }
});
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
vm.runInContext(source.replace('refresh();setInterval(refresh,1200);',''),scope);
vm.runInContext('render = () => {};',scope);
const run=code=>vm.runInContext(code,scope);
function result(stage,value,revision=1) {
 return {revision,more:false,unchanged:false,view:{stages:{[stage]:{stage,done:value,total:100}},metrics:{loss:[{t:100,v:value}]},artifacts:{'image.png':{path:'image.png',label:stage}},last_event:100,errors:0,checkpoint:'saved.pt',log:stage+' log',worker_log:stage+' diagnostic'}};
}
(async()=>{
 snapshots.set('a',result('project A',10));snapshots.set('b',result('project B',20));
 run("selectAttempt('a')");await run('readOutput()');
 assert.equal(element('log').textContent,'project A log');
 run("selectAttempt('b')");await run('readOutput()');
 assert.equal(element('log').textContent,'project B log');
 run("selectAttempt('a')");
 assert.equal(element('log').textContent,'project A log','switch restores immediately before HTTP');
 snapshots.set('a',{revision:1,more:false,unchanged:true});await run('readOutput()');
 assert.ok(requests.at(-1).endsWith('since=1'));
 assert.equal(element('log').textContent,'project A log');
 snapshots.set('a',result('project A',11,2));await run('readOutput()');
 assert.equal(run("metrics.get('loss').length"),1,'snapshot replaces instead of appending duplicates');
 assert.equal(run("metrics.get('loss')[0].v"),11);
 run("selectAttempt('')");run("selectAttempt('a')");
 assert.match(element('artifacts').innerHTML,/image.png/,'save/reset selection preserves artifact display');
 let release;deferred=new Promise(resolve=>release=resolve);
 const pending=run('readOutput()');run("selectAttempt('b')");
 release({ok:true,json:async()=>result('project A',12,3)});await pending;deferred=null;
 assert.equal(element('log').textContent,'project B log','late response cannot overwrite selected project');
 run("selectAttempt('a')");assert.equal(run("metrics.get('loss')[0].v"),12);
 const quiet=result('quiet',21,4); quiet.view.log='';quiet.view.event_tail=[[100,'training · 20/128 步'],[110,'training · 21/128 步']];
 snapshots.set('a',quiet);await run('readOutput()');
 assert.match(element('log-size').textContent,/结构化事件/);assert.match(element('log').textContent,/20\/128 步\n.*21\/128 步/);
 snapshots.set('a',{...quiet,revision:5,view:{...quiet.view,log:'actual stderr\n'}});await run('readOutput()');
 assert.equal(element('log').textContent,'actual stderr\n');assert.match(element('log-size').textContent,/stdout/);
 run("selectAttempt('b')");assert.equal(element('log').textContent,'project B log');
 console.log('Page state checks passed: A/B/A cache, revision, no duplicate points, reset, late response isolation.');
})().catch(error=>{console.error(error);process.exitCode=1;});
