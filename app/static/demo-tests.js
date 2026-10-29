/* Explicit demo space: no automatic verification, no hidden live fallback. */
(()=>{
  let status=null,lastProgress='',loading=false;
  const active=r=>r&&['queued','running','reporting'].includes(r.state);
  const names={practice:'练习题',evaluation:'评测题'};
  const percent=value=>value==null?'N/A':(100*value).toFixed(2)+'%';
  const states={queued:'等待开始',running:'导入与识别中',reporting:'生成报告中',completed:'识别处理结束',partial:'部分失败或未完成',failed:'运行失败',interrupted:'进程中断，需核对'};
  function selected(){return status?.runs.find(r=>r.id===status.selected)}
  function permissions(){
    const busy=status?.runs.some(active)||operationBusy,readOnly=$('#role').value==='viewer';
    $('#demo-practice').disabled=busy||readOnly;$('#demo-evaluation').disabled=busy||readOnly;
    $('#demo-reset').disabled=busy||readOnly||!selected()||selected().archived;
    $('#demo-mode').disabled=busy||readOnly;
  }
  function render(){
    const run=selected();$('#demo-formal').hidden=!run;
    $('#demo-space-label').textContent=run?`测试空间 · ${names[run.dataset]} · ${run.id} · ${run.mode==='replay'?'历史缓存回放，非新OCR':'实时云识别'} · 正式合同不在此空间`:'正式业务空间 · 测试将使用独立数据空间';
    $('#demo-global-space').hidden=!run;
    $('#demo-global-space').textContent=run?`当前是测试空间（${names[run.dataset]} / ${run.id}），所有合同、核算和草稿仅用于演示。返回合同管理可切回正式业务空间。`:'';
    const running=status.runs.find(active);
    $('#demo-progress').textContent=running?`${names[running.dataset]}：${states[running.state]} · 已处理 ${running.progress?.processed||0}/${running.expected} 份 · 第 ${running.progress?.batch||0} 批（每批5份）。可刷新页面继续查看。`:run?`${states[run.state]||run.state} · ${run.progress?.processed||0}/${run.expected} 份${run.error?' · '+run.error:''}`:'尚未启动测试。重复点击已有同配置运行会显示原结果；要重新演示，请先归档重置该测试空间。';
    $('#demo-history').innerHTML=status.runs.length?status.runs.map(r=>`<p><b>${names[r.dataset]}</b> · ${r.mode==='replay'?'缓存回放':'实时'} · ${states[r.state]||esc(r.state)} · ${esc(r.id)} ${r.archived?'<span class="tag">已归档，可恢复</span>':''} ${r.has_report?`<a href="/api/demo/${r.id}/report?format=md" download>下载报告</a>`:''} <button data-demo-${r.archived?'restore':'activate'}="${r.id}">${r.archived?'恢复测试数据':'查看测试空间'}</button></p>`).join(''):'暂无测试历史。';
    $('#demo-history').querySelectorAll('[data-demo-activate]').forEach(b=>b.onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;await api(`/api/demo/${b.dataset.demoActivate}/activate`,{});reload()}));
    $('#demo-history').querySelectorAll('[data-demo-restore]').forEach(b=>b.onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;await api(`/api/demo/${b.dataset.demoRestore}/restore`,{});reload()}));
    permissions();
  }
  function reload(){location.hash='view=inbox';location.reload()}
  async function load(){
    if(loading)return;loading=true;
    try{
      status=await api('/api/demo/status');render();
      const run=selected(),signature=run?`${run.id}/${run.updated}/${run.state}`:'';
      if(signature!==lastProgress){lastProgress=signature;
        if(run?.has_report){const report=await api(`/api/demo/${run.id}/report`);showReport(report)}
        else $('#demo-report').innerHTML='';
        if(run&&!operationBusy&&!dirty)await refresh();
      }
    }catch(error){$('#demo-progress').textContent='测试状态读取失败：'+error.message}
    finally{loading=false}
  }
  function showReport(r){
    const a=r.actual,s=r.scoring,strict=s.available?`${s.strict.correct}/${s.strict.denominator}（${(100*s.strict.accuracy).toFixed(2)}%）`:'N/A';
    $('#demo-report').innerHTML=`<div class="context-box"><h3>本次测试报告</h3><p>${r.mode==='live'?'实时识别尝试（本次请求'+r.external_calls_new+'次）':'历史冻结响应回放，本次没有新OCR'} · ${esc(r.elapsed_seconds)} 秒 · 识别完成率 ${(100*r.completion_rate).toFixed(2)}%</p><p>应处理 ${r.expected.documents_expected} 份 / ${r.expected.pages_expected} 页；导入 ${a.documents_imported} 份 / ${a.pages_imported} 页；重复 ${a.duplicate_imports}；待核验 ${a.pending_verification}；发布 ${a.published}。</p><p>实际终态：${esc(JSON.stringify(a.recognition_states))}</p><p><b>严格字段正确率：${strict}</b></p>${s.available?`<p>唯一输出精度：${percent(s.precision.accuracy)} · 覆盖率：${percent(s.coverage.accuracy)} · 全字段完整正确合同：${s.whole_contract.all_15_correct}/${r.expected.documents_expected}</p><p>系统自测评分：${s.score_100}/${s.maximum}。${esc(s.formula)}。不是官方竞赛综合分。</p><p>独立格式比较：${s.normalized_format.correct}/${s.normalized_format.denominator}，不计作OCR收益。</p>`:`<p>${esc(s.reason)}</p>`}<p>${esc(r.notice)}</p><div class="actions"><a href="/api/demo/${r.run_id}/report?format=md" download>下载测试报告（Markdown）</a><a href="/api/demo/${r.run_id}/report" target="_blank" rel="noopener">查看指标JSON</a></div></div>`;
  }
  for(const dataset of ['practice','evaluation'])$('#demo-'+dataset).onclick=()=>action(async()=>{
    if(await guardUnsaved()==='cancel')return;
    await api('/api/demo/start',{dataset,mode:$('#demo-mode').value});reload();
  });
  $('#demo-formal').onclick=()=>action(async()=>{if(await guardUnsaved()==='cancel')return;await api('/api/demo/formal',{});reload()});
  $('#demo-reset').onclick=()=>action(async()=>{
    const run=selected();if(!run)return;
    const plan=await api(`/api/demo/${run.id}/reset-plan`);
    const counts=plan.counts;
    const accepted=await requestDialog({title:'重置本次测试数据',description:`将归档 ${names[run.dataset]} 的 ${counts.documents} 份文档、${counts.review_drafts} 份草稿、${counts.publications} 条台账快照和 ${counts.notices} 条通知。${plan.effect}。`,submit:'确认归档测试数据',danger:true});
    if(!accepted)return;
    await api(`/api/demo/${run.id}/reset`,{token:plan.token,confirm:true});reload();
  });
  $('#role').addEventListener('change',permissions);
  load();setInterval(load,2000);
})();
