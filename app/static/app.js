const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const technicalLabels={parsed:"提取完成",partial:"部分完成",failed:"解析失败",legacy:"历史资料"};
const locationLabel=s=>state.fields?.[s]||({document:"整份文书"}[s])||s;
const LISTS={use:'uses',grade:'grades',mode:'modes',doc_type:'doc-types',has_map:'yes-no',altered_sealed:'yes-no'};
const labels = {queued:'等待处理',recognizing:'正在识别',needs_human:'待核对',held:'待补件',published:'已登记',withdrawn:'已撤回'};
const recognitionLabels={unconfigured:'未启用云识别',native_only:'原生结构提取',test_stub:'测试桩（非真实识别）','live-cloud':'云端识别','live-local':'本地识别',resumed_evidence:'复用已完成证据',existing_evidence:'已有证据'};
const pages={
  inbox:{title:'材料管理',description:'上传租赁材料，查看进度并继续处理。'},
  governance:{title:'待办事项',description:'先处理需要核对、补充材料或确认的问题。'},
  review:{title:'材料核对',description:'对照原件填写信息，核对租金后登记台账。'},
  ledger:{title:'宗地台账',description:'查看已确认的租金核算结果及历史记录。'},
  measure:{title:'系统管理',description:'查看识别状态，管理计租标准和数据备份。'}
};
const filters=[
  {key:'needs_human',label:'待核对',states:['needs_human'],icon:'pending',tip:'需要人工核对材料'},
  {key:'processing',label:'处理中',states:['queued','recognizing'],icon:'file',tip:'等待或正在提取信息'},
  {key:'held',label:'待补件',states:['held'],icon:'held',tip:'补充清晰、完整的材料'},
  {key:'published',label:'已登记',states:['published'],icon:'confirmed',tip:'已纳入有效宗地台账'}
];
const uiIcons={file:'<path d="M5 3h10l4 4v14H5zM14 3v5h5M8 12h8m-8 4h6"/>',pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',confirmed:'<path d="m4 12 5 5L20 6"/>',held:'<path d="m12 3 10 18H2L12 3Zm0 6v5m0 3h.01"/>'};
const uiIcon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true">${uiIcons[name]}</svg>`;
let state={}, doc=null, draft=null, started=0, dirty=false, citationDetails={}, selectedPayments=[], selectedReliefs=[], governanceRows=[];
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').style.display='block';toastTimer=setTimeout(()=>$('#toast').style.display='none',6500)}
async function api(path,body,method){
  const headers={'X-FDE-Role':$('#role').value,'X-FDE-Actor':encodeURIComponent($('#actor').value)};
  if(body && !(body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch(path,{method:method||(body?'POST':'GET'),headers,body:body?(body instanceof FormData?body:JSON.stringify(body)):undefined});
  if(response.status===401) throw Error('需要已配置的本地账号登录');
  let value;try{value=await response.json()}catch{throw Error(`服务返回 ${response.status}，请查看日志`)}
  if(!response.ok) throw Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail)); return value;
}
let activeTab='inbox', documentFilter='', operationBusy=false, invocationTrigger=null, refreshPending=false;
let listSignature='', initialLoad=true, toastTimer;
function feedback(target, message){
  const host=target?.closest('form,.field-panel,.card,.tab') || $('.tab:not(.hidden)');
  let box=host.querySelector(':scope > .action-feedback');
  if(!box){box=document.createElement('div');box.className='action-feedback error';box.setAttribute('role','alert');host.prepend(box)}
  box.textContent=message;box.hidden=!message;
}
async function action(fn, trigger=invocationTrigger){
  if(operationBusy)return false;
  operationBusy=true;
  const originalFocus=document.activeElement, originalTab=activeTab;
  SelectControls.close();
  const button=trigger?.matches('button')?trigger:trigger?.querySelector('button[type="submit"],button');
  const text=button?.innerHTML, disabled=button?.disabled;
  if(button){button.disabled=true;button.textContent='处理中…';button.setAttribute('aria-busy','true')}
  feedback(trigger,'');
  document.querySelectorAll('.tab').forEach(el=>el.inert=true);$('.identity').inert=true;
  try{await fn();return true}
  catch(error){feedback(trigger,error.message);toast(error.message);return false}
  finally{
    operationBusy=false;document.querySelectorAll('.tab').forEach(el=>el.inert=false);$('.identity').inert=false;
    if(button?.isConnected){button.innerHTML=text;button.disabled=disabled;button.removeAttribute('aria-busy')}
    applyPermissions();updateRequirements();
    if(activeTab!==originalTab)$('#title').focus({preventScroll:true});
    else if(originalFocus?.isConnected&&originalFocus.getClientRects().length&&!originalFocus.disabled)originalFocus.focus({preventScroll:true});
  }
}
async function saveDraft(){
  await api(`/api/documents/${doc.id}/draft`,body());dirty=false;doc.draft=body();
  if(state.documents){const item=state.documents.find(d=>d.id===doc.id);if(item)item.hasLocalDraft=true}
  toast('草稿已保存，尚未登记台账');
}
function guardUnsaved(){
  if(!dirty||!doc)return Promise.resolve('continue');
  const modal=$('#unsaved-dialog');$('#unsaved-error').hidden=true;
  return new Promise(resolve=>{
    const finish=value=>{modal.close();resolve(value)};
    modal.oncancel=e=>{e.preventDefault();finish('cancel')};
    $('#unsaved-cancel').onclick=()=>finish('cancel');
    $('#unsaved-discard').onclick=()=>finish('discard');
    $('#unsaved-save').onclick=async()=>{
      const buttons=[...modal.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
      modal.oncancel=e=>e.preventDefault();$('#unsaved-save').textContent='正在保存…';
      try{await saveDraft();finish('saved')}
      catch(error){$('#unsaved-error').textContent='保存失败：'+error.message;$('#unsaved-error').hidden=false}
      finally{buttons.forEach(b=>b.disabled=false);$('#unsaved-save').textContent='保存后继续';modal.oncancel=e=>{e.preventDefault();finish('cancel')}}
    };
    modal.showModal();$('#unsaved-cancel').focus();
  });
}
async function tab(name, skipGuard=false){
  if(!skipGuard&&name!==activeTab){
    const choice=await guardUnsaved();if(choice==='cancel')return false;
    if(choice==='discard')await openDoc(doc.id,true);
  }
  SelectControls.close();activeTab=name;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==name));
  document.querySelectorAll('nav button').forEach(x=>{x.classList.toggle('active',x.dataset.tab===name);if(x.dataset.tab===name)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current')});
  $('#title').textContent=pages[name].title;$('#breadcrumb-current').textContent=pages[name].title;
  $('#page-description').textContent=pages[name].description;$('#open-upload').hidden=name!=='inbox';
  window.scrollTo({top:0,behavior:'instant'});
  if(name==='ledger')await loadLedger();if(name==='governance'){await loadGovernance();await loadBatch();}
  return true;
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>action(()=>tab(b.dataset.tab),b));
function updateStatistics(){
  const docs=state.documents||[];
  if(!$('#stats').children.length)$('#stats').innerHTML=filters.map(f=>`<button class="stat stat-${f.icon}" data-filter="${f.key}" aria-pressed="false"><span class="stat-top"><span>${f.label}</span><span class="stat-icon">${uiIcon(f.icon)}</span></span><span class="stat-value"><b>0</b><span>份</span></span><span class="stat-foot">${f.tip}</span></button>`).join('');
  filters.forEach(f=>{const b=$(`[data-filter="${f.key}"]`);b.querySelector('b').textContent=docs.filter(d=>f.states.includes(d.state)).length;b.setAttribute('aria-pressed',String(documentFilter===f.key));b.onclick=()=>{documentFilter=f.key;updateStatistics();renderDocuments(true)}});
  $('#all-documents').setAttribute('aria-pressed',String(!documentFilter));
}
function renderDocuments(force=false){
  const all=state.documents||[], filter=filters.find(f=>f.key===documentFilter);
  const docs=all.filter(d=>!filter||filter.states.includes(d.state));
  const signature=JSON.stringify([documentFilter,docs]);
  // Never replace a focused row during polling; render it after focus leaves.
  if(!force&&($('#documents').contains(document.activeElement)||signature===listSignature))return;
  listSignature=signature;$('#document-count').textContent=`${docs.length} 份材料`;
  $('#filter-description').textContent=filter?`${filter.label} · 共 ${docs.length} 份材料`:`全部 ${all.length} 份材料（含已撤回）`;
  $('#documents').innerHTML=docs.length?`<table><thead><tr><th>档案名称</th><th>处理状态</th><th>材料处理情况</th><th>下一步</th></tr></thead><tbody>${docs.map(d=>`<tr><td><b>${esc(d.name)}</b><details><summary>档案编号</summary><small>${esc(d.id)}</small></details></td><td><span class="badge-state state-${esc(d.state)}">${esc(labels[d.state]||d.state)}</span></td><td><span class="${d.intake?.technical_status==='failed'?'failure-text':''}">${esc(technicalLabels[d.intake?.technical_status]||'待处理')}</span><br><small>${d.pages.length} 个原文位置${d.result?' · '+esc(recognitionLabels[d.result.mode]||d.result.mode):' · 可对照原件手动核对'}</small></td><td><button data-open="${esc(d.id)}" data-state="${esc(d.state)}">${d.state==='published'?'查看台账':d.state==='held'?'补充材料':d.state==='withdrawn'?'查看材料':d.state==='needs_human'?(d.review||d.hasLocalDraft?'继续核对':'开始核对'):'查看并核对'}</button>${d.state==='published'?`<button data-open="${esc(d.id)}" data-state="view" class="secondary-link">查看材料</button>`:''}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">${uiIcon('file')}<b>${all.length?'暂无'+esc(filter?.label||'')+'材料':'暂无材料'}</b><small>${all.length?'可以切换到全部材料查看。':'点击“上传材料”，开始办理。'}</small></div>`;
  bindOpenButtons();
}
async function refresh(){
  const fresh=await api('/api/status');
  const localDraftIds=new Set((state.documents||[]).filter(d=>d.hasLocalDraft).map(d=>d.id));
  state=fresh;state.documents.forEach(d=>{if(localDraftIds.has(d.id))d.hasLocalDraft=true});
  updateStatistics();renderDocuments();
  if(initialLoad){$('#intake').open=!state.documents.length;initialLoad=false}
  $('#batches').innerHTML=(state.batches||[]).map(b=>`<p><b>${esc(b.label)}</b> · 已用 ${esc(b.elapsed_seconds)} 秒 · 已登记 ${b.published}/${b.expected||b.documents} · 待补件 ${b.held} · 失败 ${b.failures||0} · ${b.all_published?'全部登记，仍需核对正确性':'尚未全部完成'}</p>`).join('');
  $('#model-summary').textContent=state.models.enabled?'已启用云端识别；是否识别成功请查看实际任务记录。':'未启用云端识别，可对照原件手动核对。';
  $('#model-status').textContent=JSON.stringify(state.models,null,2);
  if(!$('#jobs').contains(document.activeElement))$('#jobs').innerHTML=`<table><thead><tr><th>任务</th><th>状态</th><th>信息</th></tr></thead><tbody>${state.jobs.map(j=>`<tr><td>${esc(j.id)}<br>${esc(j.strategy)}</td><td>${esc(j.state)}</td><td>${esc(j.error||j.run_path||'等待处理')}</td></tr>`).join('')}</tbody></table>`;
  // Offer an explicit reload instead of replacing a material being read or edited.
  if(doc){const latest=state.documents.find(d=>d.id===doc.id);$('#document-update').hidden=!latest||latest.version<=doc.version;updateRequirements()}
}
function bindOpenButtons(){document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>action(async()=>{
  if(b.dataset.state==='published'){
    if(await tab('ledger')){const d=state.documents.find(d=>d.id===b.dataset.open);const card=[...document.querySelectorAll('[data-parcel]')].find(el=>el.dataset.parcel===d?.parcel_id);if(card){card.scrollIntoView({block:'start'});card.focus()}}
  }else if(await openDoc(b.dataset.open)){
    if(b.dataset.state==='held'){$('#attachments').open=true;$('#attachments').scrollIntoView({block:'start'});$('#attach-files').focus()}
  }
},b))}
async function loadGovernance(){governanceRows=await api('/api/governance');renderGovernance()}
function renderGovernance(){
  const filter=$('#governance-filter').value;const rows=governanceRows.filter(r=>!filter||r.categories.includes(filter));
  $('#governance-list').innerHTML=rows.length?rows.map(r=>`<article class="case-card"><div class="section-head"><h2>${esc(r.name)}</h2><button data-open="${r.id}" data-state="${esc(r.state)}">${r.state==='held'?'补充材料':'开始核对'}</button></div><div class="quality-stages"><span>材料处理：${esc(technicalLabels[r.technical_status]||r.technical_status)}</span><span>资料：${esc(r.material_status)}</span><span>登记状态：${esc(r.business_status)}</span></div><p>${labels[r.state]} · ${r.candidate_count} 项提取结果，需对照原件核对 · 版本 ${r.version}</p><ul>${r.issues.map(i=>`<li><b>${esc(i.category)}</b> · ${esc(locationLabel(i.location))}：${esc(i.message)}</li>`).join('')}</ul></article>`).join(''):'<div class="empty">该类别暂无待核验材料。</div>';
  bindOpenButtons();
}
$('#refresh-governance').onclick=()=>action(loadGovernance);$('#governance-filter').onchange=renderGovernance;
$('#refresh').onclick=()=>action(refresh);
$('#files').onchange=()=>{const files=[...$('#files').files];$('#file-summary').textContent=files.length?`已选择 ${files.length} 个文件 · ${files.map(f=>f.name).join('、')}`:'尚未选择文件'};
document.querySelectorAll('[data-sample]').forEach(b=>b.onclick=()=>action(async()=>{b.disabled=true;try{const r=await api('/api/sample/'+b.dataset.sample,{});await refresh();await openDoc(r.id);toast(r.duplicate?'已识别重复材料，未重复入账':'材料已接收，请对照原件核对')}finally{b.disabled=false}}));
$('#upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('name',$('#name').value);for(const f of $('#files').files)data.append('files',f);const r=await api('/api/import',data);await refresh();await openDoc(r.id)})};
$('#batch-upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('file',$('#batch-file').files[0]);const r=await api('/api/import-batch',data);toast(r.partial_failure?'部分文件导入失败，已保留成功项':'整包已接收，继续核验');await refresh()})};
function invalidate(){dirty=true;draft=null;$('#calculation-clear').checked=false;$('#calculation-status').hidden=!$('#calculation').children.length;updateRequirements()}
function unitOptions(selected){return doc.pages.map(p=>`<option value="${p.id}" ${p.id===selected?'selected':''}>${esc(p.name)}</option>`).join('')}
async function openDoc(id,skipGuard=false){
  if(!skipGuard&&await guardUnsaved()==='cancel')return false;
  doc=await api('/api/documents/'+id);const saved=doc.draft||doc.review||{};
  dirty=false;draft=null;started=Date.now();citationDetails=structuredClone(saved.citation_details||{});selectedPayments=saved.payment_ids||[];selectedReliefs=saved.relief_ids||[];
  await tab('review',true);$('#empty-review').classList.add('hidden');$('#review-content').classList.remove('hidden');
  $('#doc-name').textContent=doc.name;$('#doc-meta').textContent=`${labels[doc.state]} · 版本 ${doc.version} · ${doc.pages.length} 个原文位置`;
  $('#governance-status').textContent=`材料处理：${technicalLabels[doc.intake?.technical_status]||'历史资料'} ｜资料完整性：${doc.review?.material_complete?'已核对':'待核对'} ｜台账登记：${doc.state==='published'?'已确认':'未生效'}`;
  $('#inventory').innerHTML=(doc.intake?.files||[]).map(f=>`<p><b>${esc(f.name)}</b> · ${esc(f.format)} · ${esc(technicalLabels[f.technical_status]||f.technical_status)} · ${f.units} 个原文位置</p>`).join('')+`<details><summary>原件校验与完整位置清单</summary><pre>${esc(JSON.stringify({digest:doc.digest,files:doc.intake?.files,pages:doc.pages.map(p=>({id:p.id,name:p.name,kind:p.kind,location:p.location,lines:p.native_lines?.length,hidden:p.hidden,merged_ranges:p.merged_ranges}))},null,2))}</pre></details>`;
  $('#page-select').innerHTML=unitOptions(doc.pages[0]?.id);
  $('#merge-target').innerHTML='<option value="">选择目标文书</option>'+state.documents.filter(d=>d.id!==doc.id&&!['published','withdrawn','recognizing'].includes(d.state)).map(d=>`<option value="${d.id}">${esc(d.name)} · ${d.pages.length}页</option>`).join('');
  $('#evidence-field').innerHTML=Object.entries(state.fields).map(([k,label])=>`<option value="${k}">${label}</option>`).join('');
  $('#risks').innerHTML=(doc.governance_issues||[]).map(i=>`<div class="risk"><b>${esc(i.category)}</b> · ${esc(i.message)}</div>`).join('');
  $('#show-suggestions').checked=false;
  $('#fields').innerHTML=Object.entries(state.fields).map(([key,label])=>{
    const candidates=doc.result?.candidates?.[key]||[];
    return `<div class="field"><div class="field-title"><b>${label}</b><small>${candidates.length} 个候选</small></div><div class="candidates hidden">${candidates.map((c,i)=>`<button class="candidate" data-key="${key}" data-candidate="${i}">${esc(c.value)}<span>${c.kind==='native_extract'?'原生结构提取（非AI分数）':c.kind==='context_suggestion'?'上下文预计值 · 不能当原文':c.score_meaning==='not_provided'?'未提供识别分数':`${Math.round(c.score*100)}%识别分数（非正确概率）`} · ${esc(c.model)}</span><small>原值：${esc(c.raw_value??c.raw_text)}<br>${esc(c.reason)}<br>${esc(c.source_locator||c.source_page_id)}</small></button>`).join('')||'<small>无候选，不能推定事实不存在。</small>'}</div><input data-field="${key}" aria-label="${label}采纳值" value="${esc(saved.fields?.[key]||'')}" placeholder="人工核验后采纳" ${LISTS[key]?`list="${LISTS[key]}"`:''} ${key==='missing_pages'&&doc.hints?.missing_pages?`placeholder="系统提示：可能缺${esc(doc.hints.missing_pages)}，请看原件确认"`:''}><select data-cite="${key}" aria-label="${label}主要来源"><option value="">请选择证据单元</option>${unitOptions(saved.citations?.[key])}</select><div data-reference-list="${key}" class="references"></div></div>`;
  }).join('');
  document.querySelectorAll('[data-field]').forEach(e=>{e.oninput=invalidate;e.onfocus=()=>{$('#evidence-field').value=e.dataset.field;SelectControls.enhance()}});
  document.querySelectorAll('[data-cite]').forEach(e=>e.onchange=()=>{invalidate();if(e.value){$('#evidence-field').value=e.dataset.cite;showPage(e.value)}});
  document.querySelectorAll('[data-candidate]').forEach(b=>b.onclick=()=>{
    const key=b.dataset.key,c=doc.result.candidates[key][+b.dataset.candidate];$(`[data-field="${key}"]`).value=c.value;$(`[data-cite="${key}"]`).value=c.source_page_id;
    if(c.source_locator)for(const locator of c.source_locator.split(' + '))addReference(key,c.source_page_id,locator);else addReference(key,c.source_page_id,'');
    $('#evidence-field').value=key;showPage(c.source_page_id,c.bbox,c.source_locator?.split(' + ')[0]);invalidate();
  });
  Object.keys(state.fields).forEach(renderReferences);
  $('#reason').value=saved.reason||'';$('#uncertainty').value=saved.uncertainty||'';$('#stop-date').value=saved.stop||'';
  for(const id of ['material-clear','evidence-clear','payments-clear','calculation-clear'])$('#'+id).checked=false;
  $('#matching').innerHTML='';delete $('#matching').dataset.doc;$('#calculation').innerHTML='';$('#calculation-status').hidden=true;$('#confirm').disabled=true;
  const blocking=(doc.governance_issues||[]).filter(i=>i.severity==='blocking');
  $('#quality-resolutions').innerHTML=blocking.length?`<p><b>登记前需解决的问题</b></p>${blocking.map(i=>{const r=saved.quality_resolutions?.[i.id]||{};return `<div class="risk" data-quality="${i.id}"><b>${esc(locationLabel(i.location))} · ${esc(i.message)}</b><select data-decision aria-label="${esc(locationLabel(i.location))}处理结论"><option value="">未解决</option><option value="verified_with_evidence" ${r.decision==='verified_with_evidence'?'selected':''}>已有可核实证据</option><option value="excluded_with_reason" ${r.decision==='excluded_with_reason'?'selected':''}>有依据排除该内容</option></select><input data-resolution-reason aria-label="问题处理依据" placeholder="不能靠猜测；说明核对或排除依据" value="${esc(r.reason||'')}"><select data-resolution-unit aria-label="问题处理证据位置"><option value="">选择原文位置</option>${unitOptions(r.source_unit)}</select></div>`}).join('')}`:'';
  $('#audit').textContent=doc.audit.map(x=>`${new Date(x.at*1000).toLocaleString()} · ${x.actor} · ${x.action}\n${x.payload}`).join('\n\n');
  const locked=['published','withdrawn'].includes(doc.state);$('#retry').disabled=locked;$('#attach-form button').disabled=locked;$('#save-draft').disabled=locked;document.querySelectorAll('[data-field]').forEach(e=>e.readOnly=locked);document.querySelectorAll('[data-cite],[data-candidate],[data-decision],[data-resolution-reason],[data-resolution-unit]').forEach(e=>e.disabled=locked);
  showPage(doc.pages[0]?.id);applyPermissions();updateRequirements();$('#document-update').hidden=true;return true;
}
function addReference(key,unit_id,locator){const rows=citationDetails[key]||(citationDetails[key]=[]);if(!rows.some(r=>r.unit_id===unit_id&&r.locator===locator))rows.push({unit_id,locator});renderReferences(key)}
function renderReferences(key){const target=$(`[data-reference-list="${key}"]`);if(!target)return;target.innerHTML=(citationDetails[key]||[]).map((r,i)=>`<span>${esc(doc.pages.find(p=>p.id===r.unit_id)?.name||r.unit_id)} · ${esc(r.locator||'图像页')} <button data-remove-ref="${i}" aria-label="移除引用">×</button></span>`).join('');target.querySelectorAll('[data-remove-ref]').forEach(b=>b.onclick=()=>{citationDetails[key].splice(+b.dataset.removeRef,1);renderReferences(key);invalidate();applyPermissions()})}
function showPage(id,box,locator){
  const unit=doc.pages.find(p=>p.id===id);if(!unit)return;
  $('#page-select').value=id;$('#download-original').href=`/api/original/${doc.id}/${id}`;
  $('#rotate').disabled=!unit.path;$('#previous').disabled=!unit.path;
  $('#image-wrap').classList.toggle('hidden',!unit.path);$('#overlay').innerHTML='';
  if(unit.path){$('#original').onload=()=>{const im=$('#original');$('#overlay').setAttribute('viewBox',`0 0 ${im.naturalWidth} ${im.naturalHeight}`);if(box)$('#overlay').innerHTML=`<rect x="${box[0]}" y="${box[1]}" width="${box[2]-box[0]}" height="${box[3]-box[1]}" fill="#efb52c33" stroke="#e69712" stroke-width="5"/>`};$('#original').src=`/api/pages/${doc.id}/${id}?v=${doc.version}`}
  const lines=unit.native_lines||[];$('#native-evidence').classList.toggle('hidden',!lines.length&&!(!unit.path));
  $('#native-evidence').innerHTML=lines.length?`<h3>${esc(unit.name)} · 原文内容</h3><p class="muted">${unit.kind==='native'?'未模拟Word分页；按真实段落/单元格定位。':'PDF文字层与原图并存，请同时核验。'}</p><table><thead><tr><th>原件位置</th><th>原值与显示解释</th><th></th></tr></thead><tbody>${lines.map((l,i)=>`<tr class="${l.locator===locator?'evidence-selected':''}"><td>${esc(l.locator)}${l.hidden?' · 隐藏':''}</td><td><b>${esc(l.raw_text)}</b>${l.display_value!==undefined?`<br><small>显示解释：${esc(l.display_value)} · 格式 ${esc(l.number_format)}<br>XML原值：${esc(l.raw_xml_value)}${l.formula!==null&&l.formula!==undefined?`<br>公式：${esc(l.formula)} · 缓存：${esc(l.cached_value)}`:''}</small>`:''}${l.deleted_text?`<br>删除修订：${esc(l.deleted_text)}`:''}</td><td><button data-native-ref="${i}">引用</button></td></tr>`).join('')}</tbody></table>`:'<div class="error">该原件未能解析。可下载核查并补充受支持格式，不能标为已完成。</div>';
  document.querySelectorAll('[data-native-ref]').forEach(b=>b.onclick=()=>{const key=$('#evidence-field').value;addReference(key,id,lines[+b.dataset.nativeRef].locator);$(`[data-cite="${key}"]`).value=id;SelectControls.enhance();invalidate();toast('已关联原文位置，尚未采纳任何值')});
  SelectControls.enhance();
  $('#native-evidence .evidence-selected')?.scrollIntoView({block:'nearest'});applyPermissions();
}
$('#show-suggestions').onchange=()=>document.querySelectorAll('.candidates').forEach(e=>e.classList.toggle('hidden',!$('#show-suggestions').checked));
$('#page-select').onchange=()=>showPage($('#page-select').value);$('#reload-doc').onclick=()=>action(()=>openDoc(doc.id));
$('#retry').onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;await api(`/api/documents/${doc.id}/retry`,{version:doc.version});await openDoc(doc.id,true);toast('已创建处理任务；未配置模型会明确转人工')});
for(const [selector,operation] of [['#rotate','rotate'],['#previous','previous']])$(selector).onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;if(!confirm('调整原件页面会使旧确认和核算预览失效，需要重新核对。继续吗？'))return;await api(`/api/documents/${doc.id}/page`,{version:doc.version,page_id:$('#page-select').value,action:operation});await openDoc(doc.id,true)});
$('#attach-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!$('#attach-files').files.length)throw Error('先选择补充材料');if(await guardUnsaved()==='cancel')return;if(!confirm('补件会使旧草稿、确认和核算预览失效，原件仍会保留。继续上传吗？'))return;const data=new FormData();data.append('version',doc.version);for(const f of $('#attach-files').files)data.append('files',f);const r=await api(`/api/documents/${doc.id}/attach`,data);await openDoc(doc.id,true);toast(r.duplicate?'补件内容重复，无新增贡献':'补件已保存；旧确认、旧草稿和核算预览已失效')})};
$('#merge-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(await guardUnsaved()==='cancel')return;const id=$('#merge-target').value;if(!id)throw Error('先选择目标文书');const target=state.documents.find(d=>d.id===id);await api(`/api/documents/${doc.id}/merge-into`,{version:doc.version,target:id,target_version:target.version,reason:'补档分组修正'});await refresh();await openDoc(id,true);toast('已并入目标文书，目标需重新识别与确认')})};
function body(){
  const fields={},citations={},quality_resolutions={};document.querySelectorAll('[data-field]').forEach(e=>fields[e.dataset.field]=e.value.trim());document.querySelectorAll('[data-cite]').forEach(e=>citations[e.dataset.cite]=e.value);
  document.querySelectorAll('[data-quality]').forEach(e=>quality_resolutions[e.dataset.quality]={decision:e.querySelector('[data-decision]').value,reason:e.querySelector('[data-resolution-reason]').value,source_unit:e.querySelector('[data-resolution-unit]').value});
  return {version:doc.version,fields,citations,citation_details:citationDetails,payment_ids:selectedPayments,relief_ids:selectedReliefs,quality_resolutions,reason:$('#reason').value,uncertainty:$('#uncertainty').value,stop:$('#stop-date').value,material_complete:$('#material-clear').checked,evidence_clear:$('#evidence-clear').checked,confirm_payments:$('#payments-clear').checked,confirm_calculation:$('#calculation-clear').checked,review_elapsed_seconds:Math.round((Date.now()-started)/1000),draft_hash:draft?.draft_hash};
}
$('#save-draft').onclick=()=>action(saveDraft);
$('#preview').onclick=()=>action(async()=>{$('#calculation').innerHTML='';$('#calculation-status').hidden=true;draft=null;$('#calculation-clear').checked=false;updateRequirements();const r=await api(`/api/documents/${doc.id}/preview`,body());renderMatches(r.matches);if(r.errors.length){draft=null;$('#calculation').innerHTML=`<div class="error">${r.errors.map(esc).join('<br>')}</div>`;$('#confirm').disabled=true}else{draft=r;$('#calculation').innerHTML=renderCalc(r.calculation,r.previous);updateRequirements();toast('租金预览已生成，请核对后确认登记')}});
function renderMatches(m){
  if(!$('#matching').dataset.doc&&!doc.draft&&!doc.review){selectedPayments=m.payments.filter(p=>p.preselect).map(p=>String(p['序号']));selectedReliefs=m.reliefs.map(r=>r['批准文号']);}
  $('#matching').dataset.doc=doc.id;

  $('#matching').innerHTML=`<h3>核对登记、缴费与减免资料</h3><p class="muted">${esc(m.warning||'多候选需人工核对')} · 登记来源：02 不动产登记导出；匹配依据：${esc(m.registry_basis||'未取得匹配依据')}</p><details><summary>查看匹配的登记资料</summary>${businessTable('登记资料（接口匹配结果）',[['label','字段'],['value','内容']],(Array.isArray(m.registry)?m.registry:[m.registry]).filter(Boolean).flatMap((row,i)=>Object.entries(row).map(([label,value])=>({label:label,value}))))}</details><div class="payments">${m.payments.map(p=>`<label><input type="checkbox" data-payment="${esc(p['序号'])}" ${selectedPayments.includes(String(p['序号']))?'checked':''}>票${esc(p['票据号'])} · ${esc(p['缴费日期'])} · ${esc(p['金额（元）'])}元 · ${esc(p['所属年度']||'未注明年度')} · ${esc(p['缴款人'])}<small> · ${esc(p.match_basis||'请人工核对归属')}</small></label>`).join('')||'没有精确匹配的缴费候选；仍需核对原表，不能据此断言无缴费。'}</div>${m.reliefs.map(r=>`<label><input type="checkbox" data-relief="${esc(r['批准文号'])}" ${selectedReliefs.includes(r['批准文号'])?'checked':''}>${esc(r['批准文号'])} ${esc(r['减免比例'])}</label>`).join('')}`;
  document.querySelectorAll('[data-payment],[data-relief]').forEach(e=>e.onchange=()=>{selectedPayments=[...document.querySelectorAll('[data-payment]:checked')].map(e=>e.dataset.payment);selectedReliefs=[...document.querySelectorAll('[data-relief]:checked')].map(e=>e.dataset.relief);invalidate()});
}
function businessTable(title,columns,rows){return `<h3>${esc(title)}</h3><div class="table-scroll"><table><thead><tr>${columns.map(([key,label])=>`<th scope="col">${esc(label)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${columns.map(([key])=>`<td class="${['due','paid','arrears','amount','annual','prorated','rate','area'].includes(key)?'numeric':''}">${esc(row[key]??'—')}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${columns.length}">暂无记录，请核对资料覆盖范围。</td></tr>`}</tbody></table></div>`}
function renderCalc(p,previous){
 const old=previous?.payload;
 return `<div class="calc"><p>核算截至 ${esc(p.as_of)} · ${esc(p.ruleset)}</p><dl class="amounts">${[['due','应缴租金'],['paid','已缴租金'],['arrears','欠缴租金'],['overpaid','多缴金额']].map(([key,label])=>`<div class="${key==='arrears'?'amount-total':''}"><dt>${label}</dt><dd>¥ ${esc(p[key]??'—')}</dd></div>`).join('')}</dl><p>催缴对象：${esc(p.recipient)} · ${esc(p.phone||'电话待核实')}</p>
 ${businessTable('逐年核算',[['year','年度'],['due','应缴'],['paid','已归集'],['arrears','欠缴']],p.annual||[])}
 <p class="muted">年度欠缴与总欠缴可能因多缴款处理不同而不同；所有金额均采用服务端核算结果。</p>
 <details><summary>分段计租依据</summary>${businessTable('计租明细',[['year','年度'],['months','计租月数'],['area','面积'],['rate','费率'],['annual','全年租金'],['prorated','分段租金'],['source','依据']],p.segments||[])}</details>
 ${businessTable('缴费归集',[['receipt','票据号'],['year','归属年度'],['amount','归集金额']],(p.allocations||[]).map(x=>({...x,year:x.year??(x.type==='多缴'?'多缴／未归入年度':'未注明年度')})))}
 ${old?businessTable('版本对照',[['label','项目'],['old','上版'],['current','本版']],[['due','应缴'],['paid','已缴'],['arrears','欠缴']].filter(([k])=>old[k]!=null&&p[k]!=null).map(([k,label])=>({label,old:old[k],current:p[k]}))):'<p>无上一版核算数据，无法比较历史差异。</p>'}
 ${(p.scenarios||[]).map(x=>`<p class="risk">${esc(x.what)}：暂按 ${esc(x.chosen)}，差额 ${esc(x.difference)} 元，需核对依据。</p>`).join('')}
 <h3>核算提示</h3>${(p.issues||[]).map(x=>`<p class="risk">${esc(x.type||'待核对')}：${esc(x.reason||x.message||'请展开审计数据核查原始信息')}</p>`).join('')||'<p>未发现核算提示，仍须核对原件和核算依据。</p>'}<p>${esc(p.coverage)}</p><details><summary>审计数据</summary><pre>${esc(JSON.stringify(p,null,2))}</pre></details></div>`;
}
$('#confirm').onclick=()=>action(async()=>{if(!draft)throw Error('请先重新预览');await api(`/api/documents/${doc.id}/confirm`,body());dirty=false;draft=null;toast('已登记台账，核对记录已保存');await refresh();await openDoc(doc.id,true)});
$('#hold').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/draft`,body());await api(`/api/documents/${doc.id}/hold`,{version:doc.version,reason:$('#reason').value});dirty=false;await refresh();await openDoc(doc.id,true);toast('已转为待补件，尚未登记台账')});
$('#withdraw').onclick=()=>action(async()=>{if(!$('#reason').value.trim())throw Error('请先在核对说明中填写撤回原因');if(!confirm('撤回后，本份材料不再参与台账核算，系统将重新计算。原件和历史记录保留。确认撤回吗？'))return;await api(`/api/documents/${doc.id}/withdraw`,{version:doc.version,reason:$('#reason').value});dirty=false;await refresh();await openDoc(doc.id,true);toast('已撤回登记并重新核算，历史与原件保留')});
async function loadLedger(){
 $('#ledger-content').textContent='正在读取当前有效快照…';$('#notice-preview').innerHTML='';
 const q=new URLSearchParams({street:$('#f-street').value,status:$('#f-status').value,arrears:$('#f-arrears').value,keyword:$('#f-keyword').value});
 const [rows,stats,reminders]=await Promise.all([api('/api/ledger?'+q),api('/api/stats'),api('/api/reminders')]);
 $('#stats-panel').innerHTML=[['宗地',stats['宗地数']],['欠缴宗地',stats['欠缴宗地数']],['欠缴合计',stats['欠缴合计']],['累计收缴率',stats['累计收缴率']],['已到期未续',stats['已到期未续']],['90天内到期',stats['90天内到期']]].map(([k,v])=>`<div class="stat"><span>${k}</span><b>${esc(v)}</b></div>`).join('');
 $('#reminder-list').innerHTML=reminders.length?`<table><thead><tr><th>级别</th><th>日期</th><th>事项</th><th>对象</th></tr></thead><tbody>${reminders.map(r=>`<tr><td><span class="level level-${esc(r.level)}">${esc(r.level)}</span></td><td>${esc(r.date)}</td><td>${esc(r.kind)} · ${esc(r.message)}</td><td>${esc(r.recipient)}<br><small>${esc(r.address)} · ${esc(r.phone||'电话待核实')}</small></td></tr>`).join('')}</tbody></table>`:'<p class="muted">暂无提醒。</p>';
 $('#ledger-content').innerHTML=rows.length?rows.map(r=>`<div class="card" data-parcel="${esc(r.parcel_id)}" tabindex="-1"><div class="section-head"><h2>${esc(r.payload.address||'')} · ${esc(r.payload.recipient||'')}</h2><span class="tag">${esc(r.payload.ledger?.合同状态||'')} · 版本 ${r.id}</span></div>${renderCalc(r.payload)}<div class="actions"><button data-notice="${r.id}" ${Number(r.payload.arrears)>0?'':'disabled'}>生成催缴通知书与短信（不发送）</button><button data-contract="${esc(r.parcel_id)}">生成续期合同草稿</button></div></div>`).join(''):'<div class="empty">没有符合条件的台账。</div>';
 document.querySelectorAll('[data-notice]').forEach(b=>b.onclick=()=>action(async()=>{const n=await api('/api/notices/'+b.dataset.notice,{});$('#notice-preview').innerHTML=`<div class="card"><h2>催缴通知草稿 · 待审核</h2><p>短信（${n.sms.length}字，${n.sms.segments}条计费）：${esc(n.sms_draft)}</p>${n.phone_valid?'':'<div class="error">联系电话缺失或位数不对，需补充后再发送</div>'}${n.settings_configured?'':'<div class="error">缴费地点/经办电话未在 config/notice.json 配置</div>'}<a class="button" href="/api/notices/${n.id}/document">下载通知书 DOCX</a></div>`;await loadNotices()}));
 document.querySelectorAll('[data-contract]').forEach(b=>b.onclick=()=>action(async()=>{const start=prompt('新合同起始日（YYYY-MM-DD）');if(!start)return;const end=prompt('新合同终止日（YYYY-MM-DD）');if(!end)return;const r=await api('/api/contracts/draft',{parcel_id:b.dataset.contract,kind:'续期',start,end});$('#notice-preview').innerHTML=`<div class="card"><h2>续期合同草稿已归档</h2><p>年租金 ${esc(r.values['年租金小写'])} 元（${esc(r.values['年租金大写'])}），${esc(r.values['计租方式'])}</p><a class="button" href="/api/contracts/${r.id}/document">下载合同 DOCX</a><p class="muted">${esc(r.note)}</p></div>`}));
 await loadNotices();await loadTaxPushes();SelectControls.enhance();
}
async function loadNotices(){
 const rows=await api('/api/notices');
 $('#notice-list').innerHTML=rows.length?`<table><thead><tr><th>对象</th><th>欠缴</th><th>状态</th><th>登记结果</th></tr></thead><tbody>${rows.map(n=>`<tr><td>${esc(n.recipient)}<br><small>${esc(n.phone||'无电话')}</small></td><td>${esc(n.arrears)}</td><td>${esc(n.status)}</td><td><select data-notice-status="${n.id}"><option value="">登记…</option>${['已审核','已发送','发送失败','号码无效','已缴清','有异议','作废'].map(x=>`<option>${x}</option>`).join('')}</select> <a href="/api/notices/${n.id}/document">通知书</a></td></tr>`).join('')}</tbody></table>`:'<p class="muted">尚未生成催缴通知。</p>';
 SelectControls.enhance();
 document.querySelectorAll('[data-notice-status]').forEach(e=>e.onchange=()=>action(async()=>{if(!e.value)return;await api(`/api/notices/${e.dataset.noticeStatus}/events`,{status:e.value,note:''});toast('已登记：'+e.value);await loadNotices()}));
}
async function loadTaxPushes(){
 const rows=await api('/api/tax-pushes');
 $('#tax-list').innerHTML=rows.map(r=>`<p>${esc(r.period)} · ${r.rows} 行 · ${new Date(r.created*1000).toLocaleString()} · ${esc(r.actor)} · <a href="/api/tax-push/${r.id}/document">下载</a></p>`).join('')||'<p class="muted">尚无推送记录。</p>';
}
$('#ledger-filter').onsubmit=e=>{e.preventDefault();action(loadLedger)};
$('#tax-push').onclick=()=>action(async()=>{const r=await api('/api/tax-push',{year:+$('#tax-year').value,quarter:+$('#tax-quarter').value});toast(`已生成并留存 ${r.period} 推送单（${r.rows} 行）`);await loadTaxPushes()});
async function loadBatch(){
 $('#batch-reviewed').checked=false;
 $('#batch-confirm').disabled=true;
 $('#batch-confirm').dataset.docs='[]';
 const rows=await api('/api/batch-candidates');const ok=rows.filter(r=>r.eligible);
 $('#batch-list').innerHTML=rows.length?`<p>${ok.length} 份可核对后批量确认，${rows.length-ok.length} 份需逐项处理。同名弱匹配、缺失证据、低分或业务歧义不能批量入账。</p><table><thead><tr><th>档案</th><th>分流</th><th>关键字段与核算</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.name)}<br><button data-batch-review="${esc(r.id)}">查看原件与证据</button></td><td>${r.eligible?'待核对后确认':'人工复核：'+esc(r.reasons.slice(0,3).join('；'))+(r.reasons.length>3?`<details><summary>全部 ${r.reasons.length} 项原因</summary><p>${esc(r.reasons.join('；'))}</p></details>`:'')}</td><td><small>${esc(['tenant','parcel_no','area','start','end','mode','annual'].map(k=>r.fields[k]||'—').join(' · '))}</small>${r.calculation?`<p>应缴 ${esc(r.calculation.due)} · 实缴 ${esc(r.calculation.paid)} · 欠缴 ${esc(r.calculation.arrears)} 元</p><details><summary>逐年核算、缴费与减免依据</summary>${renderCalc(r.calculation)}<pre>${esc(JSON.stringify(r.matches,null,2))}</pre></details>`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">没有待处理材料。</p>';
 document.querySelectorAll('[data-batch-review]').forEach(b=>b.onclick=()=>action(()=>openDoc(b.dataset.batchReview)));
 $('#batch-confirm').dataset.docs=JSON.stringify(ok.map(r=>({id:r.id,version:r.version,draft_hash:r.draft_hash})));
}
$('#batch-refresh').onclick=()=>action(loadBatch);
$('#batch-reviewed').onchange=()=>{$('#batch-confirm').disabled=!$('#batch-reviewed').checked||!JSON.parse($('#batch-confirm').dataset.docs||'[]').length};
$('#batch-confirm').onclick=()=>action(async()=>{
 if(!$('#batch-reviewed').checked)throw Error('请逐份核对原件、资料完整性、缴费和核算后勾选声明');
 $('#batch-confirm').disabled=true;
 const r=await api('/api/batch-confirm',{documents:JSON.parse($('#batch-confirm').dataset.docs||'[]'),reason:'批量确认：已逐份核对原件、完整性、核算和缴费',reviewed_summary:true,evidence_clear:true,material_complete:true,confirm_calculation:true,confirm_payments:true});
 toast(`批量确认发布 ${r.published} 份；${r.results.filter(x=>!x.published).map(x=>x.reason).join('；')||'全部成功'}`);
 await loadBatch();await refresh();
});
$('#backup').onclick=()=>action(async()=>$('#backup-result').textContent=JSON.stringify(await api('/api/backup',{}),null,2));
for(const selector of ['#uncertainty','#stop-date'])$(selector).oninput=invalidate;
$('#reason').oninput=()=>{dirty=true;updateRequirements()};
let ruleDraft=null;
$('#read-rules').onclick=()=>action(async()=>{const r=await api('/api/rules');$('#rates-json').value=JSON.stringify(r.rates,null,2);$('#rule-impact').textContent='当前版本 '+r.active;ruleDraft=null;$('#confirm-rules').disabled=true});
$('#rates-json').oninput=()=>{$('#rule-calculation-clear').checked=false;ruleDraft=null;$('#confirm-rules').disabled=true};
$('#preview-rules').onclick=()=>action(async()=>{$('#rule-calculation-clear').checked=false;ruleDraft=await api('/api/rules/preview',{rates:JSON.parse($('#rates-json').value)});$('#rule-impact').textContent=JSON.stringify(ruleDraft,null,2);$('#confirm-rules').disabled=false});
$('#confirm-rules').onclick=()=>action(async()=>{if(!ruleDraft)throw Error('请先预览差异');const r=await api('/api/rules/confirm',{rates:JSON.parse($('#rates-json').value),reason:$('#rule-reason').value,draft_hash:ruleDraft.draft_hash,confirm_calculation:$('#rule-calculation-clear').checked});$('#rule-impact').textContent=JSON.stringify(r,null,2);ruleDraft=null;$('#confirm-rules').disabled=true;toast('新规则和重算已发布，旧通知保持原版本')});


function updateRequirements(){
  $('#confirm-rules').disabled=operationBusy||!ruleDraft||!$('#rule-calculation-clear').checked;
  if(!doc)return;
  $('#draft-state').textContent=dirty?'有未保存修改':(doc.draft||doc.review?'草稿已保存':'尚未保存草稿');
  $('#preview-state').textContent=draft?'预览有效':($('#calculation').children.length?'预览已失效':'未预览');
  const missing=[];
  if($('#role').value==='viewer')missing.push('当前为只读角色');
  if(doc.state==='published')missing.push('本份材料已登记；修改前需先撤回登记');
  if(doc.state==='withdrawn')missing.push('本份材料已撤回；重新上传后可继续办理');
  if(doc.state==='held')missing.push('本份材料待补件；补件或重新识别后再登记');
  if(doc.intake?.technical_status==='failed')missing.push('材料解析失败，请补充支持的文件格式');
  const latest=state.documents?.find(d=>d.id===doc.id);
  if(latest&&latest.version>doc.version)missing.push('材料已更新，请重新加载后核对');
  if(!draft)missing.push('请先预览租金');
  const unresolved=[...document.querySelectorAll('[data-quality]')].filter(e=>!e.querySelector('[data-decision]').value||!e.querySelector('[data-resolution-reason]').value.trim()||!e.querySelector('[data-resolution-unit]').value).length;
  if(unresolved)missing.push(`还有 ${unresolved} 项问题未处理`);
  const uncited=[...document.querySelectorAll('[data-field]')].filter(e=>{
    if(!e.value.trim())return false;
    const key=e.dataset.field, unitId=$(`[data-cite="${key}"]`).value, unit=doc.pages.find(p=>p.id===unitId);
    return !unit||(unit.kind==='native'&&!(citationDetails[key]||[]).some(r=>r.unit_id===unitId&&r.locator));
  }).length;
  if(uncited)missing.push(`还有 ${uncited} 项信息未关联原文位置`);
  if(!$('#reason').value.trim())missing.push('请填写核对说明');
  for(const [id,label] of [['material-clear','确认材料完整性'],['evidence-clear','确认已核对原件'],['payments-clear','确认缴费与减免归属'],['calculation-clear','完成租金核算复核']])if(!$('#'+id).checked)missing.push('请'+label);
  const immutable=['published','withdrawn'].includes(doc.state);
  const html=immutable?`<b>${doc.state==='published'?'本份材料已登记。需要修订时，请在其他操作中撤回登记。':'本份材料已撤回。重新上传后可继续办理。'}</b>`:missing.length?`<b>登记前还需完成</b><ul>${missing.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<b>已完成核对，可以确认登记。</b>';
  if($('#confirm-requirements').innerHTML!==html)$('#confirm-requirements').innerHTML=html;
  $('#remaining-count').textContent=immutable?'当前文书只读':missing.length?`登记前待办 · ${missing.length} 项`:'已完成核对';
  const destinations=missing.map(x=>x.includes('预览')?'preview':x.includes('问题')?'quality-resolutions':x.includes('原文位置')?'fields':x.includes('说明')?'reason':x.includes('更新')?'reload-doc':x.includes('完整性')?'material-clear':x.includes('原件')?'evidence-clear':x.includes('缴费')?'payments-clear':x.includes('核算')?'calculation-clear':'review-stage-3');
  const pending=(immutable?[]:missing).map((x,i)=>`<button data-jump="${destinations[i]}">${esc(x)}</button>`).join('');
  if($('#remaining-items').innerHTML!==pending)$('#remaining-items').innerHTML=pending;
  $('#confirm').disabled=operationBusy||!!missing.length;
  $('#preview').classList.toggle('primary',!draft);
  $('#confirm').classList.toggle('primary',!!draft);
}
function applyPermissions(){
  const readOnly=$('#role').value==='viewer', admin=$('#role').value==='admin';
  document.querySelectorAll('[data-admin]').forEach(el=>el.hidden=!admin);
  $('#admin-hint').hidden=admin;
  document.querySelectorAll('#upload button,#upload input,#batch-upload button,#batch-upload input,[data-sample]').forEach(el=>el.disabled=readOnly);
  if(doc){
    const locked=readOnly||['published','withdrawn'].includes(doc.state);
    document.querySelectorAll('[data-field]').forEach(el=>el.readOnly=locked);
    document.querySelectorAll('[data-cite],[data-candidate],[data-decision],[data-resolution-reason],[data-resolution-unit],[data-remove-ref],[data-native-ref],#uncertainty,#stop-date,#material-clear,#evidence-clear,#payments-clear,#calculation-clear,#attach-files').forEach(el=>el.disabled=locked);
    for(const id of ['retry','save-draft','preview','hold'])$('#'+id).disabled=locked;
    $('#attach-form button').disabled=locked||doc.state==='recognizing';
    $('#reason').readOnly=readOnly||doc.state==='withdrawn';
    $('#withdraw').disabled=readOnly||doc.state!=='published';
    const page=doc.pages.find(p=>p.id===$('#page-select').value);
    $('#rotate').disabled=locked||!page?.path;
    $('#previous').disabled=locked||!page?.path||doc.pages[0]?.id===page.id;
  }
  document.querySelectorAll('[data-notice]').forEach(el=>el.disabled=readOnly);
  SelectControls.enhance();
}
$('#open-upload').onclick=()=>{$('#intake').open=true;$('#intake').scrollIntoView({block:'start'});$('#name').focus()};
$('#all-documents').onclick=()=>{documentFilter='';updateStatistics();renderDocuments(true)};
$('#go-pending').onclick=()=>action(()=>tab('governance'));
$('#review-content').addEventListener('input',e=>{
  if(e.target.matches('[data-resolution-reason]'))invalidate();
});
$('#review-content').addEventListener('change',e=>{
  if(e.target.matches('[data-decision],[data-resolution-unit]'))invalidate();
  if(e.target.matches('#material-clear,#evidence-clear,#payments-clear,#calculation-clear')){dirty=true;updateRequirements()}
});
let selectedRole=$('#role').value;
$('#rule-calculation-clear').onchange=updateRequirements;
$('#role').onchange=()=>{
  const next=$('#role').value;$('#role').value=selectedRole;SelectControls.enhance();
  action(async()=>{const choice=await guardUnsaved();if(choice==='cancel')return;if(choice==='discard')await openDoc(doc.id,true);$('#role').value=next;selectedRole=next;applyPermissions();updateRequirements()});
};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});
$('.brand').onclick=e=>{e.preventDefault();action(()=>tab('inbox'))};
// Pass the originating control to action(), including keyboard-submitted forms.
for(const el of document.querySelectorAll('button,form,.brand,#role')){
  for(const name of ['onclick','onsubmit','onchange']){
    const handler=el[name];if(!handler)continue;
    el[name]=function(event){const old=invocationTrigger;invocationTrigger=event.submitter||this;try{return handler.call(this,event)}finally{invocationTrigger=old}};
  }
}
async function poll(){
  if(operationBusy||refreshPending||document.hidden||$('#unsaved-dialog').open)return;
  refreshPending=true;
  try{await refresh();$('#connection-status').hidden=true}
  catch(error){$('#connection-status').textContent='进度刷新失败：'+error.message+'。已保留当前输入，稍后自动重试。';$('#connection-status').hidden=false}
  finally{refreshPending=false}
}
SelectControls.enhance();applyPermissions();
document.querySelectorAll('nav button[data-tab]').forEach(b=>b.querySelector('span').textContent=pages[b.dataset.tab].title);
$('#title').textContent=pages.inbox.title;$('#breadcrumb-current').textContent=pages.inbox.title;$('#page-description').textContent=pages.inbox.description;
action(refresh);setInterval(poll,5000);

document.addEventListener('click',event=>{const b=event.target.closest('[data-jump]');if(!b)return;let target=document.getElementById(b.dataset.jump);if(!target)return;for(let p=target.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;target=target.matches('input,button,textarea')?target:target.querySelector('input:not(:disabled),button:not(:disabled)')||target;if(!target.matches('input,button,textarea,select,a[href]'))target.setAttribute('tabindex','-1');target.scrollIntoView({block:'center'});target.focus({preventScroll:true})});
