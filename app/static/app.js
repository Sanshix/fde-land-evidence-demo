const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const technicalLabels={parsed:"提取完成",partial:"部分完成",failed:"解析失败",legacy:"历史资料"};
const locationLabel=s=>state.fields?.[s]||({document:"整份文书"}[s])||s;
const LISTS={use:'uses',grade:'grades',mode:'modes',doc_type:'doc-types',has_map:'yes-no',altered_sealed:'yes-no'};
const labels = {queued:'等待处理',recognizing:'正在识别',pending_confirmation:'待确认',needs_human:'待核对',held:'待补件',published:'已登记',withdrawn:'已撤回'};
const completionLabels={completed:'识别读取完成，待核验',partial:'识别部分完成',unknown:'识别结果未知',failed:'识别未完成'};
const recognitionLabels={unconfigured:'未启用云识别',native_only:'原生结构提取',test_stub:'测试桩（非真实识别）','live-cloud':'云端识别','live-local':'本地识别',resumed_evidence:'复用已完成证据',existing_evidence:'已有证据'};
const pages={
  rent:{title:'租金核算',description:'逐年核算、缴费确认与规则版本。'},
  reminders:{title:'到期与欠缴提醒',description:'按查看日期分级提醒，金额保留原核算截至日。'},
  notices:{title:'催缴通知',description:'审核文书、发送任务与缴纳跟踪。'},
  contracts:{title:'合同生成和协议变更',description:'起草合同、预览确认及协议变更终止。'},
  tax:{title:'税务推送单',description:'预览确认后生成文件，留存每期内容。'},
  statistics:{title:'统计查询',description:'欠缴、到期及收缴率查询与导出。'},
  inbox:{title:'存量合同治理与新合同上传',description:'上传租赁材料，查看进度并继续处理。'},
  governance:{title:'待办事项',description:'先处理需要核对、补充材料或确认的问题。'},
  review:{title:'合同确认',description:'对照原件填写信息，核对租金后登记台账。'},
  ledger:{title:'一宗一档电子台账',description:'查看已确认的租金核算结果及历史记录。'},
  measure:{title:'系统管理',description:'查看识别状态，管理计租标准和数据备份。'}
};
const filters=[
  {key:'pending_confirmation',label:'待确认',states:['pending_confirmation'],icon:'confirmed',tip:'高可信且业务校验通过，逐份复看后可批量确认'},
  {key:'needs_human',label:'待核对',states:['needs_human'],icon:'pending',tip:'需要人工核对材料'},
  {key:'processing',label:'处理中',states:['queued','recognizing'],icon:'file',tip:'等待或正在提取信息'},
  {key:'held',label:'待补件',states:['held'],icon:'held',tip:'补充清晰、完整的材料'},
  {key:'published',label:'已登记',states:['published'],icon:'confirmed',tip:'已纳入有效宗地台账'}
];
const uiIcons={file:'<path d="M5 3h10l4 4v14H5zM14 3v5h5M8 12h8m-8 4h6"/>',pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',confirmed:'<path d="m4 12 5 5L20 6"/>',held:'<path d="m12 3 10 18H2L12 3Zm0 6v5m0 3h.01"/>'};
const uiIcon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true">${uiIcons[name]}</svg>`;
let reviewValidationAttempted=false;
let obligationEpoch=0, obligationPreview=null, obligationMode=false;
let state={}, doc=null, draft=null, started=0, dirty=false, citationDetails={}, selectedPayments=[], selectedReliefs=[], governanceRows=[];
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').style.display='block';toastTimer=setTimeout(()=>$('#toast').style.display='none',6500)}
// The timeout includes response-body parsing. A timed-out write may have committed;
// never retry it automatically or discard the user's draft.
async function api(path,body,method,timeoutMs=45000){
  const headers={'X-FDE-Role':$('#role').value,'X-FDE-Actor':encodeURIComponent($('#actor').value)};
  if(body && !(body instanceof FormData))headers['Content-Type']='application/json';
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(path,{method:method||(body?'POST':'GET'),headers,body:body?(body instanceof FormData?body:JSON.stringify(body)):undefined,signal:controller.signal});
    if(response.status===401)throw Error('需要已配置的本地账号登录');
    let value;try{value=await response.json()}catch(error){if(error.name==='AbortError')throw error;throw Error(`服务返回 ${response.status}，请刷新重试或查看任务日志`)}
    if(!response.ok)throw Error((response.status===409?'版本冲突，请重新加载材料并核对。当前输入已保留。':'')+(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail)));
    return value;
  }catch(error){if(error.name==='AbortError')throw Error(body?'请求超时，服务器可能已接收。当前输入已保留，请刷新核实结果后再操作，避免重复提交。':'请求超时，请检查连接后重试。当前输入已保留。');throw error}
  finally{clearTimeout(timer)}
}
const stamp=value=>value?new Date(Number(value)*1000).toLocaleString('zh-CN',{hour12:false}):'暂无时间';
let documentSearch='',documentSort='updated',documentPage=1,batchCandidates=[],activeRuleVersion='尚未读取';
const PAGE_SIZE=20;
let ledgerPage=1;
function writeRoute(){
  const q=new URLSearchParams({view:activeTab});
  if(doc)q.set('doc',doc.id);
  if(documentFilter)q.set('status',documentFilter);
  if(documentSearch)q.set('q',documentSearch);
  if(documentSort!=='updated')q.set('sort',documentSort);
  if(documentPage>1)q.set('page',documentPage);
  history.replaceState(null,'','#'+q);
}
async function restoreRoute(){
  const q=new URLSearchParams(location.hash.slice(1)),view=Object.hasOwn(pages,q.get('view'))?q.get('view'):'inbox';
  documentFilter=[...filters.map(f=>f.key),'withdrawn'].includes(q.get('status'))?q.get('status'):'';
  documentSearch=q.get('q')||'';documentSort=['updated','oldest','name'].includes(q.get('sort'))?q.get('sort'):'updated';
  documentPage=Math.max(1,parseInt(q.get('page'),10)||1);
  $('#document-search').value=documentSearch;$('#document-sort').value=documentSort;
  updateStatistics();renderDocuments(true);
  const id=q.get('doc');
  if(id&&state.documents.some(d=>d.id===id))await openDoc(id,true);
  else if(id)toast('该材料已不存在，请从列表重新选择');
  await tab(view,true);SelectControls.enhance();
}
function requestDialog({title,description,fields=[],submit='确认',danger=false,validate,content=''}){
  const modal=$('#action-dialog'),opener=document.activeElement;
  $('#action-title').textContent=title;$('#action-description').textContent=description;
  $('#action-fields').innerHTML=fields.map(f=>`<label for="dialog-${f.name}">${esc(f.label)}${f.required?'<span class="required-mark" aria-hidden="true"> *</span>':''}${f.options?`<select id="dialog-${f.name}" name="${f.name}" ${f.required?'required':''}><option value="">请选择</option>${f.options.map(o=>`<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select>`:f.type==='textarea'?`<textarea id="dialog-${f.name}" name="${f.name}" ${f.required?'required':''}></textarea>`:`<input id="dialog-${f.name}" name="${f.name}" type="${f.type||'text'}" ${f.required?'required':''}>`}</label>`).join('');
  $('#action-fields').insertAdjacentHTML('beforeend',content);
  $('#action-error').hidden=true;$('#action-submit').textContent=submit;$('#action-submit').classList.toggle('danger-fill',danger);
  return new Promise(resolve=>{
    const finish=value=>{modal.close();if(opener?.isConnected)opener.focus({preventScroll:true});resolve(value)};
    $('#action-cancel').onclick=()=>finish(null);modal.oncancel=e=>{e.preventDefault();finish(null)};
    $('#action-form').onsubmit=e=>{e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));const message=validate?.(values);if(message){$('#action-error').textContent=message;$('#action-error').hidden=false;return}finish(values)};
    modal.showModal();SelectControls.enhance();(fields.length?$('#action-fields input, #action-fields textarea, #action-fields select'):$('#action-cancel')).focus();
  });
}
const confirmAction=(description,title='确认操作')=>requestDialog({title,description,danger:true});
// All dynamic tables retain native semantics and a named keyboard scroll region.
function enhanceTables(){
  document.querySelectorAll('.tab table').forEach(table=>{
    let title=table.closest('[aria-label]')?.getAttribute('aria-label')||table.previousElementSibling?.textContent||table.closest('.card,.image-panel,.field-panel')?.querySelector('h2,h3')?.textContent||'业务明细';
    if(!table.caption){const caption=table.createCaption();caption.className='sr-only';caption.textContent=title}
    table.querySelectorAll('thead th').forEach(th=>{th.scope='col';if(!th.textContent.trim())th.textContent='操作'});
    let region=table.parentElement;
    if(!region.classList.contains('table-scroll')){region=document.createElement('div');region.className='table-scroll';table.before(region);region.append(table)}
    region.tabIndex=0;region.setAttribute('role','region');if(!region.hasAttribute('aria-label'))region.setAttribute('aria-label',title);
  });
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
    $('#unsaved-discard').onclick=()=>{dirty=false;finish('discard')};
    $('#unsaved-save').onclick=async()=>{
      const buttons=[...modal.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
      modal.oncancel=e=>e.preventDefault();$('#unsaved-save').textContent='正在保存…';
      try{await saveDraft();finish('saved')}
      catch(error){$('#unsaved-error').textContent='保存失败：'+error.message;$('#unsaved-error').hidden=false}
      finally{buttons.forEach(b=>b.disabled=false);$('#unsaved-save').textContent='保存后继续';modal.oncancel=e=>{e.preventDefault();finish('cancel')}}
    };
    modal.showModal();SelectControls.enhance();$('#unsaved-cancel').focus();
  });
}
async function tab(name, skipGuard=false){
  if(!skipGuard&&name!==activeTab){
    const choice=await guardUnsaved();if(choice==='cancel')return false;
    if(choice==='discard')await openDoc(doc.id,true);
  }
  SelectControls.close();activeTab=name;document.body.dataset.view=name;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==name));
  document.querySelectorAll('nav button').forEach(x=>{x.classList.toggle('active',x.dataset.tab===(['governance','review'].includes(name)?'inbox':name));if(x.dataset.tab===(['governance','review'].includes(name)?'inbox':name))x.setAttribute('aria-current','page');else x.removeAttribute('aria-current')});
  $('#title').textContent=pages[name].title;$('#breadcrumb-current').textContent=pages[name].title;
  $('#page-description').textContent=pages[name].description;$('#open-upload').hidden=name!=='inbox';
  window.scrollTo({top:0,behavior:'instant'});
  $('#governance-subnav').hidden=!['inbox','governance','review'].includes(name);
  syncContractSubnav();
  if(name==='review')await loadContractDrafts();
  writeRoute();if(['ledger','rent','reminders','notices','contracts','tax','statistics'].includes(name))await loadBusinessPage(name);if(name==='measure'){const rules=await api('/api/rules');activeRuleVersion=rules.active;renderSystemSummary()}if(name==='governance'){await loadGovernance();await loadBatch();}
  return true;
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>action(()=>tab(b.dataset.tab),b));
function syncContractSubnav(){
  const supplement=activeTab==='inbox'&&documentFilter==='held';
  document.querySelectorAll('[data-sub]').forEach(button=>{
    const selected=button.dataset.sub===activeTab&&(activeTab!=='inbox'||Boolean(button.dataset.documentFilter)===supplement);
    button.setAttribute('aria-pressed',String(selected));
  });
  if(activeTab==='inbox'){
    $('#title').textContent=supplement?'合同待补录':pages.inbox.title;
    $('#breadcrumb-current').textContent=$('#title').textContent;
    $('#page-description').textContent=supplement?'集中处理已转为待补件的合同，补充原件或信息后继续核验。':pages.inbox.description;
  }
}
function updateStatistics(){
  syncContractSubnav();
  const docs=state.documents||[];
  if(!$('#stats').children.length)$('#stats').innerHTML=filters.map(f=>`<button class="stat stat-${f.icon}" data-filter="${f.key}" aria-pressed="false"><span class="stat-top"><span>${f.label}</span><span class="stat-icon">${uiIcon(f.icon)}</span></span><span class="stat-value"><b>0</b><span>份</span></span><span class="stat-foot">${f.tip}</span></button>`).join('');
  filters.forEach(f=>{const b=$(`[data-filter="${f.key}"]`);b.querySelector('b').textContent=docs.filter(d=>f.states.includes(d.processing_state||d.state)).length;b.setAttribute('aria-pressed',String(documentFilter===f.key));b.onclick=()=>setDocumentFilter(f.key)});
  $('#all-documents').setAttribute('aria-pressed',String(!documentFilter));$('#document-status').value=documentFilter;SelectControls.enhance();
}
function renderDocuments(force=false){
  const all=state.documents||[], filter=filters.find(f=>f.key===documentFilter);
  const matched=all.filter(d=>(!documentFilter||(filter?filter.states.includes(d.processing_state||d.state):d.state===documentFilter))&&(!documentSearch||`${d.name} ${d.id}`.toLocaleLowerCase().includes(documentSearch.toLocaleLowerCase())));
  matched.sort((a,b)=>documentSort==='name'?a.name.localeCompare(b.name,'zh-CN'):documentSort==='oldest'?(a.updated||0)-(b.updated||0):(b.updated||0)-(a.updated||0));
  const totalPages=Math.max(1,Math.ceil(matched.length/PAGE_SIZE));documentPage=Math.min(documentPage,totalPages);
  const docs=matched.slice((documentPage-1)*PAGE_SIZE,documentPage*PAGE_SIZE);
  const signature=JSON.stringify([documentFilter,documentSearch,documentSort,documentPage,matched.length,docs]);
  // Never replace a focused row during polling; render it after focus leaves.
  if(!force&&($('#documents').contains(document.activeElement)||signature===listSignature))return;
  listSignature=signature;$('#document-count').textContent=`${matched.length} 份材料`;
  $('#filter-description').textContent=`${filter?.label||labels[documentFilter]||'全部材料'}${documentSearch?' · 搜索“'+documentSearch+'”':''} · ${matched.length} 份结果`;
  $('#page-info').textContent=`第 ${documentPage} / ${totalPages} 页 · 每页 ${PAGE_SIZE} 份`;
  $('#page-prev').disabled=documentPage<=1;$('#page-next').disabled=documentPage>=totalPages;
  $('#documents').innerHTML=docs.length?`<table><thead><tr><th>档案名称</th><th>处理状态</th><th>材料 / OCR 情况</th><th>更新时间 / 版本</th><th>下一步</th></tr></thead><tbody>${docs.map(d=>`<tr><td><b>${esc(d.name)}</b><details><summary>档案编号</summary><small>${esc(d.id)}</small></details></td><td><span class="badge-state state-${esc(d.processing_state||d.state)}">${esc(labels[d.processing_state||d.state]||d.state)}</span>${confidenceSummary(d.confidence,true,d.state==='published')}</td><td><span class="${d.intake?.technical_status==='failed'?'failure-text':''}">${esc(technicalLabels[d.intake?.technical_status]||'待处理')}</span><br><small>${d.pages.length} 个原文位置${d.result?' · '+esc(completionLabels[d.result.completion_status]||recognitionLabels[d.result.mode]||d.result.mode):' · 可对照原件手动核对'}</small></td><td class="timestamp">${esc(stamp(d.updated))}<br><small>版本 ${esc(d.version)}</small></td><td><button data-open="${esc(d.id)}" data-state="${esc(d.state)}">${d.state==='published'?'查看台账':d.state==='held'?'补充材料':d.state==='withdrawn'?'查看材料':d.processing_state==='pending_confirmation'?'开始确认':d.state==='needs_human'?(d.review||d.hasLocalDraft?'继续核对':'开始核对'):'查看并核对'}</button>${d.state==='published'?`<button data-open="${esc(d.id)}" data-state="view" class="secondary-link">查看材料</button>`:''}</td></tr>`).join('')}</tbody></table>`:`<div class="empty">${uiIcon('file')}<b>${all.length?'暂无'+esc(filter?.label||'')+'材料':'暂无材料'}</b><small>${all.length?'可以切换到全部材料查看。':'点击“上传材料”，开始办理。'}</small></div>`;
  if($('#withdrawal-queue'))$('#withdrawal-queue').innerHTML=(state.withdrawals||[]).length?'<h3>撤回登记待审核</h3>'+(state.withdrawals||[]).map(r=>`<p>${esc(r.name)} <button data-open="${esc(r.document_id)}" data-state="view">审核申请</button></p>`).join(''):'';
  bindOpenButtons();enhanceTables();writeRoute();
}
async function refresh(){
  let fresh;
  try{fresh=await api('/api/status')}catch(error){
    if(initialLoad){$('#documents').innerHTML=`<div class="empty"><b>材料读取失败</b><small>${esc(error.message)}</small><button id="retry-list">重试读取</button></div>`;$('#retry-list').onclick=()=>action(refresh,$('#retry-list'))}
    throw error;
  }
  const localDraftIds=new Set((state.documents||[]).filter(d=>d.hasLocalDraft).map(d=>d.id));
  state=fresh;state.documents.forEach(d=>{if(localDraftIds.has(d.id))d.hasLocalDraft=true});
  updateStatistics();renderDocuments();await loadApprovalDashboard();
  if(initialLoad){$('#intake').open=!state.documents.length;initialLoad=false}
  $('#batches').innerHTML=(state.batches||[]).map(b=>`<p><b>${esc(b.label)}</b> · 已用 ${esc(b.elapsed_seconds)} 秒 · 已登记 ${b.published}/${b.expected||b.documents} · 待补件 ${b.held} · 失败 ${b.failures||0} · ${b.all_published?'全部登记，仍需核对正确性':'尚未全部完成'}</p>`).join('');
  $('#model-summary').textContent=state.test_space?.mode==='replay'?'测试缓存回放：历史模型响应，当前规则重新解析；本次未调用模型。':state.models.enabled?'已启用模型识别；部署位置与实际结果请查看模型和任务记录。':'未启用模型识别，可对照原件手动核对。';
  $('#model-status').textContent=JSON.stringify(state.models,null,2);
  if(!$('#jobs').contains(document.activeElement)){
    const roles={reviewer:'审核员',admin:'合同管理员',viewer:'只读观察员'};
    const statuses={queued:'等待处理',running:'处理中',fallback_queued:'等待备用识别',fallback_running:'备用识别中',done:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断',partial:'部分完成',unknown:'结果未知',needs_human:'待人工核验'};
    $('#jobs').innerHTML=`<h3>识别任务</h3><table><thead><tr><th>任务</th><th>操作人</th><th>操作角色</th><th>发起时间</th><th>当前状态</th><th>信息</th></tr></thead><tbody>${state.jobs.map(j=>`<tr><td>${esc(j.id)}<br>${esc(j.strategy)}</td><td>${esc(j.actor||'未记录')}</td><td>${esc(roles[j.role]||'未记录')}</td><td>${stamp(j.requested_at||j.created)}</td><td>${esc(statuses[j.state]||j.state)}</td><td>${j.document_id?`<button data-open="${esc(j.document_id)}">查看关联材料</button>`:''}${j.error?`<p class="error">${esc(j.error)}</p>`:''}<details><summary>技术记录</summary><pre>${esc(j.run_path||'尚无运行路径')}</pre></details></td></tr>`).join('')||'<tr><td colspan="6">暂无任务</td></tr>'}</tbody></table>`;
    const names={contract_draft:'起草合同',import:'导入材料',retry:'重新识别',recognition_primary:'主模型识别',recognition_fallback:'备用识别',recognition_failed:'识别失败'};
    $('#jobs').innerHTML+=`<h3>操作追溯记录</h3><table><thead><tr><th>操作</th><th>操作人</th><th>操作角色</th><th>操作时间</th><th>状态</th><th>信息</th></tr></thead><tbody>${(state.operations_log||[]).map(r=>{const p=JSON.parse(r.payload);return `<tr><td>${esc(names[r.action]||r.action)}</td><td>${esc(r.actor)}</td><td>${esc(roles[p.operation_role]||(r.actor==='cloud-worker'?'系统服务':'未记录'))}</td><td>${stamp(r.at)}</td><td>${r.action==='recognition_failed'?'失败':'已记录'}</td><td>关联对象：${esc(r.target)}<details><summary>操作详情</summary><pre>${esc(JSON.stringify(p,null,2))}</pre></details></td></tr>`}).join('')||'<tr><td colspan="6">暂无操作记录</td></tr>'}</tbody></table>`;
  }
  // Offer an explicit reload instead of replacing a material being read or edited.
  renderSystemSummary();
  enhanceTables();bindOpenButtons();
  if(doc){const latest=state.documents.find(d=>d.id===doc.id);$('#document-update').hidden=!latest||latest.version<=doc.version;updateRequirements()}
}
function renderSystemSummary(){
  $('#system-summary').innerHTML=`<span>识别：${state.models.enabled?'已启用':'未启用'}</span><span>最近任务成功 ${state.jobs.filter(j=>j.state==='done').length}</span><span>最近任务失败 ${state.jobs.filter(j=>j.state==='failed').length}</span><span>规则版本：${esc(activeRuleVersion)}</span><span>备份：${$('#backup-result').textContent?'已返回备份记录':'本会话未执行'}</span>`;
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
  const filter=$('#governance-filter').value;const eligible=new Set(batchCandidates.filter(r=>r.eligible).map(r=>r.id));const rows=governanceRows.filter(r=>!eligible.has(r.id)&&(!filter||r.categories.includes(filter)));
  $('#governance-counts').innerHTML=`<span>待办 ${governanceRows.length} 份</span>`+['识别问题','原件问题','业务冲突'].map(c=>`<span>${c} ${governanceRows.filter(r=>r.categories.includes(c)).length}</span>`).join('');
  $('#governance-list').innerHTML=rows.length?rows.map(r=>`<article class="case-card"><div class="section-head"><h2>${esc(r.name)}</h2><button data-open="${r.id}" data-state="${esc(r.state)}">${r.state==='held'?'补充材料':'开始核对'}</button></div><div class="quality-stages"><span>材料处理：${esc(technicalLabels[r.technical_status]||r.technical_status)}</span><span>资料：${esc(r.material_status)}</span><span>登记状态：${esc(r.business_status)}</span></div>${r.review_priority?`<p class="queue-priority">${esc(r.review_priority.reason)}</p>`:''}<p>${labels[r.processing_state||r.state]} · ${r.candidate_count} 项提取结果，需对照原件核对 · 版本 ${r.version}</p>${confidenceSummary(r.confidence,true)}<ul>${r.issues.slice(0,1).map(i=>`<li><b>${esc(i.category)}</b> · ${esc(locationLabel(i.location))}：${esc(i.message)}</li>`).join('')}</ul>${r.issues.length>1?`<details><summary>另 ${r.issues.length-1} 项问题</summary><ul>${r.issues.slice(1).map(i=>`<li>${esc(i.category)}：${esc(i.message)}</li>`).join('')}</ul></details>`:''}</article>`).join(''):'<div class="empty">该类别暂无待核验材料。</div>';
  bindOpenButtons();
}
$('#refresh-governance').onclick=()=>action(loadGovernance);$('#governance-filter').onchange=renderGovernance;
$('#refresh').onclick=()=>action(refresh);
$('#files').onchange=()=>{const files=[...$('#files').files];$('#file-summary').textContent=files.length?`已选择 ${files.length} 个文件 · ${files.map(f=>f.name).join('、')}`:'尚未选择文件'};
document.querySelectorAll('[data-sample]').forEach(b=>b.onclick=()=>action(async()=>{b.disabled=true;try{const r=await api('/api/sample/'+b.dataset.sample,{});await refresh();await openDoc(r.id);toast(r.duplicate?'已识别重复材料，未重复入账':'材料已接收，请对照原件核对')}finally{b.disabled=false}}));
$('#upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('name',$('#name').value);for(const f of $('#files').files)data.append('files',f);const r=await api('/api/import',data);await refresh();await openDoc(r.id)})};
$('#batch-upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('file',$('#batch-file').files[0]);const r=await api('/api/import-batch',data);toast(r.partial_failure?'部分文件导入失败，已保留成功项':'整包已接收，继续核验');await refresh()})};
function invalidateObligation(message='试算已失效，请重新核对后试算。', resetChecks=true){
  obligationEpoch++;obligationPreview=null;
  $('#obligation-result').innerHTML='';$('#obligation-state').textContent=message;
  if(resetChecks)for(const id of ['obligation-evidence-clear','obligation-reliefs-clear'])$('#'+id).checked=false;
  updateObligationButton();
}
function updateObligationButton(){
  if(!$('#obligation-preview'))return;
  const latest=state.documents?.find(d=>d.id===doc?.id);
  $('#obligation-preview').disabled=operationBusy||!doc||$('#role').value==='viewer'||['published','withdrawn'].includes(doc?.state)||!!(latest&&latest.version>doc.version)||!$('#obligation-evidence-clear').checked||!$('#obligation-reliefs-clear').checked;
}
function obligationSignature(){
  const b=body();return JSON.stringify({id:doc.id,version:doc.version,fields:b.fields,citations:b.citations,reliefs:b.relief_ids,month:$('#obligation-termination-month').value,source:$('#obligation-termination-source').value,evidence:$('#obligation-evidence-clear').checked,reliefCheck:$('#obligation-reliefs-clear').checked});
}
function invalidate(){if($('#approval-check-result').textContent)$('#approval-check-result').innerHTML='<p>字段已修改，请重新核查。</p>';dirty=true;draft=null;invalidateObligation();$('#registration-clear').checked=false;$('#calculation-clear').checked=false;$('#calculation-status').hidden=!$('#calculation').children.length;updateRequirements()}
// Groups follow the API's 26-field insertion order; never reorder submission keys.
const FIELD_GROUPS={doc_type:'合同与签约主体',start:'租期与租金',parcel_no:'宗地信息',has_map:'原件质量'};
function fieldInputAttributes(key,value){
  // Preserve noncanonical stored evidence verbatim instead of letting date/number
  // controls silently erase it. Validation remains the server's responsibility.
  if(['signed','start','end'].includes(key))return !value||/^\d{4}-\d{2}-\d{2}$/.test(value)?'type="date"':'type="text" inputmode="numeric"';
  if(['area','annual','rate_standard'].includes(key))return !value||/^-?\d+(\.\d+)?$/.test(value)?'type="number" step="any" inputmode="decimal"':'type="text" inputmode="decimal"';
  if(key==='phone')return 'type="tel"';
  return 'type="text"';
}
function groupFields(){
  let group;
  const order=['doc_type','contract_no','original_contract_no','signed','lessor','tenant','identity','phone','residence','start','end','mode','annual','annual_upper','rate_standard','parcel_no','address','area','use','grade','has_map','missing_pages','altered_field','altered_before','altered_sealed','note'];
  for(const row of [...$('#fields').children].sort((a,b)=>order.indexOf(a.dataset.fieldRow)-order.indexOf(b.dataset.fieldRow))){
    const label=FIELD_GROUPS[row.dataset.fieldRow];
    if(label){group=document.createElement('fieldset');group.className='field-group';const legend=document.createElement('legend');legend.textContent=label;group.append(legend);$('#fields').append(group)}
    group.append(row);
  }
}
function updateFieldStates(){
  document.querySelectorAll('[data-field-state]').forEach(el=>{
    const key=el.dataset.fieldState,value=$(`[data-field="${key}"]`).value,unit=$(`[data-cite="${key}"]`).value;
    const source=doc.pages.find(p=>p.id===unit),linked=source&&(source.kind!=='native'||(citationDetails[key]||[]).some(r=>r.unit_id===unit&&r.locator));
    const verification=doc.field_verification?.[key];
    el.textContent=verification?'已人工核验':!value?'待填写':'';
  });
}
function selectField(key,locate=true){
  $('#evidence-field').value=key;
  document.querySelectorAll('[data-field-row]').forEach(row=>{const current=row.dataset.fieldRow===key;row.classList.toggle('current-field',current);row.querySelector('.candidates').classList.toggle('hidden',!current||!$('#show-suggestions').checked)});
  const id=$(`[data-cite="${key}"]`)?.value,reference=citationDetails[key]?.find(r=>r.unit_id===id);
  if(locate&&id)showPage(id,undefined,reference?.locator);
  $('#evidence-live').textContent=id?`已关联：${state.fields[key]}，${doc.pages.find(p=>p.id===id)?.name||id}${reference?.locator?'，'+reference.locator:''}`:`当前字段：${state.fields[key]}。尚未关联原文证据。`;
  updateFieldStates();SelectControls.enhance();
}
function renderSourceIssues(){
  const issues=[];
  if(doc.hints?.missing_pages)issues.push('可能缺页：'+doc.hints.missing_pages+'。请核实页序并补充材料。');
  const failedJob=(state.jobs||[]).find(j=>j.document_id===doc.id&&j.state==='failed');
  if(failedJob)issues.push('识别任务失败：'+(failedJob.error||'请查看任务日志')+'。可重新识别或对照原件手动核对。');
  for(const unit of doc.pages)if(unit.hidden)issues.push(`${unit.name} 包含隐藏内容，请展开原件核实。`);
  for(const file of doc.intake?.files||[])if(file.technical_status!=='parsed')issues.push(`${file.name}：${technicalLabels[file.technical_status]||file.technical_status}。请下载核实或补充受支持格式。`);
  const stages={primary:'主识别',secondary:'独立复核',third:'第三模型',fourth:'第四模型',context:'上下文提取'};
  const statuses={failed:'失败',budget_exhausted:'处理时限耗尽',not_configured:'未配置',error:'异常'};
  for(const page of doc.result?.pages||[])for(const stage of page.stages||[])if(statuses[stage.status])issues.push(`${doc.pages.find(p=>p.id===page.id)?.name||page.id} · ${stages[stage.stage]||stage.stage}：${statuses[stage.status]}。${stage.error||stage.reason||'请核查任务日志；可对照原件手动核对。'}`);
  return issues.length?`<div class="source-issues"><h3>原件与识别待办</h3>${issues.map(x=>`<p class="risk">⚠ ${esc(x)}</p>`).join('')}</div>`:'';
}
function renderParcelSummary(p){
  const l=p.ledger||{};
  return `<div class="parcel-summary">${businessTable('合同—登记对照',[['label','项目'],['contract','合同 / 核算采纳'],['registry','登记 / 依据']],[
    {label:'面积（㎡）',contract:l.合同面积,registry:l.登记面积},
    {label:'承租人 / 权利人',contract:l.合同承租人,registry:l.登记权利人},
    {label:'用途',contract:l.用途,registry:'此快照未提供独立登记用途'},
    {label:'土地级别',contract:l.土地级别,registry:l.土地级别依据},
    {label:'租期终止日',contract:l.合同终止日,registry:'完整租期见合同原件'},
    {label:'计租方式',contract:l.计租方式,registry:(p.registry_basis||[]).join('、')||'未提供登记匹配依据'}
  ])}</div>`;
}

function unitOptions(selected){return doc.pages.map(p=>`<option value="${p.id}" ${p.id===selected?'selected':''}>${esc(p.name)}</option>`).join('')}
function alterationReview(){
  if($('#alteration-assistance').hidden)return undefined;
  const decision=$('[name="seal-decision"]:checked')?.value;
  return decision?{decision,unit_id:$('#alteration-source').value}:undefined;
}
function syncAlterationControls(){
  if(!doc)return;
  const altered=doc.review_assistance?.altered||!!$('[data-field="altered_field"]')?.value.trim();
  $('#alteration-assistance').hidden=!altered;
  const selected=$('[name="seal-decision"]:checked');
  if(selected&&$('[data-field="altered_sealed"]').value!==({present:'是',absent:'否',unknown:'无法判断'}[selected.value]))selected.checked=false;
  const decision=alterationReview();
  $('#seal-decision-hint').textContent=decision?.decision==='unknown'?'签章无法判断：请保存草稿并转为待补件，取得清晰证据后再继续核验。':decision?.decision==='absent'?'已选无签章：核对涂改前后原文后，在租金预览中检查存疑暂算和金额差异。':decision?.decision==='present'?'已选有签章：请确认手印或盖章位于涂改处，并核对采用的涂改后原文。':'尚未作出真人核验结论。';
}
function renderAlterationAssistance(saved){
  const assistance=doc.review_assistance||{};
  const regions=[...(assistance.candidates||[]),...(assistance.seal_regions||[])];
  $('#seal-region-note').textContent=assistance.seal_region_note||'未取得签章区域定位，请在完整原件中核对手印或盖章。';
  $('#alteration-impact-note').textContent=assistance.amount_impact_note||'金额影响以采纳原文后的租金预览为准。';
  $('#alteration-source').innerHTML='<option value="">请选择判断依据所在原件</option>'+unitOptions(saved.alteration_review?.unit_id);
  const roleLabels={crossed_out:'涂改前候选',current:'涂改后候选',alternative:'备选原文',seal:'签章候选区域',unspecified:'原文候选'};
  $('#alteration-candidates').innerHTML=regions.length?regions.map((c,i)=>`<figure class="alteration-candidate"><figcaption><b>${esc(roleLabels[c.role]||'原文候选')} · ${esc(c.label)}</b><span>${esc(c.value)}</span></figcaption>${c.bbox?`<canvas data-alteration-crop="${i}" role="img" aria-label="${esc(roleLabels[c.role]||'原文候选')}：${esc(c.raw_text||c.value)}"></canvas>`:'<p class="caption">未取得区域定位，请查看完整原件。</p>'}<button data-alteration-open="${i}">查看完整原件</button><small>${esc(doc.pages.find(p=>p.id===c.page_id)?.name||c.page_id)}</small></figure>`).join(''):'<p class="caption">未取得可定位的涂改前后候选，请在左侧完整原件中核对。</p>';
  document.querySelectorAll('[data-alteration-open]').forEach(button=>button.onclick=()=>{
    const c=regions[+button.dataset.alterationOpen];showPage(c.page_id,c.bbox);$('#original').closest('.image-panel').scrollIntoView({block:'nearest'});
  });
  const documentId=doc.id,version=doc.version;
  document.querySelectorAll('[data-alteration-crop]').forEach(canvas=>{
    const region=regions[+canvas.dataset.alterationCrop],image=new Image();
    image.onload=()=>{
      if(!canvas.isConnected||doc.id!==documentId||doc.version!==version)return;
      const [x1,y1,x2,y2]=region.bbox,pad=16;
      const x=Math.max(0,x1-pad),y=Math.max(0,y1-pad),w=Math.min(image.naturalWidth,x2+pad)-x,h=Math.min(image.naturalHeight,y2+pad)-y;
      if(w<=0||h<=0){canvas.replaceWith(document.createTextNode('区域超出原件，请查看完整原件。'));return}
      const scale=Math.min(2,640/w,240/h);canvas.width=Math.max(1,Math.round(w*scale));canvas.height=Math.max(1,Math.round(h*scale));
      canvas.getContext('2d').drawImage(image,x,y,w,h,0,0,canvas.width,canvas.height);
    };
    image.onerror=()=>{if(canvas.isConnected)canvas.replaceWith(document.createTextNode('原件预览读取失败，请查看完整原件。'))};
    image.src=`/api/pages/${documentId}/${region.page_id}?v=${version}`;
  });
  document.querySelectorAll('[name="seal-decision"]').forEach(input=>{
    input.checked=saved.alteration_review?.decision===input.value;
    input.onchange=()=>{$('[data-field="altered_sealed"]').value={present:'是',absent:'否',unknown:'无法判断'}[input.value];invalidate();updateFieldStates();syncAlterationControls()};
  });
  $('#alteration-source').onchange=()=>{invalidate();syncAlterationControls()};
  syncAlterationControls();
}
async function openDoc(id,skipGuard=false){
  if(!skipGuard&&await guardUnsaved()==='cancel')return false;
  invalidateObligation('尚未试算；请先核对上方两项。');
  doc=await api('/api/documents/'+id);const existing=doc.draft||doc.review||{};const saved={...existing,fields:{...doc.prefill?.fields,...existing.fields},citations:{...doc.prefill?.citations,...existing.citations},citation_details:{...doc.prefill?.citation_details,...existing.citation_details}};
  $('#approval-check-result').innerHTML='';
  reviewValidationAttempted=false;dirty=false;draft=null;obligationMode=false;started=Date.now();citationDetails=structuredClone(saved.citation_details||{});selectedPayments=saved.payment_ids||[];selectedReliefs=saved.relief_ids||[];
  await tab('review',true);$('#empty-review').classList.add('hidden');$('#review-content').classList.remove('hidden');
  $('#doc-name').textContent=doc.name;$('#doc-meta').textContent=`${labels[doc.processing_state||doc.state]} · 版本 ${doc.version} · ${doc.pages.length} 个原文位置${doc.result?.completion_status?' · '+completionLabels[doc.result.completion_status]:''}${doc.review?.actor?' · 确认人 '+doc.review.actor+' · '+stamp(doc.review.at):''}`;
  $('#governance-status').textContent=`材料处理：${technicalLabels[doc.intake?.technical_status]||'历史资料'} ｜资料完整性：${doc.review?.material_complete?'已核对':'待核对'} ｜台账登记：${doc.state==='published'?'已确认':'未生效'}`;
  $('#confidence-summary').innerHTML=confidenceSummary(doc.confidence,false,doc.state==='published');
  $('#inventory').innerHTML=(doc.intake?.files||[]).map(f=>`<p><b>${esc(f.name)}</b> · ${esc(f.format)} · ${esc(technicalLabels[f.technical_status]||f.technical_status)} · ${f.units} 个原文位置</p>`).join('')+`<details><summary>原件校验与完整位置清单</summary><pre>${esc(JSON.stringify({digest:doc.digest,files:doc.intake?.files,pages:doc.pages.map(p=>({id:p.id,name:p.name,kind:p.kind,location:p.location,lines:p.native_lines?.length,hidden:p.hidden,merged_ranges:p.merged_ranges}))},null,2))}</pre></details>`;
  $('#page-select').innerHTML=unitOptions(doc.pages[0]?.id);
  $('#obligation-termination-month').value='';$('#obligation-termination-source').innerHTML='<option value="">请选择原件</option>'+unitOptions('');
  $('#merge-target').innerHTML='<option value="">选择目标文书</option>'+state.documents.filter(d=>d.id!==doc.id&&!['published','withdrawn','recognizing'].includes(d.state)).map(d=>`<option value="${d.id}">${esc(d.name)} · ${d.pages.length}页</option>`).join('');
  $('#evidence-field').innerHTML=Object.entries(state.fields).map(([k,label])=>`<option value="${k}">${label}</option>`).join('');
  $('#risks').innerHTML=(doc.governance_issues||[]).filter(i=>!state.fields[i.location||i.field]).map(i=>`<div class="risk"><b>${esc(i.category)}</b> · ${esc(i.message)}</div>`).join('');
  $('#risks').insertAdjacentHTML('beforeend',renderSourceIssues());
  $('#show-suggestions').checked=false;
  $('#fields').innerHTML=Object.entries(state.fields).map(([key,originalLabel])=>{
    const label=({lessor:"出租人（甲方）",tenant:"承租人（乙方）",contract_no:"合同编号（选填）",annual:"年租金（元）",start:"租赁开始日期",end:"租赁结束日期"})[key]||originalLabel;
    const candidates=doc.result?.candidates?.[key]||[];
    return `<div class="field" data-field-row="${key}"><div class="field-title"><label for="field-${key}"><span class="field-number"></span>${label}<span class="required-mark" data-required="${key}" aria-hidden="true"></span></label><small data-field-state="${key}"></small></div><div class="candidates hidden"><p class="candidate-note">系统候选不是已核实事实，请先对照原件。</p>${candidates.map((c,i)=>`<button class="candidate" data-key="${key}" data-candidate="${i}">${esc(c.value)}<span>${c.kind==='native_extract'?'原生结构提取（非AI分数）':c.kind==='context_suggestion'?'上下文预计值 · 不能当原文':c.score_meaning==='not_provided'||c.score===null||c.score===undefined?'未提供识别分数':`${Math.round(c.score*100)}%识别分数（非正确概率）`} · ${esc(c.model)}</span><small>原文：${esc(c.raw_text||'未提供原文片段')}<br>模型原值：${esc(c.raw_value??c.value)}${c.canonical_value?`<br>规范编号（比较／检索用）：${esc(c.canonical_value)}；不替代原件`:""}<br>${esc(c.reason)}<br>${esc(c.source_locator||c.source_page_id)}</small></button>`).join('')||'<small>无候选，不能推定事实不存在。</small>'}</div><input id="field-${key}" ${fieldInputAttributes(key,saved.fields?.[key])} data-field="${key}" aria-label="${label}采纳值" value="${esc(saved.fields?.[key]||'')}" placeholder="人工核验后采纳" ${LISTS[key]?`list="${LISTS[key]}"`:''} ${key==='missing_pages'&&doc.hints?.missing_pages?`placeholder="系统提示：可能缺${esc(doc.hints.missing_pages)}，请看原件确认"`:''}><input type="hidden" data-cite="${key}" value="${esc(saved.citations?.[key]||'')}"><div data-reference-list="${key}" class="references"></div></div>`;
  }).join('');
  const modeInput=$('[data-field="mode"]'),modeSelect=document.createElement('select');
  modeSelect.id=modeInput.id;modeSelect.dataset.field='mode';modeSelect.setAttribute('aria-label','计租方式');
  const modeValue=saved.fields?.mode||'';
  modeSelect.innerHTML=['','固定','随标准调整','未勾选',...(!['','固定','随标准调整','未勾选'].includes(modeValue)?[modeValue]:[])].map(v=>`<option value="${esc(v)}" ${v===modeValue?'selected':''}>${esc(v||'请选择计租方式')}</option>`).join('');modeInput.replaceWith(modeSelect);
  $('[data-cite="mode"]').hidden=true;$('[data-cite="mode"]').setAttribute('aria-hidden','true');
  const lessorInput=$('[data-field="lessor"]');lessorInput.setAttribute('list','lessor-options');
  let lessors=document.getElementById('lessor-options');if(!lessors){lessors=document.createElement('datalist');lessors.id='lessor-options';document.body.append(lessors)}
  lessors.innerHTML=[...new Set([...(doc.result?.candidates?.lessor||[]).map(c=>c.value),...state.documents.map(d=>d.review?.fields?.lessor)].filter(Boolean))].map(v=>`<option value="${esc(v)}"></option>`).join('');
  groupFields();
  document.querySelectorAll('.field-number').forEach((el,i)=>el.textContent=`${i+1}. `);
  renderWithdrawalReview();
  document.querySelectorAll('[data-field]').forEach(e=>{e.oninput=()=>{linkCurrentOriginal(e.dataset.field);if(['altered_field','altered_sealed'].includes(e.dataset.field))syncAlterationControls();invalidate();updateFieldStates()};e.onfocus=()=>selectField(e.dataset.field)});
  document.querySelectorAll('[data-cite]').forEach(e=>e.onchange=()=>{invalidate();if(e.value){selectField(e.dataset.cite,false);showPage(e.value);updateFieldStates()}});
  document.querySelectorAll('[data-candidate]').forEach(b=>b.onclick=()=>{
    const key=b.dataset.key,c=doc.result.candidates[key][+b.dataset.candidate];const input=$(`[data-field="${key}"]`);if((input.type==='date'&&!/^\d{4}-\d{2}-\d{2}$/.test(c.value))||(input.type==='number'&&!/^-?\d+(\.\d+)?$/.test(c.value)))input.type='text';input.value=c.value;$(`[data-cite="${key}"]`).value=c.source_page_id;
    if(c.source_locator)for(const locator of c.source_locator.split(' + '))addReference(key,c.source_page_id,locator);else addReference(key,c.source_page_id,'');
    selectField(key,false);showPage(c.source_page_id,c.bbox,c.source_locator?.split(' + ')[0]);invalidate();
  });
  renderConfidenceFields();
  renderFieldIssues();
  renderContractNavigation();
  renderAlterationAssistance(saved);
  Object.keys(state.fields).forEach(renderReferences);
  $('#reason').value=saved.reason||'';$('#uncertainty').value=saved.uncertainty||'';$('#stop-date').value=saved.stop||'';
  for(const id of ['registration-clear','material-clear','evidence-clear','payments-clear','calculation-clear'])$('#'+id).checked=false;
  $('#matching').innerHTML='';delete $('#matching').dataset.doc;$('#calculation').innerHTML='';$('#calculation-status').hidden=true;$('#confirm').disabled=true;
  const blocking=(doc.governance_issues||[]).filter(i=>i.severity==='blocking');
  $('#quality-resolutions').innerHTML=blocking.length?`<p><b>登记前需解决的问题</b></p>${blocking.map(i=>{const r=saved.quality_resolutions?.[i.id]||{};return `<div class="risk" data-quality="${i.id}"><b>${esc(locationLabel(i.location))} · ${esc(i.message)}</b><select data-decision aria-label="${esc(locationLabel(i.location))}处理结论"><option value="">未解决</option><option value="verified_with_evidence" ${r.decision==='verified_with_evidence'?'selected':''}>已有可核实证据</option><option value="excluded_with_reason" ${r.decision==='excluded_with_reason'?'selected':''}>有依据排除该内容</option></select><input data-resolution-reason aria-label="问题处理依据" placeholder="不能靠猜测；说明核对或排除依据" value="${esc(r.reason||'')}"><select data-resolution-unit aria-label="问题处理证据位置"><option value="">选择原文位置</option>${unitOptions(r.source_unit)}</select></div>`}).join('')}`:'';
  for(const issue of blocking){
    const key=issue.location||issue.field,row=$(`[data-field-row="${key}"]`),control=$(`[data-quality="${issue.id}"]`);
    if(row&&control){const existing=row.querySelector('.field-issues');if(existing)existing.remove();row.append(control)}
  }
  $('#review-issues').hidden=!$('#quality-resolutions').textContent.trim();
  $('#audit').textContent=doc.audit.map(x=>`${new Date(x.at*1000).toLocaleString()} · ${x.actor} · ${x.action}\n${x.payload}`).join('\n\n');
  $('#resume-review').hidden=doc.state!=='held';$('#retry-unknown').hidden=!doc.result?.unknown_attempts?.length;
  const locked=['published','withdrawn'].includes(doc.state);$('#retry').disabled=locked;$('#attach-form button').disabled=locked;$('#save-draft').disabled=locked;document.querySelectorAll('[data-field]').forEach(e=>e.readOnly=locked);document.querySelectorAll('[data-cite],[data-candidate],[data-decision],[data-resolution-reason],[data-resolution-unit]').forEach(e=>e.disabled=locked);
  if(doc.obligation_matches)renderMatches(doc.obligation_matches);
  showPage(doc.pages[0]?.id);selectField(Object.keys(state.fields)[0],false);updateFieldStates();applyPermissions();updateRequirements();$('#document-update').hidden=true;renderApprovalState();return true;
}
const confidenceLabels={high:'高',medium:'中',low:'低'};
function confidenceText(score){return `${esc(score)}% · ${Number(score)<60?'待人工核验':'待人工确认'}`}
function confidenceSummary(c,compact=false,historical=false){
  if(!c||c.score==null)return '<small>暂无评分 · 待人工核验</small>';
  return `<section class="confidence-summary" aria-label="综合置信度"><b>${confidenceText(c.score)}</b></section>`;
}
function renderConfidenceFields(){
  for(const [key,info] of Object.entries(doc.confidence?.fields||{})){
    const row=$(`[data-field-row="${key}"]`);if(!row)continue;
    const box=document.createElement('small');box.className='field-confidence';
    box.textContent=info.score==null?'暂无评分 · 待人工核验':`${info.score}% · ${Number(info.score)<60?'待人工核验':'待人工确认'}`;
    row.append(box);
  }
}
function requiredFieldKeys(){
  const value=key=>$(`[data-field="${key}"]`)?.value.trim();
  const keys=new Set(value('doc_type')==='变更'?(state.change_required_fields||['tenant','parcel_no']):(state.required_fields||['tenant','parcel_no','address','area','use','start','end','mode']));
  if(value('doc_type')!=='变更'&&['固定','未勾选'].includes(value('mode')))keys.add('annual');
  if(value('altered_field'))keys.add('altered_sealed');
  return keys;
}
function syncReviewError(input,invalid,message){
  const id=input.id+'-error';
  let hint=document.getElementById(id);
  if(invalid&&!hint){hint=document.createElement('small');hint.id=id;hint.className='review-field-error';input.after(hint)}
  if(hint){hint.textContent=invalid?message:'';hint.hidden=!invalid}
  if(invalid)input.setAttribute('aria-invalid','true');else input.removeAttribute('aria-invalid');
  const descriptions=new Set((input.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean));
  if(invalid)descriptions.add(id);else descriptions.delete(id);
  if(descriptions.size)input.setAttribute('aria-describedby',[...descriptions].join(' '));else input.removeAttribute('aria-describedby');
  const trigger=input.parentElement.querySelector(`[data-select-for="${input.id}"] .select-trigger`);
  if(trigger){
    if(invalid){trigger.setAttribute('aria-invalid','true');trigger.setAttribute('aria-describedby',[...descriptions].join(' '))}
    else{trigger.removeAttribute('aria-invalid');trigger.removeAttribute('aria-describedby')}
  }
}
function syncRequiredFields(){
  const keys=requiredFieldKeys(),missing=[];
  document.querySelectorAll('[data-field]').forEach(input=>{
    const key=input.dataset.field,required=keys.has(key);input.required=required;input.setAttribute('aria-required',String(required));
    const mark=$(`[data-required="${key}"]`);if(mark)mark.textContent=required?' *':'';
    const empty=required&&!input.value.trim();
    if(empty)missing.push(state.fields[key]);
    syncReviewError(input,reviewValidationAttempted&&empty,'请填写'+(state.fields[key]||key));
  });
  renderApprovalState();
  return missing;
}
function linkCurrentOriginal(key){
  const citation=$(`[data-cite="${key}"]`);if(!citation)return;
  const unit=doc.pages.find(p=>p.id===$('#page-select').value);
  // Manual edits refer to the original currently visible beside the form.
  if(unit&&citation.value!==unit.id){citation.value=unit.id;if(unit.kind!=='native')addReference(key,unit.id,'')}
}
function renderFieldIssues(){
  for(const issue of doc.governance_issues||[]){
    const key=issue.location||issue.field;
    const row=$(`[data-field-row="${key}"]`);
    if(!row)continue;
    let box=row.querySelector('.field-issues');
    if(!box){box=document.createElement('div');box.className='field-issues';row.append(box)}
    const message=document.createElement('p');message.className='risk';message.textContent=`${issue.category||'识别问题'}：${issue.message}`;box.append(message);
  }
}
function renderContractNavigation(){
  const docs=state.documents||[],index=docs.findIndex(d=>d.id===doc.id);
  for(const [id,delta] of [['review-prev',-1],['review-next',1]]){
    const target=docs[index+delta],button=$('#'+id);button.disabled=!target;
    button.onclick=()=>action(async()=>{if(target)await openDoc(target.id)});
  }
}
$('#review-back').onclick=()=>action(()=>tab('inbox'));
function addReference(key,unit_id,locator){const rows=citationDetails[key]||(citationDetails[key]=[]);if(!rows.some(r=>r.unit_id===unit_id&&r.locator===locator))rows.push({unit_id,locator});renderReferences(key)}
function renderReferences(key){const target=$(`[data-reference-list="${key}"]`);if(!target)return;target.innerHTML=(citationDetails[key]||[]).map((r,i)=>`<span>${esc(doc.pages.find(p=>p.id===r.unit_id)?.name||r.unit_id)} · ${esc(r.locator||'图像页')} <button data-remove-ref="${i}" aria-label="移除引用">×</button></span>`).join('');target.querySelectorAll('[data-remove-ref]').forEach(b=>b.onclick=()=>{citationDetails[key].splice(+b.dataset.removeRef,1);renderReferences(key);invalidate();updateFieldStates();applyPermissions()})}
function renderTranscription(id){
  const page=doc.transcription_pages?.find(p=>p.id===id);
  const versions=page?.versions||[];
  const names={primary:'主模型转写',secondary:'备用模型转写',native:'原生文字提取',text_revision:'模型文字复核稿（基于首遍OCR）'};
  $('#transcription-versions').innerHTML=versions.length?versions.map(v=>`<section><h3>${esc(v.stage==='text_revision'&&v.input_source==='original_images_and_ocr'?'模型文字复核稿（原图＋OCR全文）':names[v.stage]||v.stage)} · ${esc(v.model||'未知模型')}</h3><p>${v.coverage==='region'?'仅当前局部区域':v.coverage==='legacy_scope_unknown'?'历史转写，覆盖范围未记录':'本页文字'} · 尚未人工核验</p><pre class="transcription-text">${esc(v.text)}</pre></section>`).join(''):'<p>本页暂无完整文字转写。历史字段片段不等于全文，可重新识别后查看。</p>';
  const comparison=doc.result?.text_comparison;
  if(comparison){
    const row=comparison.pages.find(p=>p.source_page_id===id);
    const labels={identical:'两遍文字相同',changed:'两遍文字有改动',missing_revision:'缺少模型逐页复核稿',shared_quote:'两遍保留该原文片段',changed_quote:'复核稿改动了引用片段',unsupported_quote:'引用片段未在首遍OCR中找到'};
    const fields=Object.entries(comparison.fields).flatMap(([key,rows])=>rows.filter(r=>r.source_page_id===id).map(r=>`<li>${esc(state.fields[key]||key)}：${esc(r.value)} · ${esc(labels[r.status]||r.status)}；仍需人工核验</li>`)).join('');
    $('#transcription-versions').insertAdjacentHTML('beforeend',`<section><h3>两遍文字比对与字段证据</h3><p>${esc(comparison.basis)}</p><p>${esc(labels[row?.status]||'暂无比对结果')}；未估计识别正确概率。</p>${(row?.changes||[]).map(c=>`<details><summary>文字改动：首遍第${c.original_lines[0]}行附近</summary><p>首遍 OCR</p><pre class="transcription-text">${esc(c.original)}</pre><p>模型复核稿</p><pre class="transcription-text">${esc(c.revised)}</pre></details>`).join('')}<ul>${fields}</ul></section>`);
  }
  $('#download-transcription').href=`/api/documents/${encodeURIComponent(doc.id)}/transcription.txt`;
}
function showPage(id,box,locator){
  renderTranscription(id);
  const unit=doc.pages.find(p=>p.id===id);if(!unit)return;
  $('#evidence-live').textContent=`当前原件：${unit.name}${locator?' · '+locator:''}；核对字段：${state.fields[$('#evidence-field').value]||'尚未选择'}`;
  $('#page-select').value=id;$('#download-original').href=`/api/original/${doc.id}/${id}`;
  $('#rotate').disabled=!unit.path;$('#previous').disabled=!unit.path;
  $('#image-wrap').classList.toggle('hidden',!unit.path);$('#overlay').innerHTML='';
  if(unit.path){$('#original').onload=()=>{const im=$('#original');$('#overlay').setAttribute('viewBox',`0 0 ${im.naturalWidth} ${im.naturalHeight}`);if(box)$('#overlay').innerHTML=`<rect x="${box[0]}" y="${box[1]}" width="${box[2]-box[0]}" height="${box[3]-box[1]}" fill="#efb52c33" stroke="#e69712" stroke-width="5"/>`};$('#original').alt=`${doc.name} · ${unit.name}`;$('#original').src=`/api/pages/${doc.id}/${id}?v=${doc.version}`}
  const lines=unit.native_lines||[];$('#native-evidence').classList.toggle('hidden',!lines.length&&!(!unit.path));
  $('#native-evidence').innerHTML=lines.length?`<h3>${esc(unit.name)} · 原文内容</h3><p class="muted">${unit.kind==='native'?'未模拟Word分页；按真实段落/单元格定位。':'PDF文字层与原图并存，请同时核验。'}</p><table><thead><tr><th>原件位置</th><th>原值与显示解释</th><th></th></tr></thead><tbody>${lines.map((l,i)=>`<tr class="${l.locator===locator?'evidence-selected':''}"><td>${esc(l.locator)}${l.hidden?' · 隐藏':''}</td><td><b>${esc(l.raw_text)}</b>${l.display_value!==undefined?`<br><small>显示解释：${esc(l.display_value)} · 格式 ${esc(l.number_format)}<br>XML原值：${esc(l.raw_xml_value)}${l.formula!==null&&l.formula!==undefined?`<br>公式：${esc(l.formula)} · 缓存：${esc(l.cached_value)}`:''}</small>`:''}${l.deleted_text?`<br>删除修订：${esc(l.deleted_text)}`:''}</td><td><button data-native-ref="${i}">引用</button></td></tr>`).join('')}</tbody></table>`:'<div class="error">该原件未能解析。可下载核查并补充受支持格式，不能标为已完成。</div>';
  document.querySelectorAll('[data-native-ref]').forEach(b=>b.onclick=()=>{const key=$('#evidence-field').value;addReference(key,id,lines[+b.dataset.nativeRef].locator);$(`[data-cite="${key}"]`).value=id;SelectControls.enhance();invalidate();updateFieldStates();$('#evidence-live').textContent=`已关联：${state.fields[key]}，${unit.name}，${lines[+b.dataset.nativeRef].locator}。尚未采纳值。`;toast('已关联原文位置，尚未采纳任何值')});
  SelectControls.enhance();
  $('#native-evidence .evidence-selected')?.scrollIntoView({block:'nearest'});applyPermissions();
}
$('#show-suggestions').onchange=()=>selectField($('#evidence-field').value,false);
$('#evidence-field').onchange=()=>selectField($('#evidence-field').value);
$('#page-select').onchange=()=>showPage($('#page-select').value);$('#reload-doc').onclick=()=>action(()=>openDoc(doc.id));
$('#retry').onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;await api(`/api/documents/${doc.id}/retry`,{version:doc.version,retry_unknown:false});await openDoc(doc.id,true);toast('已创建恢复任务：复用成功证据，结果未知的请求不会重发')});
$('#retry-unknown').onclick=()=>action(async()=>{
  if(await guardUnsaved()==='cancel')return;
  const answer=await requestDialog({title:'重发结果未知的请求',description:'这些请求可能已由供应商完成并计费。重发会产生新的调用；已成功保存的页面仍会复用。',fields:[{name:'reason',label:'重发依据',type:'textarea',required:true}],submit:'确认重发未知请求',danger:true});
  if(!answer)return;
  await api(`/api/documents/${doc.id}/retry`,{version:doc.version,retry_unknown:true,reason:answer.reason});await openDoc(doc.id,true);toast('已记录重发依据并创建恢复任务');
});
$('#resume-review').onclick=()=>action(async()=>{
  if(await guardUnsaved()==='cancel')return;
  const answer=await requestDialog({title:'继续人工核验',description:'保留原件和已保存草稿，返回待核对状态。请重新核对依据并生成租金预览。',fields:[{name:'reason',label:'继续核验的依据',type:'textarea',required:true}],submit:'继续核验'});
  if(!answer)return;
  await api(`/api/documents/${doc.id}/resume-review`,{version:doc.version,reason:answer.reason});await refresh();await openDoc(doc.id,true);toast('已返回待核对，草稿保留');
});
for(const [selector,operation] of [['#rotate','rotate'],['#previous','previous']])$(selector).onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;if(!await confirmAction('调整原件页面会使旧确认和核算预览失效，需要重新核对。继续吗？'))return;await api(`/api/documents/${doc.id}/page`,{version:doc.version,page_id:$('#page-select').value,action:operation});await openDoc(doc.id,true)});
$('#attach-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!$('#attach-files').files.length)throw Error('先选择补充材料');if(await guardUnsaved()==='cancel')return;if(!await confirmAction('补件会使旧草稿、确认和核算预览失效，原件仍会保留。继续上传吗？'))return;const data=new FormData();data.append('version',doc.version);for(const f of $('#attach-files').files)data.append('files',f);const r=await api(`/api/documents/${doc.id}/attach`,data);await openDoc(doc.id,true);toast(r.duplicate?'补件内容重复，无新增贡献':'补件已保存；旧确认、旧草稿和核算预览已失效')})};
$('#merge-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(await guardUnsaved()==='cancel')return;const id=$('#merge-target').value;if(!id)throw Error('先选择目标文书');const target=state.documents.find(d=>d.id===id);if(!await confirmAction(`将“${doc.name}”（版本 ${doc.version}）并入“${target.name}”（版本 ${target.version}）。源文书将撤回，目标草稿、确认与预览失效，原件和历史保留。`,'确认合并文书'))return;await api(`/api/documents/${doc.id}/merge-into`,{version:doc.version,target:id,target_version:target.version,reason:'补档分组修正'});await refresh();await openDoc(id,true);toast('已并入目标文书，目标需重新识别与确认')})};
function body(){
  const fields={},citations={},quality_resolutions={};document.querySelectorAll('[data-field]').forEach(e=>fields[e.dataset.field]=e.value.trim());document.querySelectorAll('[data-cite]').forEach(e=>citations[e.dataset.cite]=e.value);
  document.querySelectorAll('[data-quality]').forEach(e=>quality_resolutions[e.dataset.quality]={decision:e.querySelector('[data-decision]').value,reason:e.querySelector('[data-resolution-reason]').value,source_unit:e.querySelector('[data-resolution-unit]').value});
  return {version:doc.version,fields,citations,alteration_review:alterationReview(),citation_details:citationDetails,payment_ids:selectedPayments,relief_ids:selectedReliefs,quality_resolutions,reason:$('#reason').value,uncertainty:$('#uncertainty').value,stop:$('#stop-date').value,material_complete:$('#material-clear').checked,evidence_clear:$('#evidence-clear').checked,confirm_payments:$('#payments-clear').checked,confirm_calculation:$('#calculation-clear').checked,unresolved_fields:doc.draft?.unresolved_fields||[],review_elapsed_seconds:Math.round((Date.now()-started)/1000),draft_hash:draft?.draft_hash};
}
$('#save-draft').onclick=()=>action(saveDraft);
$('#obligation-preview').onclick=()=>action(async()=>{
  const requestDoc=doc.id,version=doc.version,signature=obligationSignature(),epoch=++obligationEpoch;
  obligationMode=true;obligationPreview=null;$('#obligation-result').innerHTML='';$('#obligation-state').textContent='正在试算本期应缴…';
  draft=null;$('#calculation').innerHTML='';$('#calculation-status').hidden=true;$('#registration-clear').checked=false;$('#calculation-clear').checked=false;
  const payload={...body(),obligation_evidence_clear:$('#obligation-evidence-clear').checked,obligation_reliefs_reviewed:$('#obligation-reliefs-clear').checked};
  if($('#obligation-termination-month').value){payload.termination_month=$('#obligation-termination-month').value;payload.termination_month_source_reviewed=payload.obligation_evidence_clear;payload.citations.termination_month=$('#obligation-termination-source').value;}
  try{
    const result=await api(`/api/documents/${requestDoc}/obligation-preview`,payload);
    const latest=await api(`/api/documents/${requestDoc}`);
    if(!doc||doc.id!==requestDoc||doc.version!==version||latest.version!==version||epoch!==obligationEpoch||signature!==obligationSignature()){
      if(doc?.id===requestDoc&&epoch===obligationEpoch)invalidateObligation('材料或输入已更新，旧试算已失效。请重新加载后核对。');
      return;
    }
    obligationPreview=result;
    $('#obligation-state').textContent='只读试算已完成；合同效力与付款归属仍待核验，无登记、通知或导出资格。';
    $('#obligation-result').innerHTML=renderObligation(result);
  }catch(error){if(doc?.id===requestDoc&&epoch===obligationEpoch){$('#obligation-state').textContent='无法试算：'+error.message;$('#obligation-result').innerHTML='';}throw error;}
});
function renderObligation(result){
  const annual=(result.annual||[]).map(r=>({...r,paid:'未核实',arrears:'未核实'}));
  return `<div class="calc"><p>本合同载明期间 · ${esc(result.start)} 至 ${esc(result.end)}${result.termination?` · 收回月份 ${esc(result.termination.month)}（具体日未知）`:''}</p><dl class="amounts"><div><dt>本期应缴试算（元）</dt><dd>${esc(result.due)}</dd></div><div><dt>实缴</dt><dd>未核实</dd></div><div><dt>欠缴</dt><dd>未核实</dd></div></dl><p class="notice">仅在已核对的公式输入下成立，不代表合同有效、宗地累计欠缴或核验办结。${result.unresolved_items_retained?.length?'已有未决事项继续保留，请查看合同待办。':''}</p><details><summary>查看本期逐年应缴试算</summary>${businessTable('本期应缴试算（只读）',[['year','年度'],['months','计租月数'],['due','应缴试算（元）'],['paid','实缴'],['arrears','欠缴']],annual)}</details></div>`;
}
for(const id of ['obligation-evidence-clear','obligation-reliefs-clear'])$('#'+id).onchange=()=>invalidateObligation('核对选项已变更，请重新试算。',false);
for(const id of ['obligation-termination-month','obligation-termination-source'])$('#'+id).onchange=()=>invalidateObligation();

$('#preview').onclick=()=>action(async()=>{obligationMode=false;invalidateObligation('已切换到完整核算预览，本期试算已清除。');$('#calculation').innerHTML='';$('#calculation-status').hidden=true;draft=null;$('#registration-clear').checked=false;$('#calculation-clear').checked=false;updateRequirements();const r=await api(`/api/documents/${doc.id}/preview`,body());renderMatches(r.matches);if(r.errors.length){draft=null;$('#calculation').innerHTML=`<div class="error">${r.errors.map(esc).join('<br>')}</div>`;$('#confirm').disabled=true}else{draft=r;$('#calculation').innerHTML=renderCalc(r.calculation,r.previous);updateRequirements();toast('租金预览已生成，请核对后确认登记')}});
function renderMatches(m){
  if(!$('#matching').dataset.doc&&!doc.draft&&!doc.review){selectedPayments=m.payments.filter(p=>p.preselect).map(p=>String(p['序号']));selectedReliefs=m.reliefs.map(r=>r['批准文号']);}
  $('#matching').dataset.doc=doc.id;

  $('#matching').innerHTML=`<h3>核对登记、缴费与减免资料</h3><p class="muted">${esc(m.warning||'多候选需人工核对')} · 登记来源：02 不动产登记导出；匹配依据：${esc(m.registry_basis||'未取得匹配依据')}</p><details><summary>查看匹配的登记资料</summary>${businessTable('登记资料（接口匹配结果）',[['label','字段'],['value','内容']],(Array.isArray(m.registry)?m.registry:[m.registry]).filter(Boolean).flatMap((row,i)=>Object.entries(row).map(([label,value])=>({label:label,value}))))}</details><div class="payments">${m.payments.map(p=>`<label><input type="checkbox" data-payment="${esc(p['序号'])}" ${selectedPayments.includes(String(p['序号']))?'checked':''}>票${esc(p['票据号'])} · ${esc(p['缴费日期'])} · ${esc(p['金额（元）'])}元 · ${esc(p['所属年度']||'未注明年度')} · ${esc(p['缴款人'])}<small> · ${p.match_strength==='weak'?'弱依据，须逐项核对':'匹配依据'}：${esc(p.match_basis||'请人工核对归属')}</small></label>`).join('')||'没有精确匹配的缴费候选；仍需核对原表，不能据此断言无缴费。'}</div>${m.reliefs.map(r=>`<label><input type="checkbox" data-relief="${esc(r['批准文号'])}" ${selectedReliefs.includes(r['批准文号'])?'checked':''}>${esc(r['批准文号'])} · 减免 ${esc(r['减免比例'])} · ${esc(r['减免起始年度'])} 至 ${esc(r['减免终止年度'])} 年</label>`).join('')}`;
  document.querySelectorAll('[data-payment],[data-relief]').forEach(e=>e.onchange=()=>{selectedPayments=[...document.querySelectorAll('[data-payment]:checked')].map(e=>e.dataset.payment);selectedReliefs=[...document.querySelectorAll('[data-relief]:checked')].map(e=>e.dataset.relief);invalidate()});
}
const numericColumns=new Set(['due','paid','arrears','amount','annual','prorated','rate','area','old','current']);
function businessTable(title,columns,rows,widths){
 const cellClass=key=>numericColumns.has(key)?'numeric':'';
 return `<h3>${esc(title)}</h3><div class="table-scroll" role="region" tabindex="0" aria-label="${esc(title)}"><table${widths?' class="calculation-table"':''}><caption class="sr-only">${esc(title)}</caption>${widths?`<colgroup>${widths.map(width=>`<col style="width:${width}%">`).join('')}</colgroup>`:''}<thead><tr>${columns.map(([key,label])=>`<th scope="col" class="${cellClass(key)}">${esc(label)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${columns.map(([key])=>`<td class="${cellClass(key)}">${esc(row[key]??'—')}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${columns.length}">暂无记录，请核对资料覆盖范围。</td></tr>`}</tbody></table></div>`;
}
function renderCalc(p,previous){
 const old=previous?.payload;
 return `<div class="calc"><p>核算截至 ${esc(p.as_of)} · ${esc(p.ruleset)}</p><dl class="amounts">${[['due','应缴租金'],['paid','已缴租金'],['arrears','欠缴租金'],['overpaid','多缴金额']].map(([key,label])=>`<div class="${key==='arrears'?'amount-total':''}"><dt>${label}</dt><dd>¥ ${esc(p[key]??'—')}</dd></div>`).join('')}</dl>${Number(p.arrears)<0?'<p class="notice">总欠缴为负数，表示净多缴／抵减，不应据此催缴。具体归集见缴费明细。</p>':''}<p>催缴对象：${esc(p.recipient)} · ${esc(p.phone||'电话待核实')}</p>
 <details><summary>逐年核算</summary>${businessTable('逐年核算',[['year','年度'],['due','应缴'],['paid','已归集'],['arrears','欠缴']],p.annual||[],[22,26,26,26])}</details>
 <p class="muted">年度欠缴与总欠缴可能因多缴款处理不同而不同；所有金额均采用服务端核算结果。</p>
 <details><summary>分段计租依据</summary>${businessTable('计租明细',[['year','年度'],['months','计租月数'],['area','面积'],['rate','费率'],['annual','全年租金'],['prorated','分段租金'],['source','依据']],p.segments||[],[10,12,13,12,16,16,21])}</details>
 <details><summary>缴费归集</summary>${businessTable('缴费归集',[['receipt','票据号'],['year','归属年度'],['amount','归集金额']],(p.allocations||[]).map(x=>({...x,year:x.year??(x.type==='多缴'?'多缴／未归入年度':'未注明年度')})),[40,30,30])}</details>
 ${old?businessTable('版本对照',[['label','项目'],['old','上版'],['current','本版']],[['due','应缴'],['paid','已缴'],['arrears','欠缴']].filter(([k])=>old[k]!=null&&p[k]!=null).map(([k,label])=>({label,old:old[k],current:p[k]})),[40,30,30]):'<p>无上一版核算数据，无法比较历史差异。</p>'}
 ${(p.scenarios||[]).map(x=>`<p class="risk">${esc(x.what)}：暂按 ${esc(x.chosen)}，差额 ${esc(x.difference)} 元，需核对依据。</p>`).join('')}
 <h3>核算提示</h3>${(p.issues||[]).map(x=>`<p class="risk">${esc(x.type||'待核对')}：${esc(x.reason||x.message||'请展开审计数据核查原始信息')}</p>`).join('')||'<p>未发现核算提示，仍须核对原件和核算依据。</p>'}<p>${esc(p.coverage)}</p><details><summary>审计数据</summary><pre>${esc(JSON.stringify(p,null,2))}</pre></details></div>`;
}
$('#confirm').onclick=()=>{
  if(operationBusy||!doc)return;
  reviewValidationAttempted=true;
  const missing=updateRequirements();
  if($('#confirm').disabled)return;
  if(missing.length){
    const invalid=$('#review-content [required][aria-invalid="true"]');
    const first=$('#remaining-items [data-jump]');
    toast(invalid?document.getElementById(invalid.id+'-error').textContent:missing[0]);
    if(invalid)jumpToReviewTarget(invalid.id);else if(first)jumpToReviewTarget(first.dataset.jump);
    return;
  }
  return action(async()=>{if(!draft)throw Error('请先重新预览');if(!await requestDialog({title:'确认并登记台账',description:`${doc.name} · 版本 ${doc.version}。将按已核对的原件、缴费选择及当前核算结果生成不可变台账快照。此操作是经办人确认登记。`,submit:'确认登记',danger:true}))return;await api(`/api/documents/${doc.id}/confirm`,body());dirty=false;draft=null;toast('已登记台账，核对记录已保存');await refresh();await openDoc(doc.id,true)});
};
$('#hold').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/draft`,body());await api(`/api/documents/${doc.id}/hold`,{version:doc.version,reason:$('#reason').value});dirty=false;await refresh();await openDoc(doc.id,true);toast('已转为待补件，尚未登记台账')});
$('#withdraw').onclick=()=>action(async()=>{const reason=$('#withdraw-reason').value.trim();if(!reason)throw Error('请填写撤回原因说明');await api(`/api/documents/${doc.id}/withdraw-request`,{version:doc.version,reason});await openDoc(doc.id,true);toast('已提交管理员审核，当前登记仍有效')});
function renderWithdrawalReview(){
  $('#withdraw-reason').value='';const rows=doc.withdrawal_requests||[],pending=rows.find(r=>r.status==='pending');
  $('#withdraw-review').innerHTML=rows.map(r=>`<p>申请人：${esc(r.actor)} · ${esc(stamp(r.created))}<br>撤回原因：${esc(r.reason)}<br>审核状态：${esc(({pending:'待管理员审核',approve:'已通过',reject:'已驳回'})[r.status])}${r.review_reason?` · ${esc(r.review_reason)}`:''}</p>`).join('')+(pending&&$('#role').value==='admin'?'<label>审核意见<textarea id="withdraw-review-reason" maxlength="2000"></textarea></label><button data-withdraw-decision="approve">审核通过并撤回</button><button data-withdraw-decision="reject">驳回申请</button>':'');
  document.querySelectorAll('[data-withdraw-decision]').forEach(b=>b.onclick=()=>action(async()=>{const reason=$('#withdraw-review-reason').value.trim();if(!reason)throw Error('请填写审核意见');await api(`/api/documents/${doc.id}/withdraw`,{version:doc.version,request_id:pending.id,decision:b.dataset.withdrawDecision,reason});await refresh();await openDoc(doc.id,true);toast('审核结果已保存')}));
}

async function loadLedger(){
 $('#ledger-content').textContent='正在读取当前有效快照…';$('#notice-preview').innerHTML='';
 const q=new URLSearchParams({street:$('#f-street').value,status:$('#f-status').value,arrears:$('#f-arrears').value,keyword:$('#f-keyword').value});
 const matched=await api('/api/ledger?'+q);ledgerPage=Math.max(1,Math.min(ledgerPage,Math.ceil(matched.length/20)||1));const rows=matched.slice((ledgerPage-1)*20,ledgerPage*20);$('#ledger-pagination').innerHTML=`<span>共 ${matched.length} 宗 · 第 ${ledgerPage} 页</span><button id="ledger-prev" ${ledgerPage<=1?'disabled':''}>上一页</button><button id="ledger-next" ${ledgerPage*20>=matched.length?'disabled':''}>下一页</button>`;$('#ledger-prev').onclick=()=>action(async()=>{ledgerPage--;await loadBusinessPage('ledger')});$('#ledger-next').onclick=()=>action(async()=>{ledgerPage++;await loadBusinessPage('ledger')});
 $('#ledger-content').innerHTML=rows.length?rows.map(r=>`<div class="card" data-parcel="${esc(r.parcel_id)}" tabindex="-1"><div class="section-head"><h2>${esc(r.payload.address||'')} · ${esc(r.payload.recipient||'')}</h2><span class="tag">${esc(r.payload.ledger?.合同状态||'')} · 版本 ${r.id}</span></div>${renderParcelSummary(r.payload)}${renderCalc(r.payload)}<div class="actions"><button data-notice="${r.id}" data-no-arrears="${!(Number(r.payload.arrears)>0)}" ${Number(r.payload.arrears)>0?'':'disabled'}>生成催缴通知书与短信（不发送）</button><button data-contract="${esc(r.parcel_id)}">生成续期合同草稿</button></div></div>`).join(''):'<div class="empty">没有符合条件的台账。</div>';
 document.querySelectorAll('[data-notice]').forEach(b=>b.onclick=()=>action(async()=>{const n=await api('/api/notices/'+b.dataset.notice,{});await tab('notices');$('#notice-preview').innerHTML=`<div class="card"><h2>催缴通知草稿 · 待审核</h2><p>短信（${n.sms.length}字，${n.sms.segments}条计费）：${esc(n.sms_draft)}</p>${n.phone_valid?'':'<div class="error">联系电话缺失或位数不对，需补充后再发送</div>'}${n.settings_configured?'':'<div class="error">缴费地点/经办电话未在 config/notice.json 配置</div>'}<a class="button" href="/api/notices/${n.id}/document">下载通知书 DOCX</a></div>`;await loadNotices()}));
 document.querySelectorAll('[data-contract]').forEach(b=>b.onclick=()=>action(()=>createContract(b.dataset.contract)));
 SelectControls.enhance();
}
async function loadNotices(){
 const rows=await api('/api/notices');
 $('#notice-list').innerHTML=rows.length?`<table><thead><tr><th>对象</th><th>欠缴</th><th>状态</th><th>登记结果</th></tr></thead><tbody>${rows.map(n=>`<tr><td>${esc(n.recipient)}<br><small>${esc(n.phone||'无电话')}</small></td><td>${esc(n.arrears)}</td><td>${esc(n.status)}<br><button data-send-notice="${n.id}">模拟发送</button><button data-notice-tasks="${n.id}">任务与回执</button><button data-sim-receipt="${n.id}">补录模拟回执</button></td><td><select data-notice-status="${n.id}"><option value="">登记…</option>${['已审核','号码无效','有异议','作废'].map(x=>`<option>${x}</option>`).join('')}</select> <a href="/api/notices/${n.id}/document">通知书</a></td></tr>`).join('')}</tbody></table>`:'<p class="muted">尚未生成催缴通知。</p>';
 bindNoticeSend();SelectControls.enhance();
 document.querySelectorAll('[data-notice-status]').forEach(e=>e.onchange=()=>action(async()=>{if(!e.value)return;const status=e.value,context=e.closest('tr').cells[0].textContent;const result=await requestDialog({title:'登记催缴处理结果',description:`${context} · ${status}。此操作只记录办理结果，不会实际发送通知。`,fields:[{name:'note',label:'处理意见 / 依据',type:'textarea',required:true}],submit:'登记结果'});e.value='';SelectControls.enhance();if(!result)return;await api(`/api/notices/${e.dataset.noticeStatus}/events`,{status,note:result.note});toast('已登记：'+status);await loadNotices()}));
}
async function loadTaxPushes(){
 const rows=await api('/api/tax-pushes');
 $('#tax-list').innerHTML=rows.map(r=>`<p>${esc(r.period)} · ${r.rows} 行 · ${new Date(r.created*1000).toLocaleString()} · ${esc(r.actor)} · <a href="/api/tax-push/${r.id}/document">下载</a></p>`).join('')||'<p class="muted">尚无推送记录。</p>';
}
$('#ledger-filter').onsubmit=e=>{e.preventDefault();ledgerPage=1;action(()=>loadBusinessPage('ledger'))};
$('#tax-push').onclick=()=>action(async()=>{const params={year:+$('#tax-year').value,quarter:+$('#tax-quarter').value};const preview=await api('/api/tax-preview',params);$('#tax-list').insertAdjacentHTML('afterbegin',businessTable('待确认推送内容',preview.columns.map((c,i)=>[String(i),c]),preview.data.map(row=>Object.fromEntries(row.map((v,i)=>[String(i),v]))))); if(!await requestDialog({title:'确认税务推送单',description:`${preview.period} · ${preview.rows}行。字段：${preview.columns.join('、')}。${preview.note}`,submit:'确认归档',content:businessTable('推送内容',preview.columns.map((c,i)=>[String(i),c]),preview.data.map(row=>Object.fromEntries(row.map((v,i)=>[String(i),v]))))}))return;const r=await api('/api/tax-push',{...params,preview_hash:preview.preview_hash});toast(`已生成并留存 ${r.period} 推送单（${r.rows} 行）`);await loadTaxPushes()});
async function loadBatch(){
 $('#batch-reviewed').checked=false;
 $('#batch-confirm').disabled=true;
 $('#batch-confirm').dataset.docs='[]';
 const rows=await api('/api/batch-candidates');batchCandidates=rows;const ok=rows.filter(r=>r.eligible),shown=[...ok.slice(0,200),...rows.filter(r=>!r.eligible).slice(0,200)];renderGovernance();
 $('#batch-list').innerHTML=rows.length?`<p>${ok.length} 份可核对后批量确认（本批最多 200 份），${rows.length-ok.length} 份需逐项处理。同名弱匹配、缺失证据、低分或业务歧义不能批量入账。</p><table><thead><tr><th>档案</th><th>分流</th><th>关键字段与核算</th></tr></thead><tbody>${shown.map(r=>`<tr><td>${esc(r.name)}<br><button data-batch-review="${esc(r.id)}">查看原件与证据</button></td><td>${confidenceSummary(r.confidence,true)}${r.eligible?'':'需逐项核验：'+esc(r.reasons.slice(0,3).join('；'))+(r.reasons.length>3?`<details><summary>全部 ${r.reasons.length} 项原因</summary><p>${esc(r.reasons.join('；'))}</p></details>`:'')}</td><td><small>${esc(['tenant','parcel_no','area','start','end','mode','annual'].map(k=>r.fields[k]||'—').join(' · '))}</small>${r.evidence_status&&Object.keys(r.evidence_status).length?`<details><summary>字段证据与佐证路径</summary><ul>${Object.entries(r.evidence_status).map(([key,value])=>`<li>${esc(state.fields[key]||key)}：${esc(({anchored:'已有主证据',missing_anchor:'缺少主证据',conflict:'候选冲突'}[value.status])||value.status)} · ${value.independent_paths||0} 条识别路径${value.unknown_score_paths?' · '+value.unknown_score_paths+' 条未提供分数':''}${value.corroborating_without_region?' · '+value.corroborating_without_region+' 条仅作文本佐证':''}</li>`).join('')}</ul></details>`:''}${r.calculation?`<p>应缴 ${esc(r.calculation.due)} · 实缴 ${esc(r.calculation.paid)} · 欠缴 ${esc(r.calculation.arrears)} 元</p><details><summary>逐年核算、缴费与减免依据</summary>${renderCalc(r.calculation)}<pre>${esc(JSON.stringify(r.matches,null,2))}</pre></details>`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">没有待处理材料。</p>';
 document.querySelectorAll('[data-batch-review]').forEach(b=>b.onclick=()=>action(()=>openDoc(b.dataset.batchReview)));
 $('#batch-confirm').dataset.docs=JSON.stringify(ok.slice(0,200).map(r=>({id:r.id,version:r.version,draft_hash:r.draft_hash})));
}
$('#batch-refresh').onclick=()=>action(loadBatch);
$('#batch-reviewed').onchange=applyPermissions;
$('#batch-confirm').onclick=()=>action(async()=>{
 if(!$('#batch-reviewed').checked)throw Error('请逐份核对原件、资料完整性、缴费和核算后勾选声明');
 if(!await requestDialog({title:'批量确认登记',description:`本批 ${JSON.parse($('#batch-confirm').dataset.docs||'[]').length} 份材料。请确认已逐份核对原件、全部采纳值、缴费与核算；每份独立校验，失败项会保留原因。`,submit:'确认本批登记',danger:true}))return;
 $('#batch-confirm').disabled=true;
 const r=await api('/api/batch-confirm',{documents:JSON.parse($('#batch-confirm').dataset.docs||'[]'),reason:'批量确认：已逐份核对原件、完整性、核算和缴费',reviewed_summary:true,evidence_clear:true,material_complete:true,confirm_calculation:true,confirm_payments:true});
 toast(`批量确认发布 ${r.published} 份；${r.results.filter(x=>!x.published).map(x=>x.reason).join('；')||'全部成功'}`);
 $('#batch-results').innerHTML=businessTable('本次批量办理结果',[['name','文书'],['status','结果'],['reason','说明']],r.results.map(x=>({name:state.documents.find(d=>d.id===x.id)?.name||x.id,status:x.published?'已登记':'未登记',reason:x.reason||'确认记录已保存'})));
 await loadBatch();await refresh();
});
$('#backup').onclick=()=>action(async()=>{$('#backup-result').textContent=JSON.stringify(await api('/api/backup',{}),null,2);renderSystemSummary();toast('备份已完成，请查看保存位置')});
for(const selector of ['#uncertainty','#stop-date'])$(selector).oninput=invalidate;
$('#reason').oninput=()=>{dirty=true;updateRequirements()};
let ruleDraft=null;
$('#read-rules').onclick=()=>action(async()=>{const r=await api('/api/rules');$('#rates-json').value=JSON.stringify(r.rates,null,2);activeRuleVersion=r.active;renderSystemSummary();$('#rule-impact').textContent='当前版本 '+r.active;ruleDraft=null;$('#confirm-rules').disabled=true});
$('#rates-json').oninput=()=>{$('#rule-calculation-clear').checked=false;ruleDraft=null;$('#confirm-rules').disabled=true};
$('#preview-rules').onclick=()=>action(async()=>{$('#rule-calculation-clear').checked=false;ruleDraft=await api('/api/rules/preview',{rates:JSON.parse($('#rates-json').value),policy:currentPolicy()});$('#rule-impact').textContent=JSON.stringify(ruleDraft,null,2);$('#confirm-rules').disabled=false});
$('#confirm-rules').onclick=()=>action(async()=>{if(!ruleDraft)throw Error('请先预览差异');if(!await confirmAction('发布新费率并重算全部有效宗地。请确认已核对逐宗差异及正式变更依据。','发布计租规则'))return;const r=await api('/api/rules/confirm',{rates:JSON.parse($('#rates-json').value),policy:currentPolicy(),reason:$('#rule-reason').value,draft_hash:ruleDraft.draft_hash,confirm_calculation:$('#rule-calculation-clear').checked});$('#rule-impact').textContent=JSON.stringify(r,null,2);ruleDraft=null;$('#confirm-rules').disabled=true;toast('新规则和重算已发布，旧通知保持原版本')});


function updateRequirements(){
  $('#confirm-rules').disabled=operationBusy||$('#role').value!=='admin'||!ruleDraft||!$('#rule-calculation-clear').checked;
  if(!doc)return;
  $('#draft-state').textContent=dirty?'有未保存修改':(doc.draft||doc.review?'草稿已保存':'尚未保存草稿');
  $('#preview-state').textContent=draft?'预览有效':($('#calculation').children.length?'预览已失效':'未预览');
  const missing=[];
  const emptyFields=syncRequiredFields();
  if(emptyFields.length)missing.push('请填写必填项：'+emptyFields.join('、'));
  if($('#role').value==='viewer')missing.push('当前为只读角色');
  if(doc.state==='published')missing.push('本份材料已登记；修改前需先撤回登记');
  if(doc.state==='withdrawn')missing.push('本份材料已撤回；重新上传后可继续办理');
  if(doc.state==='held')missing.push('本份材料已挂起，请先继续人工核验或补件');
  if(!$('#alteration-assistance').hidden){
    const decision=alterationReview();
    if(!decision)missing.push('请作出涂改处签章的真人核验结论');
    else if(decision.decision==='unknown')missing.push('签章无法判断，请保存草稿并转为待补件');
    else if(!decision.unit_id)missing.push('请选择签章判断依据所在原件');
  }
  if(doc.intake?.technical_status==='failed')missing.push('材料解析失败，请补充支持的文件格式');
  const latest=state.documents?.find(d=>d.id===doc.id);
  if(latest&&latest.version>doc.version){missing.push('材料已更新，请重新加载后核对');if(obligationPreview)invalidateObligation('材料版本已更新，旧试算已失效。请重新加载材料。');}
  if(!draft)missing.push('请先预览租金');
  const unresolved=[...document.querySelectorAll('[data-quality]')].filter(e=>!e.querySelector('[data-decision]').value||!e.querySelector('[data-resolution-reason]').value.trim()||!e.querySelector('[data-resolution-unit]').value).length;
  if(unresolved)missing.push(`还有 ${unresolved} 项问题未处理`);
  const uncited=[...document.querySelectorAll('[data-field]')].filter(e=>{
    if(!e.value.trim()||e.dataset.field==='mode'||['doc_type','has_map','missing_pages','altered_sealed','note'].includes(e.dataset.field))return false;
    const key=e.dataset.field, unitId=$(`[data-cite="${key}"]`).value, unit=doc.pages.find(p=>p.id===unitId);
    return !unit||(unit.kind==='native'&&!(citationDetails[key]||[]).some(r=>r.unit_id===unitId&&r.locator));
  }).length;
  if(uncited)missing.push(`还有 ${uncited} 项信息未关联原文位置`);
  if(!$('#reason').value.trim())missing.push('请填写核对说明');
  syncReviewError($('#reason'),reviewValidationAttempted&&!$('#reason').value.trim(),'请填写核对说明');
  if(!$('#registration-clear').checked)missing.push('请勾选合同信息与核算结果确认');
  const immutable=['published','withdrawn'].includes(doc.state);
  const html=immutable?`<b>${doc.state==='published'?'本份材料已登记。需要修订时，请提交撤回登记申请。':'本份材料已撤回。重新上传后可继续办理。'}</b>`:missing.length?`<b>登记前还需完成</b><ul>${missing.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<b>已完成核对，可以确认登记。</b>';
  if($('#confirm-requirements').innerHTML!==html)$('#confirm-requirements').innerHTML=html;
  $('#remaining-count').textContent=immutable?'当前文书只读':missing.length?`登记前待办 · ${missing.length} 项`:'已完成核对';
  const destinations=missing.map(x=>x.includes('必填项')?[...document.querySelectorAll('[data-field][required]')].find(e=>!e.value.trim())?.id||'fields':x.includes('签章')?'alteration-assistance':x.includes('预览')?'preview':x.includes('问题')?'fields':x.includes('原文位置')?'fields':x.includes('说明')?'reason':x.includes('更新')?'reload-doc':x.includes('完整性')?'material-clear':x.includes('原件')?'evidence-clear':x.includes('缴费')?'payments-clear':x.includes('核算')?'calculation-clear':'review-stage-3');
  const pending=(immutable?[]:missing).map((x,i)=>`<button data-jump="${destinations[i]}">${esc(x)}</button>`).join('');
  if($('#remaining-items').innerHTML!==pending)$('#remaining-items').innerHTML=pending;
  $('#confirm').disabled=operationBusy||obligationMode||$('#role').value==='viewer'||immutable||doc.state==='held'||doc.intake?.technical_status==='failed';
  $('#preview').classList.toggle('primary',!draft);
  $('#confirm').classList.toggle('primary',!!draft);
  return missing;
}
function applyPermissions(){
  renderApprovalState();
  document.querySelectorAll('#new-contract,[data-business-event],[data-make-notice],[data-send-notice],[data-renew-parcel],[data-existing-new],[data-change-doc],[data-upload-signed]').forEach(el=>el.disabled=operationBusy||$('#role').value==='viewer');
  const readOnly=$('#role').value==='viewer', admin=$('#role').value==='admin';
  $('#role-hint').hidden=!readOnly;
  document.querySelectorAll('[data-contract],[data-notice-status],#tax-push,#tax-year,#tax-quarter,#batch-reviewed').forEach(el=>el.disabled=readOnly);
  $('#batch-confirm').disabled=readOnly||operationBusy||!$('#batch-reviewed').checked||!JSON.parse($('#batch-confirm').dataset.docs||'[]').length;
  document.querySelectorAll('[data-admin] button,[data-admin] input,[data-admin] textarea').forEach(el=>el.disabled=!admin);
  $('#confirm-rules').disabled=!admin||operationBusy||!ruleDraft||!$('#rule-calculation-clear').checked;
  document.querySelectorAll('[data-admin]').forEach(el=>el.hidden=!admin);
  document.querySelectorAll('[data-template-admin] button,[data-template-admin] input').forEach(el=>el.disabled=!admin||operationBusy);
  $('#admin-hint').hidden=admin;
  document.querySelectorAll('#upload button,#upload input,#batch-upload button,#batch-upload input,[data-sample]').forEach(el=>el.disabled=readOnly);
  if(doc){
    const locked=readOnly||['published','withdrawn'].includes(doc.state);
    document.querySelectorAll('[data-field]').forEach(el=>{el.readOnly=locked;if(el.tagName==='SELECT')el.disabled=locked});
    document.querySelectorAll('[data-cite],[data-candidate],[data-decision],[data-resolution-reason],[data-resolution-unit],[data-remove-ref],[data-native-ref],[name="seal-decision"],#alteration-source,#uncertainty,#stop-date,#registration-clear,#material-clear,#evidence-clear,#payments-clear,#calculation-clear,#attach-files,[data-payment],[data-relief]').forEach(el=>el.disabled=locked);
    for(const id of ['retry','retry-unknown','save-draft','preview','hold'])$('#'+id).disabled=locked;
    $('#resume-review').disabled=readOnly||doc.state!=='held';
    $('#attach-form button').disabled=locked||doc.state==='recognizing';
    document.querySelectorAll('#merge-form button,#merge-target').forEach(el=>el.disabled=locked||doc.state==='recognizing');
    $('#reason').readOnly=readOnly||doc.state==='withdrawn';
    $('#withdraw').disabled=readOnly||doc.state!=='published'||(doc.withdrawal_requests||[]).some(r=>r.status==='pending');
    const page=doc.pages.find(p=>p.id===$('#page-select').value);
    $('#rotate').disabled=locked||!page?.path;
    $('#previous').disabled=locked||!page?.path||doc.pages[0]?.id===page.id;
  }
  document.querySelectorAll('[data-notice]').forEach(el=>{el.disabled=readOnly||el.dataset.noArrears==='true';el.title=readOnly?'只读角色不能生成通知':el.dataset.noArrears==='true'?'无欠缴，无需催缴':''});
  document.querySelectorAll('#obligation-evidence-clear,#obligation-reliefs-clear,#obligation-termination-month,#obligation-termination-source').forEach(el=>el.disabled=readOnly||!doc||['published','withdrawn'].includes(doc?.state));
  updateObligationButton();
  enhanceTables();
  SelectControls.enhance();
}
$('#open-upload').onclick=()=>{$('#intake').open=true;$('#intake').scrollIntoView({block:'start'});$('#name').focus()};
function setDocumentFilter(value){documentFilter=value;documentPage=1;updateStatistics();renderDocuments(true)}
$('#all-documents').onclick=()=>setDocumentFilter('');
$('#document-status').onchange=()=>setDocumentFilter($('#document-status').value);
$('#document-search').oninput=()=>{documentSearch=$('#document-search').value.trim();documentPage=1;renderDocuments(true)};
$('#document-sort').onchange=()=>{documentSort=$('#document-sort').value;documentPage=1;renderDocuments(true)};
for(const [id,delta] of [['page-prev',-1],['page-next',1]])$('#'+id).onclick=()=>{documentPage+=delta;renderDocuments(true);$('#documents').focus()};
$('#go-pending').onclick=()=>action(()=>tab('governance'));
$('#review-content').addEventListener('input',e=>{
  if(e.target.matches('[data-resolution-reason]'))invalidate();
});
$('#review-content').addEventListener('change',e=>{
  if(e.target.matches('[data-decision],[data-resolution-unit]'))invalidate();
  if(e.target.matches('#registration-clear')){for(const id of ['material-clear','evidence-clear','payments-clear','calculation-clear'])$('#'+id).checked=e.target.checked;dirty=true;updateRequirements()}
  if(e.target.matches('#material-clear,#evidence-clear,#payments-clear,#calculation-clear')){dirty=true;updateRequirements()}
});
let selectedRole=$('#role').value;
$('#rule-calculation-clear').onchange=updateRequirements;
$('#role').onchange=()=>{
  const next=$('#role').value;$('#role').value=selectedRole;SelectControls.enhance();
  action(async()=>{const choice=await guardUnsaved();if(choice==='cancel')return;if(choice==='discard')await openDoc(doc.id,true);$('#role').value=next;selectedRole=next;if(doc)renderWithdrawalReview();applyPermissions();updateRequirements();window.dispatchEvent(new Event('fde-role-changed'))});
};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});
$('.skip-link').onclick=e=>{e.preventDefault();$('#main-content').focus();$('#main-content').scrollIntoView({block:'start'})};
$('.brand').onclick=e=>{e.preventDefault();action(()=>tab('inbox'))};
// Pass the originating control to action(), including keyboard-submitted forms.
for(const el of document.querySelectorAll('button,form,.brand,#role')){
  for(const name of ['onclick','onsubmit','onchange']){
    const handler=el[name];if(!handler)continue;
    el[name]=function(event){const old=invocationTrigger;invocationTrigger=event.submitter||this;try{return handler.call(this,event)}finally{invocationTrigger=old}};
  }
}
async function poll(){
  if(operationBusy||refreshPending||document.hidden||$('#unsaved-dialog').open||$('#action-dialog').open)return;
  refreshPending=true;
  try{await refresh();$('#connection-status').hidden=true}
  catch(error){$('#connection-status').textContent='进度刷新失败：'+error.message+'。已保留当前输入，稍后自动重试。';$('#connection-status').hidden=false}
  finally{refreshPending=false}
}
SelectControls.enhance();applyPermissions();
document.querySelectorAll('nav button[data-tab]').forEach(b=>b.querySelector('span').textContent=pages[b.dataset.tab].title);
$('#title').textContent=pages.inbox.title;$('#breadcrumb-current').textContent=pages.inbox.title;$('#page-description').textContent=pages.inbox.description;
const initialRoute=location.hash;
action(async()=>{await refresh();history.replaceState(null,'',location.pathname+location.search+initialRoute);await restoreRoute()});setInterval(poll,5000);
window.addEventListener('hashchange',()=>action(async()=>{const requested=location.hash;if(await guardUnsaved()==='cancel'){writeRoute();return}history.replaceState(null,'',requested);await restoreRoute()}));

function jumpToReviewTarget(id){
  let target=document.getElementById(id);if(!target)return;
  for(let p=target.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;
  if(target.matches('select'))target=target.parentElement.querySelector(`[data-select-for="${target.id}"] .select-trigger`)||target;
  else if(!target.matches('input,button,textarea'))target=target.querySelector('input:not(:disabled),button:not(:disabled),textarea:not(:disabled)')||target;
  if(!target.matches('input,button,textarea,select,a[href]'))target.setAttribute('tabindex','-1');
  target.scrollIntoView({block:'center'});target.focus({preventScroll:true});
}
document.addEventListener('click',event=>{const b=event.target.closest('[data-jump]');if(b)jumpToReviewTarget(b.dataset.jump)});
