// Run after workbench.browser.js on its isolated server (worker disabled).
// Synthetic GET fixtures and mocked failures are explicitly separated from real writes.
async page => {
  await page.unrouteAll({behavior:'ignoreErrors'});
  const assert=(ok,message)=>{if(!ok)throw Error(message)};
  const settle=()=>page.waitForFunction(()=>!operationBusy);
  const click=async selector=>{await page.locator(selector).click();await settle()};
  const choose=async (selector,value)=>{await page.locator(selector).evaluate((el,v)=>{el.value=v;el.dispatchEvent(new Event('change',{bubbles:true}))},value);await settle()};
  const origin=new URL(page.url()).origin;
  const json=async path=>(await page.request.get(origin+path)).json();
  const health=await json('/healthz');assert(!health.worker_enabled,'Only run with worker disabled');
  const real=await json('/api/status');assert(real.documents.some(d=>d.state==='published'&&d.name.includes('合成验收'))&&real.documents.every(d=>d.name.startsWith('合成')),'Requires preceding synthetic workflow');
  const publishedDoc=real.documents.find(d=>d.state==='published');
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin+'/#view=inbox');await settle();
  const fixture={...real,documents:Array.from({length:45},(_,i)=>({...real.documents[0],id:'queue-'+i,name:'合成案卷 A'+String(i).padStart(2,'0')+' 很长的历史土地租赁档案名称'.repeat(3),state:'needs_human',updated:1700000000+i}))};
  await page.route('**/api/status',r=>r.fulfill({json:fixture}));await click('#refresh');
  assert(await page.locator('#documents tbody tr').count()===20,'Queue only mounts 20 rows');
  await choose('#document-sort','name');await click('#page-next');
  assert((await page.locator('#documents tbody tr').first().innerText()).includes('A20'),'Client sort and page 2');
  assert(page.url().includes('page=2')&&page.url().includes('sort=name'),'Route stores sort and page');
  await page.reload();await settle();
  assert((await page.locator('#page-info').innerText()).includes('第 2 / 3 页'),'Reload restores page');
  await page.locator('#document-search').fill('A44');
  assert(await page.locator('#documents tbody tr').count()===1,'Keyword filter');
  await page.reload();await settle();assert(await page.locator('#document-search').inputValue()==='A44','Reload restores keyword');
  await page.locator('#document-search').fill('不存在');assert(await page.locator('#documents .empty').isVisible(),'Filtered empty state');
  assert(await page.locator('#page-next').isDisabled(),'No next page for empty state');
  await page.locator('#document-search').fill('');
  await page.locator('#documents button').first().focus();await page.waitForTimeout(5100);
  assert(await page.locator('#documents button').first().evaluate(e=>e===document.activeElement),'Polling preserves focused row');
  await page.unroute('**/api/status');await page.goto(origin+'/#view=inbox');await settle();

  // Create two unique synthetic image documents, then exercise real merge and hold APIs.
  async function importFixture(name,seed){seed+='-'+Date.now();
    const png=await page.evaluate(seed=>{const c=document.createElement('canvas');c.width=800;c.height=1000;const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,800,1000);x.fillStyle='black';x.font='24px sans-serif';x.fillText('SYNTHETIC ARCHIVE TEST '+seed,30,50);return c.toDataURL().split(',')[1]},seed);
    const response=await page.request.post(origin+'/api/import',{headers:{Origin:origin,'X-FDE-Role':'reviewer','X-FDE-Actor':'synthetic-ui-test'},multipart:{name,files:{name:seed+'.png',mimeType:'image/png',buffer:Buffer.from(png,'base64')}}});
    assert(response.ok(),'Fixture import');return (await response.json()).id;
  }
  const target=await importFixture('合成目标文书','target'),source=await importFixture('合成补充页文书','source');
  await click('#refresh');await click(`#documents [data-open="${source}"]`);
  assert(await page.locator('.field-group').count()===4,'Four semantic field groups');
  assert(await page.locator('[data-field]').count()===26,'All 26 fields retained');
  assert(await page.evaluate(()=>Object.keys(body().fields).join()===Object.keys(state.fields).join()),'Submission order retained');
  assert(await page.locator('[data-field="start"]').getAttribute('type')==='date','Native date semantics');
  assert(await page.locator('[data-field="area"]').getAttribute('inputmode')==='decimal','Decimal input semantics');
  await page.locator('[data-field="annual"]').focus();
  assert(await page.locator('[data-field-row="annual"]').evaluate(e=>e.classList.contains('current-field')),'Current field highlight');
  assert((await page.locator('#evidence-live').innerText()).includes('年租金小写'),'Evidence live announcement');
  await page.reload();await settle();assert((await page.locator('#doc-name').innerText()).includes('合成补充页'),'Reload restores document and view');
  await page.locator('details').filter({has:page.locator('#merge-form')}).locator('summary').click();
  await choose('#merge-target',target);
  let mergeWrites=0;await page.route('**/merge-into',async r=>{mergeWrites++;await r.continue()});
  await page.locator('#merge-form button:not(.select-trigger)').click();await page.locator('#action-dialog[open]').waitFor();
  assert((await page.locator('#action-description').innerText()).includes('合成目标文书'),'Merge includes target context');
  await page.keyboard.press('Escape');await settle();assert(mergeWrites===0,'Escape cancels merge without write');
  assert(await page.locator('#merge-form button:not(.select-trigger)').evaluate(e=>e===document.activeElement),'Dialog returns focus');
  await page.locator('#merge-form button:not(.select-trigger)').click();await page.locator('#action-dialog[open]').waitFor();await click('#action-submit');
  assert(mergeWrites===1&&(await page.locator('#doc-name').innerText()).includes('合成目标'),'Merge succeeds exactly once');
  await page.unroute('**/merge-into');
  assert(await page.locator('#files').getAttribute('accept')===await page.locator('#attach-files').getAttribute('accept'),'Supplement and import formats match');

  await page.locator('[data-field="tenant"]').fill('保留的输入');
  await page.route('**/draft',r=>r.fulfill({status:409,json:{detail:'合成冲突：版本已变化'}}));
  await click('#save-draft');
  assert((await page.locator('.field-panel > .action-feedback').innerText()).includes('版本冲突'),'409 offers persistent recovery');
  assert(await page.locator('[data-field="tenant"]').inputValue()==='保留的输入','409 retains draft');
  await page.unroute('**/draft');await page.locator('#reason').fill('合成测试：等待补齐清晰原件');await click('#hold');
  assert((await page.locator('#doc-meta').innerText()).includes('待补件'),'Real API hold');
  assert(await page.locator('#confirm').isDisabled(),'Held document cannot register');

  // Timeout unit is injected through the existing API wrapper, never sent to real endpoints.
  let timeouts=0;
  await page.route('**/api/ui-timeout',async r=>{timeouts++;await new Promise(resolve=>setTimeout(resolve,120));await r.fulfill({json:{ok:true}}).catch(()=>{})});
  const message=await page.evaluate(()=>api('/api/ui-timeout',{},undefined,40).then(()=>'',e=>e.message));
  assert(message.includes('可能已接收')&&timeouts===1,'Timeout explains ambiguous write and never retries');
  await page.unroute('**/api/ui-timeout');

  // Real renewal generation; failed form validation must not send a request.
  await click('[data-tab="ledger"]');let renewals=0;
  await page.route('**/api/contracts/draft',async r=>{renewals++;await r.continue()});
  await page.locator('[data-contract]').first().click();await page.locator('#action-dialog[open]').waitFor();
  await page.locator('#dialog-start').fill('2027-12-31');await page.locator('#dialog-end').fill('2027-01-01');await page.locator('#action-submit').click();
  assert(await page.locator('#action-error').isVisible()&&renewals===0,'Reversed renewal dates rejected before request');
  await page.locator('#dialog-start').fill('2027-01-01');await page.locator('#dialog-end').fill('2027-12-31');await click('#action-submit');
  assert(renewals===1&&(await page.locator('#notice-preview').innerText()).includes('续期合同草稿已归档'),'Real renewal draft archived');await page.unroute('**/api/contracts/draft');
  await click('[data-notice]');
  await choose('#role','viewer');
  for(const selector of ['[data-notice]','[data-contract]','[data-notice-status]','#tax-push'])assert(await page.locator(selector).first().isDisabled(),'Viewer cannot write: '+selector);
  await click('[data-tab="review"]');
  for(const selector of ['#merge-form button:not(.select-trigger)','#attach-form button','#retry','#save-draft','#hold','#withdraw','#confirm','[data-cite]'])assert(await page.locator(selector).first().isDisabled(),'Viewer cannot mutate review: '+selector);
  await click('[data-tab="governance"]');assert(await page.locator('#batch-confirm').isDisabled()&&await page.locator('#batch-reviewed').isDisabled(),'Viewer cannot batch-confirm');
  await choose('#role','reviewer');await click('[data-tab="ledger"]');
  const noticeId=await page.locator('[data-notice-status]').first().getAttribute('data-notice-status');
  await page.locator('[data-notice-status]').first().evaluate(el=>{el.value='有异议';el.dispatchEvent(new Event('change',{bubbles:true}))});
  await page.locator('#action-dialog[open]').waitFor();await page.locator('#dialog-note').fill('合成验收：承租人提出金额复核申请');await click('#action-submit');
  const notices=await json('/api/notices');assert(notices.find(n=>String(n.id)===noticeId).events.some(e=>e.note.includes('金额复核')),'Notice opinion persisted');

  const ledgers=await json('/api/ledger');await page.evaluate(data=>window.archiveLedgerFixture=data,ledgers);
  const noArrears=ledgers.map(r=>({...r,payload:{...r.payload,arrears:'0.00'}}));
  await page.route('**/api/ledger?*',r=>r.fulfill({json:noArrears}));await click('#ledger-filter button:not(.select-trigger)');
  for(const role of ['reviewer','viewer','admin','reviewer']){await choose('#role',role);assert(await page.locator('[data-notice]').first().isDisabled(),'No arrears stays disabled for '+role)}
  await page.unroute('**/api/ledger?*');

  // Partial batch response is mocked; assert persistent per-document outcomes.
  const batchRows=[target,source].map((id,i)=>({id,name:'合成批量材料 '+i,eligible:true,version:1,draft_hash:'synthetic',fields:{tenant:'合成用户'},reasons:[]}));
  await page.route('**/api/batch-candidates',r=>r.fulfill({json:batchRows}));
  await page.route('**/api/batch-confirm',r=>r.fulfill({json:{published:1,results:[{id:target,published:true},{id:source,published:false,reason:'合成冲突：证据版本变化，请逐项核对'}]}}));
  await click('[data-tab="governance"]');await page.locator('#batch-reviewed').check();await page.locator('#batch-confirm').click();await page.locator('#action-dialog[open]').waitFor();await click('#action-submit');
  assert((await page.locator('#batch-results').innerText()).includes('合成冲突')&&(await page.locator('#batch-results tbody tr').count())===2,'Partial batch results persist per document');
  await page.unroute('**/api/batch-confirm');await page.unroute('**/api/batch-candidates');

  // Real withdrawal with an explicit reason, then verify retained source/history.
  await page.evaluate(async id=>{await action(()=>openDoc(id))},publishedDoc.id);await settle();
  await page.locator('#reason').fill('合成验收：撤回测试数据');
  await page.locator('.secondary-actions').filter({has:page.locator('#withdraw')}).locator('summary').click();
  await page.locator('#withdraw').click();await page.locator('#action-dialog[open]').waitFor();await click('#action-submit');
  assert((await page.locator('#doc-meta').innerText()).includes('已撤回'),'Real withdrawal');
  assert(await page.locator('#download-original').getAttribute('href'),'Original remains accessible after withdrawal');
  await page.route('**/api/status',r=>r.fulfill({status:503,json:{detail:'合成故障：材料列表暂不可用'}}));
  await page.goto(origin+'/');await settle();
  assert(await page.locator('#retry-list').isVisible()&&!await page.locator('#documents .skeleton').count(),'Initial failure replaces loading with local retry');
  await page.unroute('**/api/status');await click('#retry-list');
  assert(await page.locator('#documents tbody tr').count()>0,'Local retry recovers queue');
  await page.goto(origin+'/#view=review&doc='+publishedDoc.id);await settle();
  await page.evaluate(data=>window.archiveLedgerFixture=data,ledgers);
  assert(errors.length===0,'No uncaught exceptions: '+errors.join(';'));
  return {passed:true,scenarios:['20-row queue','sort/search/page URL restore','document URL restore','field order and types','evidence focus/live text','merge cancel/commit','409 input preservation','hold','timeout no retries','renewal validation/real generation','viewer write controls','notice opinion persistence','no-arrears across roles','partial batch results','withdrawal','initial load failure/retry'],runtimeErrors:errors};
}
