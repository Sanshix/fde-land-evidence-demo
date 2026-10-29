async page => {
 const assert=(value,message)=>{if(!value)throw Error(message)};
 await page.waitForFunction(()=>typeof doc!=='undefined'&&doc&&!operationBusy&&document.querySelectorAll('[data-field]').length);
 assert(await page.locator('.review-links button').allTextContents().then(x=>x.join('|')==='核对合同事实|匹配与核算|确认登记'),'Three review steps');
 assert(await page.locator('select[data-cite]').count()===0,'No evidence unit selector');
 assert(await page.locator('.field-number').allTextContents().then(x=>x.every((v,i)=>v.trim()===`${i+1}.`)),'Sequential field numbering');
 assert(await page.locator('#field-has_map').inputValue()==='是','Map candidate prefilled');
 for(const [score,label] of [[59,'待人工核验'],[60,'待人工确认'],[99,'待人工确认']]){
   assert(await page.evaluate(({score,label})=>confidenceSummary({score}).includes(`${score}% · ${label}`),{score,label}),'Confidence boundary '+score);
 }
 const tenant=await page.locator('#field-tenant').inputValue();
 await page.locator('#field-tenant').fill('');
 assert(await page.locator('#confirm').isDisabled(),'Empty tenant blocks registration');
 assert((await page.locator('#confirm-requirements').textContent()).includes('承租人'),'Missing tenant is named');
 await page.locator('#field-tenant').fill(tenant);
 const dimensions=[];
 for(const width of [1440,1024,768,320]){
   await page.setViewportSize({width,height:900});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Review overflow '+width);
   if(width===1440){
     assert(await page.evaluate(()=>new Set([...document.querySelectorAll('.review-toolbar button')].map(b=>Math.round(b.getBoundingClientRect().top))).size===1),'All six toolbar actions on same desktop line');
     await page.locator('[data-jump="review-stage-0"]').click();
     await page.screenshot({path:'output/playwright/iteration-facts-desktop.png'});
   }
   if(width===320)await page.screenshot({path:'output/playwright/iteration-review-mobile.png'});
   dimensions.push(width);
 }
 // Discard only this isolated browser exercise's unsaved edits.
 await page.evaluate(()=>dirty=false);
 await page.setViewportSize({width:1440,height:900});
 await page.locator('[data-tab="reminders"]').click();
 await page.waitForFunction(()=>!operationBusy&&document.querySelector('#notice-template-panel').textContent.includes('评测集预置'));
 await page.locator('#reminder-start').fill('2026-01-01');
 await page.locator('#reminder-end').fill('2026-12-31');
 await page.locator('#reminder-filter').getByRole('button',{name:'查询',exact:true}).click();
 await page.waitForFunction(()=>!operationBusy);
 assert((await page.locator('#reminder-export').getAttribute('href')).includes('start=2026-01-01'),'Range included in calendar export');
 await page.screenshot({path:'output/playwright/iteration-reminders-desktop.png'});
 await page.locator('[data-tab="contracts"]').click();
 await page.waitForFunction(()=>!operationBusy&&document.querySelector('#contract-template-panel').textContent.includes('评测集预置'));
 assert(await page.locator('#contract-template-panel a').count()===2,'Current and evaluation template downloads');
 await page.screenshot({path:'output/playwright/iteration-templates-desktop.png'});
 assert(await page.locator('#template-file-contract').isVisible(),'Upload module visible to reviewer');
 assert(await page.locator('#template-file-contract').isDisabled(),'Template replacement restricted to admin');
 await page.getByRole('combobox',{name:'角色',exact:true}).click();
 await page.getByRole('option',{name:'管理员',exact:true}).click();
 await page.waitForFunction(()=>!operationBusy);
 const builtin=await page.request.get(new URL('/api/templates/contract/document?builtin=true',page.url()).href);
 await page.locator('#template-file-contract').setInputFiles({name:'界面导入验证.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:await builtin.body()});
 await page.locator('#contract-template-panel').getByRole('button',{name:'导入并使用',exact:true}).click();
 await page.waitForFunction(()=>!operationBusy&&document.querySelector('#contract-template-panel').textContent.includes('已导入模板'));
 await page.locator('#contract-template-panel').getByRole('button',{name:'恢复评测集预置',exact:true}).click();
 await page.waitForFunction(()=>!operationBusy&&document.querySelector('#contract-template-panel').textContent.includes('评测集预置'));

 return {passed:true,dimensions,steps:3,confidenceBoundary:60,mapPrefill:true,emptyRequiredBlocked:true,rangeExport:true,templatePanels:true,adminTemplateImportRestore:true};
}
