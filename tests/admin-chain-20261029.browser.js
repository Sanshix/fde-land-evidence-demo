async page=>{
 const results=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 const assert=(name,ok)=>{results.push({name,passed:!!ok});if(!ok)throw Error(name)};
 const settle=()=>page.waitForFunction(()=>!operationBusy);
 const click=async s=>{await page.locator(s).click();await settle()};
 const choose=async(s,v)=>page.locator(s).selectOption(v,{force:true});
 await choose('#role','admin');await settle();
 for(const kind of ['contract','notice']){
   await click(`[data-tab="${kind==='contract'?'contracts':'notices'}"]`);
   const raw=await(await page.request.get(`http://127.0.0.1:8882/api/templates/${kind}/document?builtin=true`)).body();
   await page.locator('#template-file-'+kind).setInputFiles({name:'automated-'+kind+'.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:raw});
   await click(`#${kind}-template-panel form button[type="submit"]`);
   assert('template_import_'+kind,(await page.locator('#'+kind+'-template-panel').innerText()).includes('已导入模板'));
   await click(`#${kind}-template-panel [data-template-restore]`);
   assert('template_restore_'+kind,(await page.locator('#'+kind+'-template-panel').innerText()).includes('评测集预置'));
 }
 await click('[data-tab="rent"]');await choose('#policy-rounding','answer_key');await page.locator('#rule-reason').fill('合成验收舍入配置切换');
 await click('#preview-rules');assert('rule_requires_reconfirmation',await page.locator('#confirm-rules').isDisabled());
 await page.locator('#rule-calculation-clear').check();await page.locator('#confirm-rules').click();await page.locator('#action-dialog[open]').waitFor();await click('#action-submit');
 const rule=await(await page.request.get('http://127.0.0.1:8882/api/rules')).json();assert('rule_version_persisted',rule.policy.rounding==='answer_key');
 await click('[data-tab="measure"]');assert('admin_backup_visible',await page.locator('#backup').isVisible());
 await click('#backup');assert('ui_backup_created',(await page.locator('#backup-result').innerText()).includes('path'));
 await choose('#role','viewer');await settle();assert('viewer_backup_hidden',!(await page.locator('#backup').isVisible()));
 await click('[data-tab="inbox"]');await click('#open-upload');assert('viewer_upload_disabled',await page.locator('#upload button').isDisabled());
 await choose('#role','reviewer');await settle();assert('no_runtime_errors',errors.length===0);
 return{results,errors};
}
