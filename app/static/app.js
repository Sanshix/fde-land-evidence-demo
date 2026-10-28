const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const technicalLabels={parsed:"提取完成",partial:"部分完成",failed:"解析失败",legacy:"历史资料"};
const locationLabel=s=>state.fields?.[s]||({document:"整份文书"}[s])||s;
const labels = {queued:'等待处理',recognizing:'正在提取 / 识别',needs_human:'待人工复核',held:'挂起 · 待补件',published:'已确认发布',withdrawn:'已撤回'};
let state={}, doc=null, draft=null, started=0, dirty=false, citationDetails={}, selectedPayments=[], selectedReliefs=[], governanceRows=[];
function toast(text){ $('#toast').textContent=text; $('#toast').style.display='block'; setTimeout(()=>$('#toast').style.display='none',6500); }
async function api(path,body,method){
  const headers={'X-FDE-Role':$('#role').value,'X-FDE-Actor':encodeURIComponent($('#actor').value)};
  if(body && !(body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch(path,{method:method||(body?'POST':'GET'),headers,body:body?(body instanceof FormData?body:JSON.stringify(body)):undefined});
  if(response.status===401) throw Error('需要已配置的本地账号登录');
  const value=await response.json(); if(!response.ok) throw Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail)); return value;
}
async function action(fn){try{return await fn()}catch(error){toast(error.message);return null}}
function tab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==name));
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));
  $('#title').textContent={inbox:'材料与任务',governance:'待核验工作台',review:'证据复核',ledger:'宗地台账',measure:'运行证据'}[name];
  if(name==='ledger') action(loadLedger); if(name==='governance') action(loadGovernance);
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
async function refresh(){
  state=await api('/api/status');const docs=state.documents;
  $('#stats').innerHTML=[['材料文书',docs.length],['待核对 / 处理中',docs.filter(x=>!['published','withdrawn','held'].includes(x.state)).length],['已确认',docs.filter(x=>x.state==='published').length],['挂起待补件',docs.filter(x=>x.state==='held').length]].map(([k,v])=>`<div class="stat"><span>${k}</span><b>${v}</b></div>`).join('');
  $('#documents').innerHTML=docs.length?`<table><thead><tr><th>档案</th><th>技术 / 业务状态</th><th>证据</th><th></th></tr></thead><tbody>${docs.map(d=>`<tr><td><b>${esc(d.name)}</b><br><small>${esc(d.id)}</small></td><td>${esc(d.intake?.technical_status||'历史记录')} · ${labels[d.state]}<br>${d.pages.length} 个证据单元</td><td>${d.result?`${esc(d.result.mode)} · ${d.result.elapsed_seconds}s · ${d.result.risks.length}项提示`:'尚未完成提取，可人工接管'}</td><td><button data-open="${d.id}">打开复核</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty">尚无材料，导入一份文书开始。</div>';
  bindOpenButtons();
  $('#batches').innerHTML=(state.batches||[]).map(b=>`<p><b>${esc(b.label)}</b> · ${b.elapsed_seconds}s · 已发布${b.published}/${b.expected||b.documents} · 挂起${b.held} · 失败${b.failures||0} · ${b.all_published?'全部发布，仍需核对正确性':'整包闭环未完成'}</p>`).join('');
  $('#model-status').textContent='';const pre=document.createElement('pre');pre.textContent=JSON.stringify(state.models,null,2);$('#model-status').append(pre);
  $('#jobs').innerHTML=`<table><thead><tr><th>任务</th><th>状态</th><th>信息</th></tr></thead><tbody>${state.jobs.map(j=>`<tr><td>${esc(j.id)}<br>${esc(j.strategy)}</td><td>${esc(j.state)}</td><td>${esc(j.error||j.run_path||'等待处理')}</td></tr>`).join('')}</tbody></table>`;
  if(doc&&!dirty&&['queued','recognizing'].includes(doc.state)){const latest=docs.find(d=>d.id===doc.id);if(latest&&latest.version!==doc.version)await openDoc(doc.id)}
}
function bindOpenButtons(){document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>action(()=>openDoc(b.dataset.open)))}
async function loadGovernance(){governanceRows=await api('/api/governance');renderGovernance()}
function renderGovernance(){
  const filter=$('#governance-filter').value;const rows=governanceRows.filter(r=>!filter||r.categories.includes(filter));
  $('#governance-list').innerHTML=rows.length?rows.map(r=>`<article class="case-card"><div class="section-head"><h2>${esc(r.name)}</h2><button data-open="${r.id}">进入核验</button></div><div class="quality-stages"><span>技术：${esc(technicalLabels[r.technical_status]||r.technical_status)}</span><span>资料：${esc(r.material_status)}</span><span>业务：${esc(r.business_status)}</span></div><p>${labels[r.state]} · ${r.candidate_count} 个候选，均未采纳 · 版本 ${r.version}</p><ul>${r.issues.map(i=>`<li><b>${esc(i.category)}</b> · ${esc(locationLabel(i.location))}：${esc(i.message)}</li>`).join('')}</ul></article>`).join(''):'<div class="empty">该类别暂无待核验材料。</div>';
  bindOpenButtons();
}
$('#refresh-governance').onclick=()=>action(loadGovernance);$('#governance-filter').onchange=renderGovernance;
$('#refresh').onclick=()=>action(refresh);
document.querySelectorAll('[data-sample]').forEach(b=>b.onclick=()=>action(async()=>{b.disabled=true;try{const r=await api('/api/sample/'+b.dataset.sample,{});await refresh();await openDoc(r.id);toast(r.duplicate?'已识别重复材料，未重复入账':'已接收；真实模式以运行证据为准')}finally{b.disabled=false}}));
$('#upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('name',$('#name').value);for(const f of $('#files').files)data.append('files',f);const r=await api('/api/import',data);await refresh();await openDoc(r.id)})};
$('#batch-upload').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData();data.append('file',$('#batch-file').files[0]);const r=await api('/api/import-batch',data);toast(r.partial_failure?'部分文件导入失败，已保留成功项':'整包已接收，继续核验');await refresh()})};
function invalidate(){dirty=true;draft=null;$('#confirm').disabled=true;$('#calculation-clear').checked=false}
function unitOptions(selected){return doc.pages.map(p=>`<option value="${p.id}" ${p.id===selected?'selected':''}>${esc(p.name)}</option>`).join('')}
async function openDoc(id){
  doc=await api('/api/documents/'+id);const saved=doc.draft||doc.review||{};
  dirty=false;draft=null;started=Date.now();citationDetails=structuredClone(saved.citation_details||{});selectedPayments=saved.payment_ids||[];selectedReliefs=saved.relief_ids||[];
  tab('review');$('#empty-review').classList.add('hidden');$('#review-content').classList.remove('hidden');
  $('#doc-name').textContent=doc.name;$('#doc-meta').textContent=`${labels[doc.state]} · 版本 ${doc.version} · ${doc.pages.length} 个证据单元`;
  $('#governance-status').textContent=`技术提取：${technicalLabels[doc.intake?.technical_status]||'历史资料'} ｜资料完整性：${doc.review?.material_complete?'已人工核对':'待核对'} ｜业务生效：${doc.state==='published'?'已确认':'未生效'}`;
  $('#inventory').innerHTML=`<p>原件摘要：${esc(doc.digest)}</p>${(doc.intake?.files||[]).map(f=>`<p><b>${esc(f.name)}</b> · ${esc(f.format)} · ${esc(f.technical_status)} · ${f.units} 单元 / ${f.native_lines} 原生行<br><small>${esc(f.sha256)}</small></p>`).join('')}<details><summary>完整页 / 表 / 单元清单</summary><pre>${esc(JSON.stringify(doc.pages.map(p=>({id:p.id,name:p.name,kind:p.kind,location:p.location,lines:p.native_lines?.length,hidden:p.hidden,merged_ranges:p.merged_ranges})),null,2))}</pre></details>`;
  $('#page-select').innerHTML=unitOptions(doc.pages[0].id);
  $('#evidence-field').innerHTML=Object.entries(state.fields).map(([k,label])=>`<option value="${k}">${label}</option>`).join('');
  $('#risks').innerHTML=(doc.governance_issues||[]).map(i=>`<div class="risk"><b>${esc(i.category)}</b> · ${esc(i.message)}</div>`).join('');
  $('#show-suggestions').checked=false;
  $('#fields').innerHTML=Object.entries(state.fields).map(([key,label])=>{
    const candidates=doc.result?.candidates?.[key]||[];
    return `<div class="field"><div class="field-title"><b>${label}</b><small>${candidates.length} 个候选</small></div><div class="candidates hidden">${candidates.map((c,i)=>`<button class="candidate" data-key="${key}" data-candidate="${i}">${esc(c.value)}<span>${c.kind==='native_extract'?'原生结构提取（非AI分数）':c.kind==='context_suggestion'?'上下文预计值 · 不能当原文':c.score_meaning==='not_provided'?'未提供识别分数':`${Math.round(c.score*100)}%识别分数（非正确概率）`} · ${esc(c.model)}</span><small>原值：${esc(c.raw_value??c.raw_text)}<br>${esc(c.reason)}<br>${esc(c.source_locator||c.source_page_id)}</small></button>`).join('')||'<small>无候选，不能推定事实不存在。</small>'}</div><input data-field="${key}" aria-label="${label}采纳值" value="${esc(saved.fields?.[key]||'')}" placeholder="人工核验后采纳" ${['use','grade','mode'].includes(key)?`list="${{use:'uses',grade:'grades',mode:'modes'}[key]}"`:''}><select data-cite="${key}" aria-label="${label}主要来源"><option value="">请选择证据单元</option>${unitOptions(saved.citations?.[key])}</select><div data-reference-list="${key}" class="references"></div></div>`;
  }).join('');
  document.querySelectorAll('[data-field]').forEach(e=>{e.oninput=invalidate;e.onfocus=()=>$('#evidence-field').value=e.dataset.field});
  document.querySelectorAll('[data-cite]').forEach(e=>e.onchange=()=>{invalidate();if(e.value){$('#evidence-field').value=e.dataset.cite;showPage(e.value)}});
  document.querySelectorAll('[data-candidate]').forEach(b=>b.onclick=()=>{
    const key=b.dataset.key,c=doc.result.candidates[key][+b.dataset.candidate];$(`[data-field="${key}"]`).value=c.value;$(`[data-cite="${key}"]`).value=c.source_page_id;
    if(c.source_locator)for(const locator of c.source_locator.split(' + '))addReference(key,c.source_page_id,locator);else addReference(key,c.source_page_id,'');
    $('#evidence-field').value=key;showPage(c.source_page_id,c.bbox,c.source_locator?.split(' + ')[0]);invalidate();
  });
  Object.keys(state.fields).forEach(renderReferences);
  $('#reason').value=saved.reason||'';$('#uncertainty').value=saved.uncertainty||'';$('#stop-date').value=saved.stop||'';
  for(const id of ['material-clear','evidence-clear','payments-clear','calculation-clear'])$('#'+id).checked=false;
  $('#matching').innerHTML='';delete $('#matching').dataset.doc;$('#calculation').innerHTML='';$('#confirm').disabled=true;
  const blocking=(doc.governance_issues||[]).filter(i=>i.severity==='blocking');
  $('#quality-resolutions').innerHTML=blocking.length?`<h3>阻断项逐条处理</h3>${blocking.map(i=>{const r=saved.quality_resolutions?.[i.id]||{};return `<div class="risk" data-quality="${i.id}"><b>${esc(locationLabel(i.location))} · ${esc(i.message)}</b><select data-decision aria-label="${esc(locationLabel(i.location))}处理结论"><option value="">未解决</option value="verified_with_evidence" ${r.decision==='verified_with_evidence'?'selected':''}>已有可核实证据</option><option value="excluded_with_reason" ${r.decision==='excluded_with_reason'?'selected':''}>有依据排除该内容</option></select><input data-resolution-reason placeholder="不能靠猜测；说明核对或排除依据" value="${esc(r.reason||'')}"><select data-resolution-unit><option value="">选择依据单元</option>${unitOptions(r.source_unit)}</select></div>`}).join('')}`:'';
  $('#audit').textContent=doc.audit.map(x=>`${new Date(x.at*1000).toLocaleString()} · ${x.actor} · ${x.action}\n${x.payload}`).join('\n\n');
  const locked=['published','withdrawn'].includes(doc.state);$('#retry').disabled=locked;$('#attach-form button').disabled=locked;$('#save-draft').disabled=locked;document.querySelectorAll('[data-field]').forEach(e=>e.readOnly=locked);document.querySelectorAll('[data-cite],[data-candidate],[data-decision],[data-resolution-reason],[data-resolution-unit]').forEach(e=>e.disabled=locked);
  showPage(doc.pages[0].id);
}
function addReference(key,unit_id,locator){const rows=citationDetails[key]||(citationDetails[key]=[]);if(!rows.some(r=>r.unit_id===unit_id&&r.locator===locator))rows.push({unit_id,locator});renderReferences(key)}
function renderReferences(key){const target=$(`[data-reference-list="${key}"]`);if(!target)return;target.innerHTML=(citationDetails[key]||[]).map((r,i)=>`<span>${esc(doc.pages.find(p=>p.id===r.unit_id)?.name||r.unit_id)} · ${esc(r.locator||'图像页')} <button data-remove-ref="${i}" aria-label="移除引用">×</button></span>`).join('');target.querySelectorAll('[data-remove-ref]').forEach(b=>b.onclick=()=>{citationDetails[key].splice(+b.dataset.removeRef,1);renderReferences(key);invalidate()})}
function showPage(id,box,locator){
  const unit=doc.pages.find(p=>p.id===id);if(!unit)return;
  $('#page-select').value=id;$('#download-original').href=`/api/original/${doc.id}/${id}`;
  $('#rotate').disabled=!unit.path;$('#previous').disabled=!unit.path;
  $('#image-wrap').classList.toggle('hidden',!unit.path);$('#overlay').innerHTML='';
  if(unit.path){$('#original').onload=()=>{const im=$('#original');$('#overlay').setAttribute('viewBox',`0 0 ${im.naturalWidth} ${im.naturalHeight}`);if(box)$('#overlay').innerHTML=`<rect x="${box[0]}" y="${box[1]}" width="${box[2]-box[0]}" height="${box[3]-box[1]}" fill="#efb52c33" stroke="#e69712" stroke-width="5"/>`};$('#original').src=`/api/pages/${doc.id}/${id}?v=${doc.version}`}
  const lines=unit.native_lines||[];$('#native-evidence').classList.toggle('hidden',!lines.length&&!(!unit.path));
  $('#native-evidence').innerHTML=lines.length?`<h3>${esc(unit.name)} · 原生证据</h3><p class="muted">${unit.kind==='native'?'未模拟Word分页；按真实段落/单元格定位。':'PDF文字层与原图并存，请同时核验。'}</p><table><thead><tr><th>原件位置</th><th>原值与显示解释</th><th></th></tr></thead><tbody>${lines.map((l,i)=>`<tr class="${l.locator===locator?'evidence-selected':''}"><td>${esc(l.locator)}${l.hidden?' · 隐藏':''}</td><td><b>${esc(l.raw_text)}</b>${l.display_value!==undefined?`<br><small>显示解释：${esc(l.display_value)} · 格式 ${esc(l.number_format)}<br>XML原值：${esc(l.raw_xml_value)}${l.formula!==null&&l.formula!==undefined?`<br>公式：${esc(l.formula)} · 缓存：${esc(l.cached_value)}`:''}</small>`:''}${l.deleted_text?`<br>删除修订：${esc(l.deleted_text)}`:''}</td><td><button data-native-ref="${i}">引用</button></td></tr>`).join('')}</tbody></table>`:'<div class="error">该原件未能解析。可下载核查并补充受支持格式，不能标为已完成。</div>';
  document.querySelectorAll('[data-native-ref]').forEach(b=>b.onclick=()=>{const key=$('#evidence-field').value;addReference(key,id,lines[+b.dataset.nativeRef].locator);$(`[data-cite="${key}"]`).value=id;invalidate();toast('已关联原文位置，尚未采纳任何值')});
  $('#native-evidence .evidence-selected')?.scrollIntoView({block:'nearest'});
}
$('#show-suggestions').onchange=()=>document.querySelectorAll('.candidates').forEach(e=>e.classList.toggle('hidden',!$('#show-suggestions').checked));
$('#page-select').onchange=()=>showPage($('#page-select').value);$('#reload-doc').onclick=()=>action(()=>openDoc(doc.id));
$('#retry').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/retry`,{version:doc.version});await openDoc(doc.id);toast('已创建处理任务；未配置模型会明确转人工')});
for(const [selector,operation] of [['#rotate','rotate'],['#previous','previous']])$(selector).onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/page`,{version:doc.version,page_id:$('#page-select').value,action:operation});await openDoc(doc.id)});
$('#attach-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!$('#attach-files').files.length)throw Error('先选择补充材料');const data=new FormData();data.append('version',doc.version);for(const f of $('#attach-files').files)data.append('files',f);const r=await api(`/api/documents/${doc.id}/attach`,data);await openDoc(doc.id);toast(r.duplicate?'补件内容重复，无新增贡献':'补件已保存；旧确认、旧草稿和核算预览已失效')})};
function body(){
  const fields={},citations={},quality_resolutions={};document.querySelectorAll('[data-field]').forEach(e=>fields[e.dataset.field]=e.value.trim());document.querySelectorAll('[data-cite]').forEach(e=>citations[e.dataset.cite]=e.value);
  document.querySelectorAll('[data-quality]').forEach(e=>quality_resolutions[e.dataset.quality]={decision:e.querySelector('[data-decision]').value,reason:e.querySelector('[data-resolution-reason]').value,source_unit:e.querySelector('[data-resolution-unit]').value});
  return {version:doc.version,fields,citations,citation_details:citationDetails,payment_ids:selectedPayments,relief_ids:selectedReliefs,quality_resolutions,reason:$('#reason').value,uncertainty:$('#uncertainty').value,stop:$('#stop-date').value,material_complete:$('#material-clear').checked,evidence_clear:$('#evidence-clear').checked,confirm_payments:$('#payments-clear').checked,confirm_calculation:$('#calculation-clear').checked,review_elapsed_seconds:Math.round((Date.now()-started)/1000),draft_hash:draft?.draft_hash};
}
$('#save-draft').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/draft`,body());dirty=false;toast('草稿已保存并留痕，未进入有效台账')});
$('#preview').onclick=()=>action(async()=>{const r=await api(`/api/documents/${doc.id}/preview`,body());renderMatches(r.matches);if(r.errors.length){draft=null;$('#calculation').innerHTML=`<div class="error">${r.errors.map(esc).join('<br>')}</div>`;$('#confirm').disabled=true}else{draft=r;$('#calculation').innerHTML=renderCalc(r.calculation,r.previous);$('#confirm').disabled=false;toast('核算预览已生成，请完成第二道复核后确认')}});
function renderMatches(m){
  $('#matching').innerHTML=`<h3>登记 / 缴费 / 减免候选</h3><p class="muted">${esc(m.warning||'多候选需人工核对')}</p><pre>${esc(JSON.stringify(m.registry,null,2))}</pre><div class="payments">${m.payments.map(p=>`<label><input type="checkbox" data-payment="${esc(p['序号'])}" ${selectedPayments.includes(String(p['序号']))?'checked':''}>票${esc(p['票据号'])} · ${esc(p['缴费日期'])} · ${esc(p['金额（元）'])}元 · ${esc(p['所属年度']||'未注明年度')} · ${esc(p['缴款人'])}</label>`).join('')||'没有精确匹配的缴费候选；仍需核对原表，不能据此断言无缴费。'}</div>${m.reliefs.map(r=>`<label><input type="checkbox" data-relief="${esc(r['批准文号'])}" ${selectedReliefs.includes(r['批准文号'])?'checked':''}>${esc(r['批准文号'])} ${esc(r['减免比例'])}</label>`).join('')}`;
  document.querySelectorAll('[data-payment],[data-relief]').forEach(e=>e.onchange=()=>{selectedPayments=[...document.querySelectorAll('[data-payment]:checked')].map(e=>e.dataset.payment);selectedReliefs=[...document.querySelectorAll('[data-relief]:checked')].map(e=>e.dataset.relief);invalidate()});
}
function renderCalc(p,previous){return `<div class="calc"><span>截至 ${esc(p.as_of)} · ${esc(p.ruleset)}</span><p>应缴 ${esc(p.due)}　实缴 ${esc(p.paid)}</p><strong>欠缴 ¥ ${esc(p.arrears)}</strong>${previous?`<p>上版 ${esc(previous.payload.arrears)} → 本版 ${esc(p.arrears)}</p>`:''}<p>催缴对象：${esc(p.recipient)} · ${esc(p.phone||'电话待核实')}</p><details><summary>逐年依据与付款归集</summary><pre>${esc(JSON.stringify({annual:p.annual,segments:p.segments,allocations:p.allocations},null,2))}</pre></details><details open><summary>问题与覆盖边界</summary><pre>${esc(JSON.stringify(p.issues,null,2))}</pre><small>${esc(p.coverage)}</small></details></div>`}
$('#confirm').onclick=()=>action(async()=>{if(!draft)throw Error('请先重新预览');await api(`/api/documents/${doc.id}/confirm`,body());toast('人工确认与核算复核已留痕，台账原子发布');await refresh();await openDoc(doc.id)});
$('#hold').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/draft`,body());await api(`/api/documents/${doc.id}/hold`,{version:doc.version,reason:$('#reason').value});await refresh();await openDoc(doc.id);toast('已挂起待补件，未进入有效台账')});
$('#withdraw').onclick=()=>action(async()=>{await api(`/api/documents/${doc.id}/withdraw`,{version:doc.version,reason:$('#reason').value});await refresh();await openDoc(doc.id);toast('已撤回贡献并重算，历史与原件保留')});
async function loadLedger(){
 $('#ledger-content').textContent='正在读取当前有效快照…';$('#notice-preview').innerHTML='';
 const rows=await api('/api/ledger');$('#ledger-content').innerHTML=rows.length?rows.map(r=>`<div class="card"><div class="section-head"><h2>宗地 ${esc(r.parcel_id)}</h2><span class="tag">版本 ${r.id} · ${esc(r.digest.slice(0,12))}</span></div>${r.payload.documents.length?renderCalc(r.payload):'<p>当前没有有效合同，贡献已撤回。</p>'}${r.payload.documents.length?`<button data-notice="${r.id}">生成催缴草稿（不发送）</button>`:''}</div>`).join(''):'<div class="empty">暂无有效台账，先完成资料核验和人工确认。</div>';
 document.querySelectorAll('[data-notice]').forEach(b=>b.onclick=()=>action(async()=>{const n=await api('/api/notices/'+b.dataset.notice,{});$('#notice-preview').innerHTML=`<div class="card"><h2>催缴草稿 · 未发送</h2><a class="button" href="/api/notices/${n.id}/document">下载模板DOCX</a><pre>${esc(JSON.stringify(n,null,2))}</pre></div>`}));
}
$('#backup').onclick=()=>action(async()=>$('#backup-result').textContent=JSON.stringify(await api('/api/backup',{}),null,2));
for(const selector of ['#uncertainty','#stop-date'])$(selector).oninput=invalidate;
let ruleDraft=null;
$('#read-rules').onclick=()=>action(async()=>{const r=await api('/api/rules');$('#rates-json').value=JSON.stringify(r.rates,null,2);$('#rule-impact').textContent='当前版本 '+r.active;ruleDraft=null;$('#confirm-rules').disabled=true});
$('#rates-json').oninput=()=>{$('#rule-calculation-clear').checked=false;ruleDraft=null;$('#confirm-rules').disabled=true};
$('#preview-rules').onclick=()=>action(async()=>{$('#rule-calculation-clear').checked=false;ruleDraft=await api('/api/rules/preview',{rates:JSON.parse($('#rates-json').value)});$('#rule-impact').textContent=JSON.stringify(ruleDraft,null,2);$('#confirm-rules').disabled=false});
$('#confirm-rules').onclick=()=>action(async()=>{if(!ruleDraft)throw Error('请先预览差异');const r=await api('/api/rules/confirm',{rates:JSON.parse($('#rates-json').value),reason:$('#rule-reason').value,draft_hash:ruleDraft.draft_hash,confirm_calculation:$('#rule-calculation-clear').checked});$('#rule-impact').textContent=JSON.stringify(r,null,2);ruleDraft=null;$('#confirm-rules').disabled=true;toast('新规则和重算已发布，旧通知保持原版本')});
action(refresh);setInterval(()=>action(refresh),5000);
