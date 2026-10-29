/* Module-specific views. Money is always returned by the server. */
function currentPolicy(){return {segmentation:'calendar_month',relief:'approved_source',uncertainty:'minimum_due',rounding:$('#policy-rounding')?.value||'half_up'}}
function parcelLinks(r){return `<button data-open-parcel="${esc(r.parcel_id)}">查看宗地</button><button data-renew-parcel="${esc(r.parcel_id)}">办理续期</button>`}
async function loadBusinessPage(name){
 const hosts={ledger:'ledger-content',rent:'rent-content',reminders:'reminder-list',notices:'notice-candidates',contracts:'contract-list',tax:'tax-list',statistics:'statistics-detail'};
 const host=$('#'+hosts[name]);host.textContent='正在加载…';
 try{
 if(name==='ledger'){await loadLedger();document.querySelectorAll('#ledger-content [data-parcel]').forEach(el=>{const heading=el.querySelector('.section-head');const details=document.createElement('details'),summary=document.createElement('summary');summary.append(heading);details.append(summary);while(el.firstChild)details.append(el.firstChild);el.append(details)});document.querySelectorAll('#ledger-content [data-parcel]').forEach(el=>{const id=el.dataset.parcel;el.querySelector('details').insertAdjacentHTML('beforeend',`<button data-history="${esc(id)}">合同链与历史版本</button><div class="parcel-history"></div>`)});document.querySelectorAll('[data-history]').forEach(b=>b.onclick=()=>action(async()=>{const h=await api(`/api/parcels/${b.dataset.history}/history`);b.nextElementSibling.innerHTML=h.publications.map(p=>`<details><summary>版本 ${p.id} · ${stamp(p.created)} · ${esc(p.actor)}</summary>${renderCalc(p.payload)}<p>档案：${(p.payload.documents||[]).map(id=>`<button data-evidence-doc="${esc(id)}">${esc(id)}</button>`).join('')}</p></details>`).join('');bindBusinessLinks()}));return}
 if(name==='reminders'){await loadReminders();return}
 if(name==='tax'){await loadTaxPushes();return}
 if(name==='statistics'){await loadStatistics();return}
 const rows=await api('/api/ledger');
 if(name==='rent'){
 const contractPanels=await Promise.all(rows.map(rentContractPanel));
 host.innerHTML=rows.map((r,i)=>`<details class="card"><summary>${esc(r.payload.address)} · ${esc(r.payload.recipient)} · 核算截至 ${esc(r.payload.as_of)}</summary>${contractPanels[i]}${renderCalc(r.payload)}<button data-business-event="payment" data-parcel-id="${esc(r.parcel_id)}" data-publication="${r.id}">补录并确认缴费记录</button></details>`).join('')||'<p>暂无已确认台账。</p>';
 await loadRateForm();bindBusinessEvents();bindApprovalActions();
 }
 if(name==='notices'){
 await loadTemplatePanel('notice');
 host.innerHTML=rows.filter(r=>Number(r.payload.arrears)>0).map(r=>`<div class="card"><b>${esc(r.payload.recipient)} · ${esc(r.payload.address)}</b><p>欠缴 ${esc(r.payload.arrears)} 元 · 核算截至 ${esc(r.payload.as_of)}</p><button data-make-notice="${r.id}">生成通知草稿</button>${parcelLinks(r)}</div>`).join('')||'<p>暂无欠缴对象。</p>';
 document.querySelectorAll('[data-make-notice]').forEach(b=>b.onclick=()=>action(async()=>{const n=await api('/api/notices/'+b.dataset.makeNotice,{});$('#notice-preview').innerHTML=`<div class="card"><h3>待审核短信</h3><p>${esc(n.sms_draft)}</p><a href="/api/notices/${n.id}/document">下载通知书</a></div>`;await loadNotices()}));await loadNotices();
 }
 if(name==='contracts'){
 await loadTemplatePanel('contract');
 $('#contract-candidates').innerHTML=rows.map(r=>`<div class="card" data-parcel="${esc(r.parcel_id)}"><b>${esc(r.payload.address)} · ${esc(r.payload.recipient)}</b><div class="actions"><button data-existing-new="${esc(r.parcel_id)}">起草新签合同</button><button data-renew-parcel="${esc(r.parcel_id)}">起草续期合同</button><button data-change-doc="${esc(r.parcel_id)}">上传变更协议</button><button data-business-event="termination" data-parcel-id="${esc(r.parcel_id)}" data-publication="${r.id}">登记终止</button></div></div>`).join('');
 await loadContractDrafts(host);bindBusinessEvents();
 }
 bindBusinessLinks();applyPermissions();SelectControls.enhance();
 }catch(e){host.textContent='加载失败：'+e.message;throw e}
}
function bindBusinessLinks(){
 document.querySelectorAll('[data-open-parcel]').forEach(b=>b.onclick=()=>action(async()=>{await tab('ledger');const el=[...document.querySelectorAll('#ledger-content [data-parcel]')].find(x=>x.dataset.parcel===b.dataset.openParcel);el?.focus();el?.scrollIntoView({block:'start'})}));
 document.querySelectorAll('[data-existing-new]').forEach(b=>b.onclick=()=>action(()=>createContract(b.dataset.existingNew,'新签')));
 document.querySelectorAll('[data-renew-parcel]').forEach(b=>b.onclick=()=>action(()=>createContract(b.dataset.renewParcel)));
 document.querySelectorAll('[data-evidence-doc]').forEach(b=>b.onclick=()=>action(()=>openDoc(b.dataset.evidenceDoc)));
 document.querySelectorAll('[data-change-doc]').forEach(b=>b.onclick=()=>action(async()=>{await tab('inbox');$('#intake').open=true;$('#name').value='变更协议';$('#files').focus();toast('上传协议后，在合同确认中核实原合同编号与宗地号，预览后确认生效。')}));
}
async function createContract(parcel='',kind=parcel?'续期':'新签'){
 const f=[{name:'start',label:'租赁起始日',type:'date',required:true},{name:'end',label:'租赁终止日',type:'date',required:true},{name:'contract_no',label:'新合同编号',required:true}];
 if(!parcel)for(const [name,label] of [['tenant','承租人'],['identity','证件号码'],['phone','联系电话'],['parcel_no','宗地号'],['address','坐落'],['area','面积（平方米）'],['use','用途'],['grade','土地级别'],['mode','计租方式']])f.push({name,label,required:true,...({use:['住宅','商服','工业','仓储'],grade:['一级','二级','三级','四级'],mode:['固定','随标准调整']}[name]?{options:({use:['住宅','商服','工业','仓储'],grade:['一级','二级','三级','四级'],mode:['固定','随标准调整']}[name]).map(v=>({value:v,label:v}))}:{})});
 const v=await requestDialog({title:kind==='续期'?'起草续期合同':'起草新签合同',description:'按当前选用的合同模板生成草稿。签订材料须另行上传核验后入账。',fields:f,submit:'生成草稿'});if(!v)return;
 const draft=await api('/api/contracts/draft',{parcel_id:parcel,kind,start:v.start,end:v.end,contract_no:v.contract_no,fields:v});await tab('review');toast('草稿已保存至合同确认，尚未归档');await previewContractDraft(draft.id);
}
async function loadReminders(){
 const day=$('#reminder-date').value||new Date().toLocaleDateString('sv-SE');$('#reminder-date').value=day;
 const start=$('#reminder-start').value,end=$('#reminder-end').value;
 if(start&&end&&start>end)throw Error('范围起始日不能晚于结束日');
 const q=new URLSearchParams({today:day,start,end,kind:$('#reminder-kind').value,level:$('#reminder-level').value});
 const rows=await api('/api/reminders?'+q);$('#reminder-export').href='/api/reminders?'+q+'&format=ics';
 $('#reminder-date-note').textContent=`提醒查看日期 ${day}；范围 ${start||'不限'} 至 ${end||'不限'}（按提醒事项日期筛选）；金额使用已确认台账的核算截至日，不随查看日期自动重算。`;
 $('#reminder-list').innerHTML=rows.map(r=>`<div class="card"><b>${esc(r.level)} · ${esc(r.kind)} · ${esc(r.date)}</b><p>${esc(r.recipient)} · ${esc(r.address)} · ${esc(r.message)}</p>${parcelLinks(r)}<button data-remind-notice="${r.publication}">办理催缴</button></div>`).join('')||'<p>暂无提醒。</p>';
 const [year,month]=day.split('-').map(Number),days=new Date(year,month,0).getDate(),offset=(new Date(year,month-1,1).getDay()+6)%7;
 $('#reminder-calendar').innerHTML=`<h3>${year}年${month}月</h3><div class="calendar-grid">${['一','二','三','四','五','六','日'].map(x=>`<b>${x}</b>`).join('')}${'<div></div>'.repeat(offset)}${Array.from({length:days},(_,i)=>{const d=`${year}-${String(month).padStart(2,'0')}-${String(i+1).padStart(2,'0')}`;return `<div class="calendar-day"><b>${i+1}</b>${rows.filter(r=>r.date===d).map(r=>`<span class="calendar-event">${esc(r.kind)} ${esc(r.recipient)}</span>`).join('')}</div>`}).join('')}</div>`;
 bindBusinessLinks();document.querySelectorAll('[data-remind-notice]').forEach(b=>b.onclick=()=>action(async()=>{await tab('notices');const el=$(`[data-make-notice="${b.dataset.remindNotice}"]`);el?.focus();el?.scrollIntoView();if(!el)toast('该对象当前无有效欠缴，无需催缴')}));
}
async function loadStatistics(){
 const q=new URLSearchParams({street:$('#s-street').value,status:$('#s-status').value,arrears:$('#s-arrears').value,keyword:$('#s-keyword').value});
 const [s,rows]=await Promise.all([api('/api/stats?'+q),api('/api/ledger?'+q)]);$('#statistics-export').href='/api/stats?'+q+'&format=csv';
 $('#stats-panel').innerHTML=Object.entries(s).filter(([,v])=>typeof v!=='object').map(([k,v])=>`<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
 $('#statistics-detail').innerHTML=`<p>累计收缴率 = 累计实缴 ${esc(s['累计实缴'])} ÷ 累计应缴 ${esc(s['累计应缴'])}；零应缴显示0%。多缴可能使收缴率超过100%。</p>`+businessTable('分街路统计',[['street','街路'],['宗地数','宗地数'],['应缴','应缴'],['实缴','实缴'],['欠缴','欠缴']],Object.entries(s['按街路']).map(([street,v])=>({street,...v})))+rows.map(r=>`<p>${esc(r.payload.address)} · ${esc(r.payload.recipient)} · 欠缴 ${esc(r.payload.arrears)} ${parcelLinks(r)}</p>`).join('');bindBusinessLinks();
}
function bindNoticeSend(){
 document.querySelectorAll('[data-notice-tasks]').forEach(b=>b.onclick=()=>action(async()=>{const tasks=await api(`/api/notices/${b.dataset.noticeTasks}/tasks`);await requestDialog({title:'任务与回执',description:'发送任务及通道返回结果。模拟任务不会发送真实短信。',content:tasks.map(t=>`<p>${esc(t.id)} · ${esc(t.status)} · ${stamp(t.created)}</p>${businessTable('回执',[['id','回执号'],['payload','结果']],t.receipts)}`).join('')||'<p>尚无任务。</p>',submit:'关闭'});}));
 document.querySelectorAll('[data-sim-receipt]').forEach(b=>b.onclick=()=>action(async()=>{const v=await requestDialog({title:'补录模拟回执',description:'仅用于验证结果未知及重复回执的处理。',fields:[{name:'receipt_id',label:'模拟回执号',required:true},{name:'status',label:'结果',options:[{value:'success',label:'成功'},{value:'failure',label:'失败'}],required:true}],submit:'登记回执'});if(!v)return;await api(`/api/notices/${b.dataset.simReceipt}/simulation-receipt`,v);await loadNotices()}));
 document.querySelectorAll('[data-send-notice]').forEach(b=>b.onclick=()=>action(async()=>{const v=await requestDialog({title:'模拟短信发送',description:'不会发送真实短信。结果未知时禁止盲目重发；同一通知重复请求返回原任务。',fields:[{name:'outcome',label:'模拟结果',options:[{value:'success',label:'成功'},{value:'failure',label:'失败'},{value:'timeout',label:'超时，结果未知'}],required:true}],submit:'提交模拟任务'});if(!v)return;const task=await api(`/api/notices/${b.dataset.sendNotice}/send`,{channel:'simulation',outcome:v.outcome});toast(task.status);await loadNotices()}))}
function bindBusinessEvents(){document.querySelectorAll('[data-business-event]').forEach(b=>b.onclick=()=>action(async()=>{
 const kind=b.dataset.businessEvent,fields=[{name:'evidence_document',label:'选择已核对的依据材料',options:(state.documents||[]).filter(d=>['needs_human','published'].includes(d.state)).map(d=>({value:d.id,label:d.name})),required:true},{name:'date',label:kind==='payment'?'缴费日期':'终止生效日（当天为最后计租日）',type:'date',required:true},{name:'reason',label:'核验依据与说明',type:'textarea',required:true}];
 if(kind==='payment')fields.push({name:'amount',label:'实缴金额',type:'number',required:true},{name:'year',label:'抵扣年度（多年度请分记录）',type:'number',required:true},{name:'receipt',label:'票据号',required:true});
 const v=await requestDialog({title:kind==='payment'?'确认补录缴费':'登记终止',description:'先在存量合同治理与新合同上传中上传凭证，查看原件核验。此处选择对应材料。提交后预览逐年金额变化，复核后生效。',fields,submit:'预览影响'});if(!v)return;
 const body={...v,kind,evidence_confirmed:true,publication:Number(b.dataset.publication)};const p=await api(`/api/parcels/${b.dataset.parcelId}/events/preview`,body);
 $('#'+(kind==='payment'?'rent-content':'contract-candidates')).insertAdjacentHTML('afterbegin',`<div class="card"><h3>待确认差异</h3>${renderCalc(p.before.payload)}${renderCalc(p.after)}</div>`);
 if(!await requestDialog({title:'确认逐年核算差异',description:`应缴 ${p.before.payload.due} → ${p.after.due}；实缴 ${p.before.payload.paid} → ${p.after.paid}；欠缴 ${p.before.payload.arrears} → ${p.after.arrears}。逐年明细已显示在页面中，取消可返回检查。`,submit:'复核无误，确认生效',content:renderCalc(p.after)}))return;
 await api(`/api/parcels/${b.dataset.parcelId}/events/confirm`,{...body,preview_hash:p.preview_hash,confirmed:true});await loadBusinessPage(activeTab);toast('已确认并重算，历史记录已保留');
}))}
async function loadRateForm(){
 const r=await api('/api/rules');$('#rates-json').value=JSON.stringify(r.rates,null,2);
 $('#rates-form').innerHTML=`<label>舍入口径<select id="policy-rounding"><option value="half_up">按口径四舍五入到分</option><option value="answer_key">比赛答案兼容舍入（需确认依据）</option></select></label><div class="table-scroll rates-grid"><table><thead><tr>${['执行起始日','用途','一级','二级','三级','四级'].map(k=>`<th>${k}</th>`).join('')}</tr></thead><tbody>${r.rates.map((v,i)=>`<tr>${['执行起始日','用途','一级','二级','三级','四级'].map(k=>`<td><input data-rate-row="${i}" data-rate-key="${k}" aria-label="第${i+1}行${k}" value="${esc(v[k])}"></td>`).join('')}</tr>`).join('')}</tbody></table></div><p>规则版本 ${esc(r.active)}。分段：按自然月；减免：来源批准文号；存疑：按产权人有利算法暂算。以上执行现有06口径，尚不支持自定义算法。</p>`;
 $('#policy-rounding').value=r.policy.rounding;$('#policy-rounding').onchange=()=>{$('#rule-calculation-clear').checked=false;ruleDraft=null;updateRequirements()};
 document.querySelectorAll('[data-rate-row]').forEach(el=>el.oninput=()=>{r.rates[+el.dataset.rateRow][el.dataset.rateKey]=el.value;$('#rates-json').value=JSON.stringify(r.rates,null,2);$('#confirm-rules').disabled=true});
}
document.addEventListener('DOMContentLoaded',()=>{
 document.querySelectorAll('[data-sub]').forEach(b=>b.onclick=()=>action(async()=>{
   if(!await tab(b.dataset.sub))return;
   if(b.dataset.sub==='inbox'){
     documentSearch='';$('#document-search').value='';
     setDocumentFilter(b.dataset.documentFilter||'');
   }
 },b));
 $('#new-contract').onclick=()=>action(()=>createContract());$('#reminder-filter').onsubmit=e=>{e.preventDefault();action(loadReminders)};$('#statistics-filter').onsubmit=e=>{e.preventDefault();action(loadStatistics)};
});

async function loadTemplatePanel(kind){
 const panel=$('#'+(kind==='contract'?'contract':'notice')+'-template-panel');
 const info=await api('/api/templates/'+kind);
 const title=kind==='contract'?'合同模板':'催缴通知模板';
 panel.innerHTML=`<h3>${title}</h3><p>当前：${esc(info.name)} <span class="tag">${esc(info.source)}</span></p><p class="caption">内置评测集模板。导入 DOCX 后用于后续生成，已归档文书保持原样。必需占位符：${info.required_placeholders.map(k=>`【${esc(k)}】`).join('、')}。支持占位符可参照预置模板。</p><div class="actions"><a class="button" href="/api/templates/${kind}/document">下载当前模板</a><a class="button" href="/api/templates/${kind}/document?builtin=true">下载评测集预置</a></div><form class="template-actions" data-template-admin><label for="template-file-${kind}">上传新模板／重新导入（DOCX，≤5 MB）<input id="template-file-${kind}" type="file" accept=".docx" required></label><button type="submit">导入并使用</button><button type="button" data-template-restore>恢复评测集预置</button></form><p class="caption">模板导入与恢复由管理员操作。</p><p data-template-feedback role="status"></p>`;
 panel.querySelector('form').onsubmit=e=>{e.preventDefault();action(async()=>{
   const file=panel.querySelector('input').files[0];if(!file)throw Error('请选择 DOCX 模板');
   const data=new FormData();data.append('file',file);
   await api('/api/templates/'+kind,data);await loadTemplatePanel(kind);toast('模板已导入，后续生成使用新模板');
 },e.currentTarget)};
 panel.querySelector('[data-template-restore]').onclick=e=>action(async()=>{await api(`/api/templates/${kind}/restore`,{});await loadTemplatePanel(kind);toast('已恢复评测集预置模板')},e.currentTarget);
 applyPermissions();
}

async function loadContractDrafts(host=$('#generated-contracts-review')){
 const list=await api('/api/contracts');
 host.innerHTML=`<h2>待确认合同草稿</h2><p class="caption">起草后先预览核对；签订材料上传后，按原件核验和租金确认流程登记归档。</p>`+(list.map(r=>`<div class="card"><div class="section-head"><h3>${esc(r.kind)} · ${esc(r.values['承租人'])} · ${esc(r.values['合同编号'])}</h3><span class="tag">待确认 · 未归档</span></div><p>起草人：${esc(r.actor)} · ${stamp(r.created)}</p><div class="actions"><button data-preview-contract="${esc(r.id)}">预览草稿</button><a class="button" href="/api/contracts/${encodeURIComponent(r.id)}/document">下载草稿</a><button data-upload-signed="${esc(r.id)}">上传签订材料</button></div></div>`).join('')||'<p class="muted">暂无待确认合同草稿。</p>');
 host.querySelectorAll('[data-preview-contract]').forEach(b=>b.onclick=()=>action(()=>previewContractDraft(b.dataset.previewContract)));
 host.querySelectorAll('[data-upload-signed]').forEach(b=>b.onclick=()=>action(async()=>{const r=list.find(r=>r.id===b.dataset.uploadSigned);await tab('inbox');$('#intake').open=true;$('#name').value=`${r.kind}合同 · ${r.values['合同编号']}`;$('#files').focus();toast('请上传签订材料，核对原件及租金后确认登记')}));
 applyPermissions();
}
async function previewContractDraft(id){
 const r=await api(`/api/contracts/${encodeURIComponent(id)}`);
 await requestDialog({title:`${r.kind}合同草稿预览`,description:'待确认，尚未归档。以下为草稿要素；完整合同请下载核对。',content:businessTable('合同草稿要素',[['key','字段'],['value','内容']],Object.entries(r.values).map(([key,value])=>({key,value})))+`<a class="button" href="/api/contracts/${encodeURIComponent(r.id)}/document">下载完整草稿</a>`,submit:'关闭预览'});
}

const approvalLabels={pending:'待管理员审批',returned:'已退回审核员',archived:'已确认归集入档'};
async function loadApprovalDashboard(){
 const rows=await api('/api/approvals');
 state.approvals=rows;
 $('#approval-dashboard').innerHTML='<h3>合同审批看板</h3>'+businessTable('办理情况',[['label','状态'],['count','合同数']],Object.entries(approvalLabels).map(([key,label])=>({label,count:rows.filter(r=>r.status===key).length})))+rows.filter(r=>r.status!=='archived').map(r=>`<p><button data-approval-open="${esc(r.document_id)}">${esc(r.name)}</button> · ${esc(approvalLabels[r.status])} · ${esc(r.reason)}</p>`).join('');
 document.querySelectorAll('[data-approval-open]').forEach(b=>b.onclick=()=>action(()=>openDoc(b.dataset.approvalOpen)));
 renderApprovalState();
}
function renderApprovalState(){
 const row=(state.approvals||[]).find(r=>r.document_id===doc?.id),admin=$('#role').value==='admin';
 $('#confirm').textContent=admin&&row?.status==='pending'?'审批通过并确认归集入档':'确认登记';
 $('#approval-state').textContent=row?`${approvalLabels[row.status]} · ${row.reason}`:'一般合同由审核员核对确认；需要上级审批的事项在此提交。';
 $('#submit-approval').disabled=operationBusy||!doc||$('#role').value==='viewer'||['published','withdrawn'].includes(doc?.state)||row?.status==='pending';
 $('#check-approval').hidden=!admin;$('#check-approval').disabled=operationBusy||!doc;
 $('#return-approval').hidden=!admin;$('#return-approval').disabled=operationBusy||row?.status!=='pending';
 if(row?.status==='pending')$('#confirm').disabled=$('#confirm').disabled||!admin;
 if(row?.status==='returned')$('#confirm').disabled=true;
}
function renderParcelCheck(check){
 if(!check)return '<p>尚未核查。</p>';
 return `<p class="notice">模型辅助核查：${esc(check.model_review?.message||'尚无模型建议')}</p><p>${check.stale?'资料已变化，请重新核查。':'本次核查已完成，待人工复核。'} · ${esc(check.model_status)}</p><p>${esc(check.note)}</p>`+businessTable('模型候选与人工确认差异',[['document','合同'],['field','字段'],['confirmed','人工确认'],['candidates','模型候选']],check.differences||[])+businessTable('跨来源核查提示',[['type','事项'],['reason','核查结果']],check.issues||[]);
}
async function rentContractPanel(row){
 const [contracts,check]=await Promise.all([api(`/api/parcels/${row.parcel_id}/confirmed-contracts`),api(`/api/parcels/${row.parcel_id}/check`)]);
 return businessTable('收款匹配',[['payments','收款记录'],['contract','对应合同'],['confirmed','合同是否已确认']],contracts.map(c=>({payments:c.payment_ids.join('、')||'尚未关联收款记录',contract:c.fields.contract_no||c.name,confirmed:c.confirmed})))+
 '<p class="caption">预加载人工确认后的合同数据。修改生成修订稿，复核入档后更新核算，历史版本保留。</p>'+contracts.map(c=>`<button data-evidence-doc="${esc(c.id)}">查看 ${esc(c.fields.contract_no||c.name)}</button><button data-contract-revision="${esc(c.id)}" data-version="${c.version}" ${$('#role').value==='admin'&&c.confirmed?'':'disabled'}>修改复核</button>`).join('')+`<h3>宗地核查结果</h3><button data-parcel-check="${esc(row.parcel_id)}" ${$('#role').value==='admin'?'':'disabled'}>一键核查</button><div data-check-result="${esc(row.parcel_id)}">${renderParcelCheck(check)}</div>`;
}
function bindApprovalActions(){
 document.querySelectorAll('[data-contract-revision]').forEach(b=>b.onclick=()=>action(async()=>{const r=await api(`/api/documents/${b.dataset.contractRevision}/revision`,{version:Number(b.dataset.version)});await refresh();await openDoc(r.id,true);toast('已预填人工确认数据，请修改并重新预览、复核入档')}));
 document.querySelectorAll('[data-parcel-check]').forEach(b=>b.onclick=()=>action(async()=>{const result=await api(`/api/parcels/${b.dataset.parcelCheck}/check`,{});b.nextElementSibling.innerHTML=renderParcelCheck(result);toast('核查完成，结果已列在本宗地后')}));
}
document.addEventListener('DOMContentLoaded',()=>{
 $('#check-approval').onclick=()=>action(async()=>{if(!['published','withdrawn'].includes(doc.state))await api(`/api/documents/${doc.id}/draft`,body());const check=await api(`/api/documents/${doc.id}/check`,{});$('#approval-check-result').innerHTML=renderParcelCheck(check)});
 for(const [id,decision] of [['submit-approval','submit'],['return-approval','return']])$('#'+id).onclick=()=>action(async()=>{
  const v=await requestDialog({title:decision==='submit'?'提交合同管理员审批':'退回审核员',fields:[{name:'reason',label:decision==='submit'?'申请事项与依据':'退回原因与补正要求',type:'textarea',required:true}],submit:'提交'});if(!v)return;
  if(decision==='submit')await api(`/api/documents/${doc.id}/draft`,body());
  await api(`/api/documents/${doc.id}/approval`,{version:doc.version,decision,reason:v.reason});dirty=false;await loadApprovalDashboard();toast('办理结果已保存');
 });
});
window.addEventListener('fde-role-changed',()=>{renderApprovalState();document.querySelectorAll('[data-contract-revision],[data-parcel-check]').forEach(b=>b.disabled=$('#role').value!=='admin')});
