/* Offline manual annotation. No detector, peak search, or prediction input. */
"use strict";
(() => {
  const VERSION = "manual-review-1.0.0", SCHEMA = "ecg-reference-v2";
  const $ = id => document.getElementById(id), clone = x => JSON.parse(JSON.stringify(x));
  const catalog = window.REVIEW_CASES, cache = window.REVIEW_WAVEFORMS = {};
  const types = ["unknown", "normal_sinus_supported", "non_normal_or_unclassified"];
  const labels = {unknown:"未知", normal_sinus_supported:"正常窦性（人工）", non_normal_or_unclassified:"非正常/待细分"};
  const colors = {unknown:"#ab690c", normal_sinus_supported:"#087d70", non_normal_or_unclassified:"#b34135"};
  let current, wave, state, undo = [], redo = [], selected = null, sequence = 0, drag = null;
  let view = {start:0, span:2500, gain:1}, mode = "beat", pointer = null, framePending = false, storageFailed = false;
  const canvas = $("wave"), ctx = canvas.getContext("2d"), PAD = 88, RIGHT = 20, TOP = 28, BOTTOM = 30;
  let width = 1000, height = 788, scales = [];
  const now = () => new Date().toISOString();
  const uid = () => crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
  const msg = (text, error=false) => { $("message").textContent = text; $("message").className = error ? "error" : ""; };
  function fresh() {
    const time = now();
    return {case_id:current.case_id, waveform_sha256:current.waveform_sha256, selection_manifest_sha256:current.selection_manifest_sha256,
      schema_version:SCHEMA, tool_version:VERSION, revision:0, created_at:time, updated_at:time,
      reviewer_id:"", prior_algorithm_exposure:null, rhythm_context:"", events:[], intervals:[], completed:false, audit_log:[]};
  }
  const key = () => "ecg-manual-v2:" + current.case_id + ":" + current.waveform_sha256;
  function persist() {
    try { localStorage.setItem(key(), JSON.stringify(state)); storageFailed=false;
      $("saveStatus").textContent = `${current.case_id} · 修订 ${state.revision} · 浏览器草稿已缓存`; }
    catch (_) { storageFailed=true; $("saveStatus").textContent = "草稿缓存失败，请立即保存 JSON 文件"; }
  }
  function commit(action, edit) {
    if (!wave || !state) return;
    undo.push(clone(state)); if (undo.length > 100) undo.shift(); redo=[];
    edit(); state.revision++; state.updated_at=now();
    state.audit_log.push({action, at:state.updated_at, revision:state.revision, case_id:current.case_id});
    persist(); refresh();
  }
  function travel(from, to, action) {
    if (!from.length) return;
    const previous=clone(state), revision=state.revision+1, log=state.audit_log;
    to.push(previous); state=from.pop(); state.revision=revision; state.updated_at=now();
    state.audit_log=log.concat([{action, at:state.updated_at, revision, case_id:current.case_id}]);
    selected=null; persist(); refresh(); msg(action==="undo" ? "已撤销；原操作仍保留在修改记录中。" : "已重做。");
  }
  const inEvaluation = sample => Number.isInteger(sample) && sample >= current.evaluation_start_sample && sample < current.evaluation_end_sample;
  function validIntervals(intervals) {
    const sorted=[...intervals].sort((a,b)=>a.start_sample-b.start_sample);
    for (let i=0;i<sorted.length;i++) {
      const x=sorted[i];
      if (!Number.isInteger(x.start_sample) || !Number.isInteger(x.end_sample) || x.start_sample<current.evaluation_start_sample ||
          x.end_sample>current.evaluation_end_sample || x.start_sample>=x.end_sample || !["readable","unreadable"].includes(x.kind)) return false;
      if (i && sorted[i-1].end_sample>x.start_sample) return false;
    }
    return true;
  }
  function fullyCovered() {
    let end=current.evaluation_start_sample;
    for (const r of [...state.intervals].sort((a,b)=>a.start_sample-b.start_sample)) { if (r.start_sample!==end) return false; end=r.end_sample; }
    return end===current.evaluation_end_sample;
  }
  function completionReady() { return state.reviewer_id.trim() && typeof state.prior_algorithm_exposure==="boolean" && fullyCovered(); }
  function validate(s, external=false) {
    if (!s || s.schema_version!==SCHEMA || s.case_id!==current.case_id || s.waveform_sha256!==current.waveform_sha256 || s.selection_manifest_sha256!==current.selection_manifest_sha256) throw Error("文件的 case、波形或选样版本不匹配。请先选择对应 case，不能跨记录导入。");
    for (const field of ["fs","evaluation_start_sample","evaluation_end_sample","waveform_file"]) {
      if ((external || Object.prototype.hasOwnProperty.call(s,field)) && s[field]!==current[field]) throw Error("采样率或评价区间等来源字段不匹配，未修改当前标注。");
    }
    if (!Array.isArray(s.events) || !Array.isArray(s.intervals) || !Array.isArray(s.audit_log) || !Number.isInteger(s.revision) || s.revision<0) throw Error("标注结构或修订号无效。");
    if (typeof s.reviewer_id!=="string" || typeof s.rhythm_context!=="string" || typeof s.completed!=="boolean" || ![null,true,false].includes(s.prior_algorithm_exposure)) throw Error("标注者或参考状态字段无效。");
    if (!Number.isFinite(Date.parse(s.created_at)) || !Number.isFinite(Date.parse(s.updated_at))) throw Error("标注时间缺失或无效。");
    const ids=new Set(), samples=new Set();
    for (const e of s.events) {
      if (!inEvaluation(e.sample_index) || samples.has(e.sample_index) || !types.includes(e.beat_type) || !["certain","uncertain"].includes(e.beat_presence) ||
          !Number.isFinite(Date.parse(e.annotated_at)) || !Number.isInteger(e.channel) || e.channel<1 || e.channel>8 || e.position_source!=="manual_click_no_peak_snap") throw Error("存在重复、越界或非人工点击来源的心搏位置。");
      samples.add(e.sample_index);
    }
    for (const x of [...s.events,...s.intervals]) { if (typeof x.id!=="string" || ids.has(x.id) || !Number.isFinite(Date.parse(x.annotated_at))) throw Error("标注编号缺失、重复或标注时间无效。"); ids.add(x.id); }
    if (!validIntervals(s.intervals)) throw Error("区间越界、重叠或类型无效。");
    if (s.completed) {
      let end=current.evaluation_start_sample;
      for (const r of [...s.intervals].sort((a,b)=>a.start_sample-b.start_sample)) { if(r.start_sample!==end) throw Error("完成状态与区间覆盖不一致。"); end=r.end_sample; }
      if(end!==current.evaluation_end_sample || !s.reviewer_id.trim() || typeof s.prior_algorithm_exposure!=="boolean") throw Error("完成状态缺少必要信息。");
    }
    return s;
  }
  function exportDocument() {
    const s=clone(state), time=now();
    return {...current, ...s, fs:current.fs, evaluation_start_sample:current.evaluation_start_sample, evaluation_end_sample:current.evaluation_end_sample,
      waveform_file:current.waveform_file, tool_version:VERSION, reference_version:`manual-v2-r${state.revision}`, exported_at:time, reviewed_at:state.updated_at,
      annotation_timezone:Intl.DateTimeFormat().resolvedOptions().timeZone, exported_utc_offset_minutes:-new Date().getTimezoneOffset(),
      evaluation_interval_convention:"[start_sample,end_sample)", sample_rounding:"nearest_integer_sample_not_peak_snap",
      reference_independence_status:"not_adjudicated", adjudication_status:state.completed ? "completed_unadjudicated" : "in_progress",
      readable_intervals:state.intervals.filter(r=>r.kind==="readable").map(r=>[r.start_sample,r.end_sample]),
      unreadable_intervals:state.intervals.filter(r=>r.kind==="unreadable").map(r=>[r.start_sample,r.end_sample]),
      unreadable_reasons:state.intervals.filter(r=>r.kind==="unreadable").map(r=>({id:r.id,start_sample:r.start_sample,end_sample:r.end_sample,reason:r.reason})),
      normal_beat_validation:"human_annotations_unadjudicated", nn_rules_frozen:false};
  }
  function save() {
    if (!state || !wave) return; document.activeElement?.blur(); persist();
    const doc=exportDocument(), name=`${current.case_id}_r${state.revision}_${doc.exported_at.replace(/[:.]/g,"-")}.json`;
    const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:"application/json"}));
    const a=document.createElement("a"); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),30000);
    msg("已发起 JSON 下载，请确认下载文件已落盘。每次保存使用新的文件名。");
  }
  function settings() { return {beat_presence:$("presence").value,beat_type:$("beatType").value,confidence:$("confidence").value}; }
  function select(kind,id) {
    selected={kind,id}; const obj=(kind==="event"?state.events:state.intervals).find(x=>x.id===id);
    if (kind==="event") { $("presence").value=obj.beat_presence; $("beatType").value=obj.beat_type; $("confidence").value=obj.confidence || "unspecified"; }
    else { $("intervalType").value=obj.kind; $("reason").value=obj.reason || "unspecified"; }
    refresh();
  }
  function refresh() {
    if (!state) return;
    $("reviewer").value=state.reviewer_id; $("exposure").value=state.prior_algorithm_exposure===null ? "" : String(state.prior_algorithm_exposure);
    $("context").value=state.rhythm_context; $("completed").checked=state.completed;
    $("undo").disabled=!undo.length; $("redo").disabled=!redo.length;
    $("delete").disabled=$("apply").disabled=!selected;
    $("counts").textContent=`${state.events.length} 个心搏 · ${state.intervals.length} 个区间 · 修订 ${state.revision}`;
    const obj=selected && (selected.kind==="event"?state.events:state.intervals).find(x=>x.id===selected.id);
    $("selection").textContent=obj ? (selected.kind==="event" ? `采样索引 ${obj.sample_index} / ${(obj.sample_index/current.fs).toFixed(3)} 秒；${labels[obj.beat_type]}` : `区间 [${obj.start_sample}, ${obj.end_sample})；${obj.kind==="readable"?"可判读":"不可判读"}`) : "尚未选择标注";
    const list=$("annotationList"); list.replaceChildren();
    for (const [kind,arr] of [["event",state.events],["interval",state.intervals]]) for (const item of [...arr].sort((a,b)=>(a.sample_index??a.start_sample)-(b.sample_index??b.start_sample))) {
      const li=document.createElement("li"), button=document.createElement("button");
      button.textContent=kind==="event" ? `${item.sample_index} · ${labels[item.beat_type]}` : `[${item.start_sample}, ${item.end_sample}) · ${item.kind==="readable"?"可判读":"不可判读"}`;
      button.className=selected?.id===item.id ? "selected" : "";
      button.onclick=()=>{select(kind,item.id); const pos=item.sample_index??item.start_sample; if(pos<view.start || pos>=view.start+view.span) setView(pos-view.span/2,view.span);};
      li.append(button); list.append(li);
    }
    schedule();
  }
  function setView(start,span) {
    if (!wave) return;
    view.span=Math.max(50,Math.min(wave.frames,Math.round(span)));
    view.start=Math.max(wave.start_sample,Math.min(wave.start_sample+wave.frames-view.span,Math.round(start)));
    $("position").min=wave.start_sample; $("position").max=wave.start_sample+wave.frames-view.span; $("position").value=view.start;
    $("viewLabel").textContent=`${(view.start/current.fs).toFixed(3)} – ${((view.start+view.span)/current.fs).toFixed(3)} 秒`;
    schedule();
  }
  const sampleAt = x => Math.round(view.start+(x-PAD)/(width-PAD-RIGHT)*view.span);
  const xAt = s => PAD+(s-view.start)/view.span*(width-PAD-RIGHT);
  function zoom(factor,anchor=.5) { const center=view.start+view.span*anchor; setView(center-view.span*factor*anchor,view.span*factor); }
  function schedule() { if (!framePending) {framePending=true; requestAnimationFrame(()=>{framePending=false; draw();});} }
  function draw() {
    if (!wave || !state) return;
    const dpr=window.devicePixelRatio||1; width=canvas.clientWidth; height=canvas.clientHeight;
    if(canvas.width!==Math.round(width*dpr) || canvas.height!==Math.round(height*dpr)) {canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);}
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.fillStyle="#fff"; ctx.fillRect(0,0,width,height);
    const plotW=width-PAD-RIGHT,rowH=(height-TOP-BOTTOM)/8;
    const rangeFill=(a,b,color)=>{const l=Math.max(PAD,xAt(a)),r=Math.min(width-RIGHT,xAt(b));if(r>l){ctx.fillStyle=color;ctx.fillRect(l,TOP,r-l,height-TOP-BOTTOM);}};
    rangeFill(wave.start_sample,current.evaluation_start_sample,"#e7f1fb"); rangeFill(current.evaluation_end_sample,wave.start_sample+wave.frames,"#e7f1fb");
    for(const r of state.intervals) rangeFill(r.start_sample,r.end_sample,r.kind==="readable"?"#e3f3ec":"#fce9e6");
    if(drag?.kind==="interval") rangeFill(Math.min(drag.sample,drag.last),Math.max(drag.sample,drag.last),"#fceec6");
    ctx.font="11px sans-serif"; ctx.lineWidth=1;
    for(let tick=0;tick<=8;tick++) {const s=view.start+view.span*tick/8,x=xAt(s);ctx.strokeStyle="#e1e8ec";ctx.beginPath();ctx.moveTo(x,TOP);ctx.lineTo(x,height-BOTTOM);ctx.stroke();ctx.fillStyle="#526775";ctx.textAlign="center";ctx.fillText((s/current.fs).toFixed(view.span<current.fs*2?3:2),x,height-10);}
    const begin=Math.max(0,view.start-wave.start_sample),end=Math.min(wave.frames,view.start+view.span-wave.start_sample);
    for(let ch=0;ch<8;ch++) {
      let low=Infinity,high=-Infinity,sum=0;
      for(let i=begin;i<end;i++){const value=wave.samples[i*8+ch];low=Math.min(low,value);high=Math.max(high,value);sum+=value;}
      const center=sum/Math.max(1,end-begin);
      scales[ch]={center,range:Math.max(1,high-center,center-low)*1.05};
      const top=TOP+rowH*ch,mid=top+rowH/2, amplitude=scales[ch].range/view.gain;
      const yAt=v=>mid-(v-scales[ch].center)/amplitude*(rowH-16)/2;
      ctx.textAlign="left";ctx.fillStyle="#183d4c";ctx.fillText(`Ch${ch+1}`,7,mid-10);ctx.fillStyle="#6c7d86";ctx.fillText((scales[ch].center+amplitude).toFixed(0),7,top+14);ctx.fillText((scales[ch].center-amplitude).toFixed(0),7,top+rowH-7);
      if(high>center+amplitude || low<center-amplitude){ctx.fillStyle="#b34135";ctx.fillText("裁切",7,mid+8);}
      ctx.save();ctx.beginPath();ctx.rect(PAD,top,plotW,rowH);ctx.clip();ctx.strokeStyle="#264f66";ctx.lineWidth=.85;ctx.beginPath();
      if(view.span<=plotW*4) {for(let i=begin;i<end;i++){const x=xAt(i+wave.start_sample),y=yAt(wave.samples[i*8+ch]);if(i===begin)ctx.moveTo(x,y);else ctx.lineTo(x,y);}}
      else {for(let px=0;px<plotW;px++){const a=Math.floor(begin+px/plotW*(end-begin)),b=Math.min(end,Math.ceil(begin+(px+1)/plotW*(end-begin)));let lo=Infinity,hi=-Infinity;for(let i=a;i<b;i++){const v=wave.samples[i*8+ch];lo=Math.min(lo,v);hi=Math.max(hi,v);}ctx.moveTo(PAD+px,yAt(lo));ctx.lineTo(PAD+px,yAt(hi));}}
      ctx.stroke();ctx.restore();ctx.strokeStyle="#dce5ea";ctx.beginPath();ctx.moveTo(PAD,top+rowH);ctx.lineTo(width-RIGHT,top+rowH);ctx.stroke();
    }
    for(const s of [current.evaluation_start_sample,current.evaluation_end_sample]) if(s>=view.start&&s<=view.start+view.span){ctx.strokeStyle="#5685ac";ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(xAt(s),TOP);ctx.lineTo(xAt(s),height-BOTTOM);ctx.stroke();ctx.setLineDash([]);}
    for(const e of state.events) if(e.sample_index>=view.start&&e.sample_index<=view.start+view.span){const x=xAt(drag?.id===e.id?drag.last:e.sample_index);ctx.strokeStyle=colors[e.beat_type];ctx.lineWidth=selected?.id===e.id?2.4:1;ctx.beginPath();ctx.moveTo(x,TOP);ctx.lineTo(x,height-BOTTOM);ctx.stroke();ctx.fillStyle=colors[e.beat_type];ctx.textAlign="center";ctx.fillText("●",x,18);}
    if(selected?.kind==="interval"){const r=state.intervals.find(r=>r.id===selected.id);if(r){ctx.strokeStyle="#087d70";ctx.lineWidth=2;ctx.strokeRect(xAt(r.start_sample),TOP,xAt(r.end_sample)-xAt(r.start_sample),height-TOP-BOTTOM);}}
    if(pointer && pointer.x>=PAD && pointer.x<=width-RIGHT){ctx.strokeStyle="#7e8c92";ctx.lineWidth=.7;ctx.setLineDash([2,4]);ctx.beginPath();ctx.moveTo(pointer.x,TOP);ctx.lineTo(pointer.x,height-BOTTOM);ctx.stroke();ctx.setLineDash([]);}
    ctx.textAlign="left";ctx.fillStyle="#526775";ctx.fillText("原始计数 · 秒（记录起点） · 人工标记",PAD,16);
  }
  const coords = e => {const r=canvas.getBoundingClientRect();return {x:e.clientX-r.left,y:e.clientY-r.top};};
  canvas.onpointerdown=e=>{
    if(!state||!wave)return; const p=coords(e);if(p.x<PAD||p.x>width-RIGHT||p.y<TOP||p.y>height-BOTTOM)return;
    const sample=sampleAt(p.x);canvas.setPointerCapture(e.pointerId);
    const kind=e.shiftKey||mode==="pan"?"pan":mode;
    drag={kind,sample,last:sample,x:p.x,y:p.y,origin:view.start,channel:Math.min(8,Math.floor((p.y-TOP)/((height-TOP-BOTTOM)/8))+1)};
    if(kind==="select") {
      const hit=state.events.filter(x=>Math.abs(xAt(x.sample_index)-p.x)<=8).sort((a,b)=>Math.abs(xAt(a.sample_index)-p.x)-Math.abs(xAt(b.sample_index)-p.x))[0];
      if(hit){select("event",hit.id);drag.id=hit.id;drag.kind="move";}
      else {const interval=state.intervals.find(r=>sample>=r.start_sample&&sample<r.end_sample);if(interval)select("interval",interval.id);else{selected=null;refresh();}}
    }
  };
  canvas.onpointermove=e=>{
    const p=coords(e);pointer=p;if(!wave)return;
    const sample=sampleAt(p.x),ch=Math.max(0,Math.min(7,Math.floor((p.y-TOP)/((height-TOP-BOTTOM)/8))));
    const raw=sample>=wave.start_sample&&sample<wave.start_sample+wave.frames?wave.samples[(sample-wave.start_sample)*8+ch]:"—";
    $("cursor").textContent=`全局采样 ${sample} · ${(sample/current.fs).toFixed(3)} 秒 · Ch${ch+1} 原始值 ${raw}`;
    if(drag){drag.last=sample;if(drag.kind==="pan")setView(drag.origin-(p.x-drag.x)/(width-PAD-RIGHT)*view.span,view.span);}
    schedule();
  };
  canvas.onpointerup=e=>{
    if(!drag)return;const d=drag,p=coords(e),sample=sampleAt(p.x);drag=null;
    if(d.kind==="beat"&&Math.hypot(p.x-d.x,p.y-d.y)<5){
      if(!inEvaluation(sample)){msg("此处是缓冲区或评价边界外，不添加心搏。",true);schedule();return;}
      if(state.events.some(x=>x.sample_index===sample)){msg("这个采样点已有人工标记，没有添加重复事件。",true);return;}
      const id=uid();commit("add_beat",()=>{state.completed=false;state.events.push({id,sample_index:sample,...settings(),channel:d.channel,annotated_at:now(),position_source:"manual_click_no_peak_snap"});state.events.sort((a,b)=>a.sample_index-b.sample_index);selected={kind:"event",id};});
      msg(`已标记采样点 ${sample}，位置来自本次点击。`);
    } else if(d.kind==="interval") {
      const a=Math.min(d.sample,sample),b=Math.max(d.sample,sample);
      const interval={id:uid(),start_sample:a,end_sample:b,kind:$("intervalType").value,reason:$("intervalType").value==="unreadable"?$("reason").value:null,annotated_at:now()};
      if(!validIntervals([...state.intervals,interval])){msg("区间须位于评价区内、长度至少1个采样点，且不能与已有区间重叠。请缩放后重选。",true);schedule();return;}
      commit("add_interval",()=>{state.completed=false;state.intervals.push(interval);selected={kind:"interval",id:interval.id};});msg(`已添加区间 [${a}, ${b})。`);
    } else if(d.kind==="move"&&Math.abs(p.x-d.x)>=3) {
      if(!inEvaluation(sample)||state.events.some(x=>x.id!==d.id&&x.sample_index===sample)){msg("移动位置越界或已有事件，原标记未改变。",true);schedule();return;}
      commit("move_beat",()=>{const item=state.events.find(x=>x.id===d.id);item.sample_index=sample;item.modified_at=now();item.channel=d.channel;state.events.sort((a,b)=>a.sample_index-b.sample_index);state.completed=false;});msg(`人工标记已移动到 ${sample}，未吸附波形峰。`);
    }
    schedule();
  };
  canvas.onpointercancel=()=>{drag=null;schedule();};canvas.onpointerleave=()=>{if(!drag)pointer=null;schedule();};
  canvas.addEventListener("wheel",e=>{if(!wave)return;e.preventDefault();const p=coords(e);zoom(e.deltaY>0?1.25:.8,Math.max(0,Math.min(1,(p.x-PAD)/(width-PAD-RIGHT))));},{passive:false});
  document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{mode=b.dataset.mode;document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x===b));canvas.style.cursor=mode==="pan"?"grab":"crosshair";});
  document.querySelectorAll("[data-span]").forEach(b=>b.onclick=()=>{if(!wave)return;const n=b.dataset.span;if(n==="all")setView(wave.start_sample,wave.frames);else if(n==="60")setView(current.evaluation_start_sample,current.evaluation_end_sample-current.evaluation_start_sample);else setView(view.start+view.span/2-Number(n)*current.fs/2,Number(n)*current.fs);});
  $("zoomIn").onclick=()=>zoom(.5);$("zoomOut").onclick=()=>zoom(2);
  $("gain").onchange=()=>{view.gain=Number($("gain").value);schedule();};$("position").oninput=()=>setView(Number($("position").value),view.span);
  $("back").onclick=()=>setView(view.start-view.span*.8,view.span);$("forward").onclick=()=>setView(view.start+view.span*.8,view.span);
  $("undo").onclick=()=>travel(undo,redo,"undo");$("redo").onclick=()=>travel(redo,undo,"redo");
  $("delete").onclick=()=>{if(!selected)return;commit("delete_annotation",()=>{const field=selected.kind==="event"?"events":"intervals";state[field]=state[field].filter(x=>x.id!==selected.id);selected=null;state.completed=false;});};
  $("apply").onclick=()=>{if(!selected)return;commit("change_annotation_type",()=>{if(selected.kind==="event")Object.assign(state.events.find(x=>x.id===selected.id),settings(),{modified_at:now()});else Object.assign(state.intervals.find(x=>x.id===selected.id),{kind:$("intervalType").value,reason:$("intervalType").value==="unreadable"?$("reason").value:null,modified_at:now()});state.completed=false;});};
  for(const [id,field] of [["reviewer","reviewer_id"],["exposure","prior_algorithm_exposure"],["context","rhythm_context"]]) $(id).onchange=()=>commit("update_"+field,()=>{state[field]=id==="exposure"?($(id).value===""?null:$(id).value==="true"):$(id).value;state.completed=false;});
  $("completed").onchange=()=>{const wanted=$("completed").checked;if(wanted&&!completionReady()){ $("completed").checked=false;msg("尚不能标完成：需填写标注者、暴露情况，并用区间覆盖整个评价区。未知类型可以保留。",true);return;}commit("set_completion",()=>{state.completed=wanted;});};
  $("save").onclick=save;$("importButton").onclick=()=>$("importFile").click();
  $("importFile").onchange=async()=>{const file=$("importFile").files[0];if(!file)return;const caseId=current.case_id;try{if(file.size>20*1024*1024)throw Error("文件超过20MB，请核实是否为标注JSON。");const imported=JSON.parse(await file.text());if(current.case_id!==caseId)throw Error("读取时case发生变化，请重新导入。");validate(imported,true);commit("import_saved_annotation",()=>{const revision=state.revision,log=state.audit_log;state={...fresh(),...clone(imported)};state.revision=Math.max(revision,imported.revision);state.audit_log=log.concat(imported.audit_log);selected=null;});msg("标注已导入，可撤销本次导入。来源与采样坐标校验通过。");}catch(e){msg("未导入："+e.message,true);}finally{$("importFile").value="";}};
  async function openCase(id) {
    const ticket=++sequence;$("loading").textContent="正在加载原始采样…";$("loading").hidden=false;$("save").disabled=true;$("importButton").disabled=true;selected=null;drag=null;wave=null;
    current=clone(catalog.find(c=>c.case_id===id));$("caseSelect").value=id;$("staticLink").href=id+"/index.html";
    try {
      if(!cache[id])await new Promise((resolve,reject)=>{const script=document.createElement("script");script.src="waveforms/"+id+".js";script.onload=resolve;script.onerror=()=>reject(Error("无法加载原始采样。请保留整个离线目录结构。"));document.head.append(script);});
      if(ticket!==sequence)return;
      const data=cache[id],bytes=Uint8Array.from(atob(data.base64),c=>c.charCodeAt(0));
      if(data.case_id!==id || data.waveform_sha256!==current.waveform_sha256 || bytes.length!==data.frames*16 || data.fs!==current.fs)throw Error("采样数据与case来源不匹配。");
      const values=new Int16Array(data.frames*8),dv=new DataView(bytes.buffer);for(let i=0;i<values.length;i++)values[i]=dv.getInt16(i*2,true);
      wave={...data,samples:values};scales=[];
      state=fresh();undo=[];redo=[];let restored=false;
      let stored=null;try{stored=localStorage.getItem(key());}catch(_){storageFailed=true;}
      try{if(stored){state=validate(JSON.parse(stored));restored=true;}}catch(e){msg("未载入缓存草稿："+e.message+"；原缓存未删除。可在其他浏览器中打开工具并导入已保存的JSON。",true);$("loading").textContent="缓存校验失败，本 case 编辑已停用，原缓存未覆盖。";$("save").disabled=true;wave=null;return;}
      $("beatType").value="unknown";$("presence").value="certain";$("confidence").value="unspecified";
      view.gain=1;$("gain").value="1";setView(current.evaluation_start_sample,10*current.fs);refresh();
      $("loading").hidden=true;$("save").disabled=false;$("importButton").disabled=false;$("saveStatus").textContent=storageFailed?"浏览器缓存不可用，请保存 JSON 文件":restored?`${id} · 已恢复修订 ${state.revision} 的浏览器草稿`:`${id} · 尚未标注`;
      msg(restored?"已恢复草稿。撤销/重做仅针对本次打开后的操作；请继续保存JSON备份。":"可开始标注。类型默认未知；先点击，再按需要选择类型并应用。");
    }catch(e){if(ticket!==sequence)return;$("loading").textContent=e.message;msg(e.message,true);}
  }
  for(const c of catalog){const o=document.createElement("option");o.value=c.case_id;o.textContent=c.case_id;$("caseSelect").append(o);}
  $("caseSelect").onchange=()=>openCase($("caseSelect").value);
  for(const [id,step] of [["previousCase",-1],["nextCase",1]])$(id).onclick=()=>{const i=catalog.findIndex(c=>c.case_id===current.case_id);if(catalog[i+step])openCase(catalog[i+step].case_id);};
  window.addEventListener("resize",schedule);
  window.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){e.preventDefault();save();return;}if(["INPUT","TEXTAREA","SELECT"].includes(e.target.tagName))return;if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="z"){e.preventDefault();travel(e.shiftKey?redo:undo,e.shiftKey?undo:redo,e.shiftKey?"redo":"undo");}if(e.key==="Delete")$("delete").click();});
  window.addEventListener("beforeunload",e=>{if(storageFailed){e.preventDefault();e.returnValue="";}});
  const requested=new URLSearchParams(location.search).get("case");openCase(catalog.some(c=>c.case_id===requested)?requested:catalog[0].case_id);
})();
