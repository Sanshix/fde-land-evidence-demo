async page => {
 const results=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 const assert=(name,ok)=>{results.push({name,passed:!!ok});if(!ok)throw Error(name)};
 const settle=()=>page.waitForFunction(()=>typeof operationBusy!=='undefined'&&!operationBusy);
 const click=async s=>{await page.locator(s).click();await settle()};
 await settle();
 const health=await (await page.request.get('http://127.0.0.1:8882/healthz')).json();
 assert('isolated_worker_disabled',!health.worker_enabled);
 await click('#open-upload');
 await page.locator('#name').fill('合成全链路测试-'+Date.now());
 const raw=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=800;c.height=1000;const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,800,1000);x.fillStyle='black';x.fillText('SYNTHETIC TEST '+Date.now(),50,50);return c.toDataURL().split(',')[1]});
 await page.locator('#files').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:Buffer.from(raw,'base64')});
 await click('#upload button');await page.locator('#field-tenant').waitFor();
 const f={tenant:'自动化合成人',parcel_no:'9-9-999',address:'测试街1号',area:'10',use:'住宅',grade:'一级',start:'2026-01-01',end:'2026-12-31',annual:'120',phone:'13800000000'};
 for(const [k,v] of Object.entries(f))await page.locator('#field-'+k).fill(v);
 await page.locator('#field-mode').selectOption('固定',{force:true});
 await page.locator('#reason').fill('合成数据仅验证操作与计算，不代表真实人工核验');
 await click('#preview');
 await click('#confirm');assert('preview_requires_confirmation',!(await page.locator('#action-dialog').evaluate(e=>e.open))&&(await page.locator('#confirm-requirements').innerText()).includes('请勾选'));
 await page.locator('#registration-clear').check();
 assert('all_confirmations_enable_publish',await page.locator('#confirm').isEnabled());
 await page.locator('#field-area').fill('11');assert('edit_invalidates_preview',(await page.locator('#confirm-requirements').innerText()).includes('请先预览'));
 await page.locator('#field-area').fill('10');await click('#preview');await page.locator('#registration-clear').check();
 await page.locator('#confirm').click();await page.locator('#action-dialog[open]').waitFor();await click('#action-submit');
 assert('published_in_ui',(await page.locator('#doc-meta').innerText()).includes('已登记'));
 for(const key of ['ledger','rent','reminders','notices','contracts','tax','statistics','measure','inbox']){
   await click(`[data-tab="${key}"]`);assert('tab_'+key,await page.locator('#'+key).isVisible());
 }
 await click('[data-tab="ledger"]');
 await page.locator('#ledger-content summary').first().click();assert('ledger_amount_120',(await page.locator('#ledger-content').innerText()).includes('120.00'));
 await click('[data-tab="reminders"]');assert('calendar_complete',await page.locator('.calendar-day').count()>=28);
 await click('[data-tab="contracts"]');await page.locator('#new-contract').click();
 for(const [k,v] of Object.entries({start:'2027-01-01',end:'2028-12-31',contract_no:'AUTO-20261029',tenant:'自动化草稿人',identity:'synthetic',phone:'13800000000',parcel_no:'auto-test-01',address:'测试路2号',area:'10'}))await page.locator('#dialog-'+k).fill(v);
 for(const [k,v] of Object.entries({use:'住宅',grade:'一级',mode:'固定'}))await page.locator('#dialog-'+k).selectOption(v,{force:true});
 await click('#action-submit');assert('new_contract_draft_visible',(await page.locator('#contract-list').innerText()).includes('自动化草稿人'));
 for(const key of ['inbox','ledger','rent','reminders','notices','contracts','tax','statistics','measure']){
   await click(`[data-tab="${key}"]`);
   for(const width of [1440,768,390,320]){
     await page.setViewportSize({width,height:900});assert('overflow_'+key+'_'+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   }
 }
 await page.setViewportSize({width:1440,height:900});await click('[data-tab="ledger"]');
 await page.screenshot({path:'output/playwright/full-chain-20261029-ledger.png',fullPage:true});
 assert('no_browser_exceptions',errors.length===0);
 return {results,errors,scope:'synthetic UI workflow with real API writes; no actual SMS'};
}
