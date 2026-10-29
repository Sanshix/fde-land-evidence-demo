// Explicit loading preserves unsaved settings while the workbench refreshes.
window.addEventListener('DOMContentLoaded',()=>{
  let settings=null;
  const form=document.querySelector('#model-settings-form');
  const message=document.querySelector('#model-settings-message');
  const model=document.querySelector('#reasoning-model');
  const deployment=document.querySelector('#model-deployment');
  const endpoint=document.querySelector('#model-endpoint');
  function describe(){
    const item=settings?.models.find(m=>m.id===model.value);
    document.querySelector('#model-choice-detail').textContent=item?`${item.tier}。${item.note}。仅权重理论占用：BF16 ${item.weight_only_bf16_gb} GB / INT4 ${item.weight_only_int4_gb} GB；另需视觉编码、量化元数据、缓存和运行空间。${item.validation}。`:'';
    document.querySelector('#model-endpoint-label').hidden=deployment.value!=='local';
    endpoint.required=deployment.value==='local';
  }
  function renderWorkflow(value){
    document.querySelector('#workflow-primary').value=value.workflow_primary||'plan3';
    document.querySelector('#workflow-enabled').checked=!!value.enabled;
    document.querySelector('#workflow-message').textContent=value.workflow_primary?'当前工作流已加载。':'当前使用其他模型配置；保存后切换到双模型工作流。';
  }
  async function loadWorkflow(){
    try{settings=await api('/api/model-settings');renderWorkflow(settings)}
    catch(error){document.querySelector('#workflow-message').textContent=error.message}
  }
  document.querySelector('#recognition-workflow-form').onsubmit=async e=>{
    e.preventDefault();const button=e.currentTarget.querySelector('button');button.disabled=true;
    const status=document.querySelector('#workflow-message');status.textContent='正在保存…';
    try{
      if(!settings)throw new Error('尚未加载配置，请刷新后重试');
      settings=await api('/api/model-settings',{version:settings.version,base_hash:settings.base_hash,workflow_primary:document.querySelector('#workflow-primary').value,enabled:document.querySelector('#workflow-enabled').checked});
      renderWorkflow(settings);form.hidden=true;status.textContent='已保存，后续任务使用所选方案；方案3同时向所选复核模型提供原图和OCR全文。';await refresh();
    }catch(error){status.textContent=error.message}
    finally{button.disabled=document.querySelector('#role').value!=='admin'}
  };
  window.addEventListener('fde-role-changed',()=>{if(document.querySelector('#role').value==='admin')loadWorkflow()});
  if(document.querySelector('#role').value==='admin')loadWorkflow();
  function render(value){
    renderWorkflow(value);
    settings=value;
    model.replaceChildren(...value.models.map(m=>new Option(`${m.tier} · ${m.name}`,m.id)));
    const selected=value.selection;
    model.value=selected?.profile.catalog_id||(value.models.some(m=>m.id===value.workflow_primary)?value.workflow_primary:value.models[0].id);
    deployment.value=selected?.deployment||(value.workflow_primary?'aliyun':'local');
    endpoint.value=selected?.deployment==='local'?selected.profile.base_url:'';
    document.querySelector('#model-enabled').checked=selected?.enabled||false;
    form.hidden=false;describe();
    SelectControls.enhance();
  }
  async function run(work){
    const buttons=[...document.querySelectorAll('#model-settings-form button,#load-model-settings')];
    buttons.forEach(b=>b.disabled=true);message.textContent='正在处理…';
    try{await work()}catch(error){message.textContent=error.message}
    finally{buttons.forEach(b=>b.disabled=document.querySelector('#role').value!=='admin')}
  }
  document.querySelector('#load-model-settings').onclick=()=>run(async()=>{
    render(await api('/api/model-settings'));
    message.textContent=settings.base_changed?'部署文件已变化，旧选择已失效。请重新选择。':'配置已加载；保存前请确认服务已部署且模型 ID 匹配。';
  });
  model.onchange=deployment.onchange=describe;
  form.onsubmit=e=>{e.preventDefault();run(async()=>{
    render(await api('/api/model-settings',{version:settings.version,base_hash:settings.base_hash,model:model.value,deployment:deployment.value,endpoint:endpoint.value,enabled:document.querySelector('#model-enabled').checked}));
    message.textContent='已保存，后续开始的识别任务使用此配置；连通性和准确率尚待实测。';await refresh();
  })};
  document.querySelector('#reset-model-settings').onclick=()=>run(async()=>{
    render(await api('/api/model-settings',{version:settings.version,base_hash:settings.base_hash,reset:true}));
    message.textContent='已恢复部署文件中的模型及启用状态。';await refresh();
  });
});
