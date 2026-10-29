async page => {
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8767');await page.setViewportSize({width:1366,height:768});
 await page.waitForFunction(()=>typeof activeTab!=='undefined'&&!operationBusy);
 const expected=['存量合同治理','一宗一档电子台账','租金核算','到期与欠缴提醒','催缴通知','合同生成与续期变更','税务推送单','统计查询','系统管理'];
 const actual=await page.locator('nav button').allTextContents();
 if(JSON.stringify(actual)!==JSON.stringify(expected))throw Error('菜单不一致 '+JSON.stringify(actual));
 for(const key of ['ledger','rent','reminders','notices','contracts','tax','statistics','measure','inbox']){
  await page.locator(`[data-tab="${key}"]`).click();
  await page.waitForFunction(k=>activeTab===k&&!operationBusy,key);
  if(await page.locator(`#${key}`).isHidden())throw Error('页面未显示 '+key);
 }
 await page.locator('[data-tab="ledger"]').click();await page.waitForFunction(()=>!operationBusy);
 await page.locator('#ledger-content summary').first().click();
 await page.locator('[data-history]').first().click();await page.waitForFunction(()=>!operationBusy);
 if(!await page.locator('.parcel-history details').count())throw Error('无历史版本');
 await page.locator('[data-tab="rent"]').click();await page.waitForFunction(()=>!operationBusy);
 if(!await page.locator('#rates-form input').count())throw Error('无费率表单');
 await page.locator('[data-tab="contracts"]').click();await page.waitForFunction(()=>!operationBusy);
 await page.locator('#new-contract').click();await page.locator('#action-dialog').waitFor();
 if(!await page.locator('#dialog-use').count())throw Error('新签缺少用途');
 for(const [k,v] of Object.entries({start:'2026-01-01',end:'2027-12-31',contract_no:'UI-SYNTHETIC',tenant:'界面合成测试人',identity:'synthetic',phone:'13800000000',parcel_no:'ui-test-01',address:'测试路2号',area:'10'}))await page.locator('#dialog-'+k).fill(v);
 await page.locator('#dialog-use').selectOption('住宅');await page.locator('#dialog-grade').selectOption('一级');await page.locator('#dialog-mode').selectOption('固定');
 await page.locator('#action-submit').click();await page.waitForFunction(()=>!operationBusy);
 if(!await page.locator('#contract-list').getByText('界面合成测试人',{exact:false}).count())throw Error('新签草稿未显示');
 await page.screenshot({path:'/tmp/fde-business8-desktop.png',fullPage:true});
 await page.locator('[data-tab="reminders"]').click();await page.waitForFunction(()=>!operationBusy);
 if(await page.locator('.calendar-day').count()<28)throw Error('月历不完整');
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/fde-business8-mobile.png',fullPage:true});
 await page.setViewportSize({width:1366,height:768});
 if(errors.length)throw Error(errors.join('\n'));
 return {menus:actual,errors,desktop:'/tmp/fde-business8-desktop.png'};
}
