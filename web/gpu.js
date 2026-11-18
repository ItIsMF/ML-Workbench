'use strict';
(() => {
 const panel=$('gpu-panel'), pick=$('gpu-record'), result=$('gpu-result');
 let baseline='';
 let mode='', loaded='', version=0, timer, home='', reading=0, actionPending=false, awaitingKind='';
 function command() {
  const set=$('gpu-set').value;
  $('gpu-command').textContent=`.\\.venv\\Scripts\\python.exe gpu_watch.py counters --seconds 10${set ? ' --metric-set '+set : ''} --out-dir '${home.replaceAll("'","''")}'`;
 }
 const fmt=v=>Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
 const table=(heads,rows)=>`<div class="gpu-table"><table><thead><tr>${heads.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
 function chart(rows,cols) {
  if(rows.length<2)return '';
  const series=(col,cls)=>{let path='',pen=false;rows.forEach((row,i)=>{const v=parseFloat(row[col]);if(!Number.isFinite(v)){pen=false;return;}const x=4+392*i/(rows.length-1), y=74-70*Math.min(100,Math.max(0,v))/100;path+=(pen?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)+' ';pen=true;});return `<path class="${cls}" d="${path}"/>`;};
  return `<figure class="gpu-trace"><figcaption>忙时占比 0–100% · 由旧到新 <span>GPU</span> <b>显存读写</b></figcaption><svg viewBox="0 0 400 80" preserveAspectRatio="none" role="img" aria-label="GPU 与显存读写忙时趋势"><path class="gpu-grid" d="M4 4H396 M4 39H396 M4 74H396"/>${series(cols[1],'gpu-busy')}${series(cols[2],'gpu-mem')}</svg></figure>`;
 }
 function show(d) {
  if(d.kind==='counters') {
   const many=new Set(d.metrics.map(m=>m.stream)).size>1;
   result.innerHTML=`<p>${esc(d.series)} 项整卡指标 · 下表为样本统计；单位见指标名。</p>`+d.notes.map(n=>`<p>${esc(n)}</p>`).join('')+table(['指标','均值','最低','最高','样本数'],d.metrics.map(m=>[(many ? m.stream+' · ' : '')+m.name,fmt(m.mean),fmt(m.low),fmt(m.high),m.samples]));
   return;
  }
  const fields=['timestamp','utilization.gpu','utilization.memory','memory.used','memory.total','power.draw','temperature.gpu','clocks.current.sm','clocks.current.memory'];
  const labels=['时间','GPU 忙时','显存忙时','已用显存','总显存','功耗','温度 °C','SM 时钟','显存时钟'];
  const cols=fields.map(k=>d.columns.findIndex(v=>v.split(' [')[0]===k));
  result.innerHTML=d.rows.length ? `<p>最近 ${d.rows.length} 个样本 · 最新在前；显存忙时不是带宽。</p>`+chart(d.rows,cols)+table(labels,[...d.rows].reverse().map(r=>cols.map(i=>r[i]??'—'))) : '<p>等待第一条完整样本。</p>';
 }
 async function record(force=false) {
  const id=pick.value, seq=++reading;
  if(!id){loaded='';result.textContent='还没有 GPU 记录。';return;}
  if(!force && id===loaded)return;
  if(id!==loaded) result.textContent='正在读取采集记录…';
  try {const d=await api('/api/gpu/record?id='+encodeURIComponent(id));if(seq===reading && id===pick.value){show(d);loaded=id;}}
  catch(e){if(seq===reading){loaded='';result.textContent='记录暂不可读：'+e.message;}}
 }
 function buttons(){ $('gpu-trend').disabled=$('gpu-counters').disabled=actionPending || !!mode; $('gpu-stop').hidden=mode!=='trend'; $('gpu-stop').disabled=actionPending; }
 async function refreshPanel(force=false) {
  const seq=++version;clearTimeout(timer);
  if(!panel.open || document.hidden)return;
  try {
   const d=await api('/api/gpu'); if(seq!==version)return;
   const was=mode;mode=d.mode; home=d.manual_dir;command();buttons();
   $('gpu-status').textContent=d.message || '当前未采集。';
   const prev=pick.value, ids=d.records.map(r=>r.id);
   if(JSON.stringify(ids)!==JSON.stringify([...pick.options].map(o=>o.value))){pick.replaceChildren(...d.records.map(r=>new Option((r.id.startsWith('panel/')?'面板 · ':'本机 · ')+r.name,r.id)));if(ids.includes(prev))pick.value=prev;}
   if(awaitingKind && ids[0]?.startsWith('panel/'+awaitingKind+'-') && ids[0]!==baseline){pick.value=ids[0];awaitingKind='';}
   if(!mode)awaitingKind='';
   await record(force || (mode==='trend' && pick.value===ids[0] && pick.value.startsWith('panel/trend-')) || (was && !mode && pick.value.startsWith('panel/')));
  } catch(e){if(seq===version)$('gpu-status').textContent='监测面板：'+e.message;}
  finally{if(seq===version && panel.open && !document.hidden && mode)timer=setTimeout(()=>refreshPanel(),mode==='trend'?5000:2200);}
 }
 async function action(path,body){actionPending=true;buttons();try{if(path==='start'){baseline=pick.options[0]?.value || '';awaitingKind=body.mode;}await api('/api/gpu/'+path,body);await refreshPanel(true);} catch(e){$('gpu-status').textContent=e.message;}finally{actionPending=false;buttons();}}
 $('gpu-trend').onclick=()=>action('start',{mode:'trend'});
 $('gpu-counters').onclick=()=>action('start',{mode:'counters',metric_set:$('gpu-set').value});
 $('gpu-stop').onclick=()=>action('stop',{});$('gpu-set').onchange=command;
 $('gpu-reload').onclick=()=>refreshPanel(true);pick.onchange=()=>{awaitingKind='';record(true);};
 panel.addEventListener('toggle',()=>refreshPanel());document.addEventListener('visibilitychange',()=>refreshPanel());
})();
