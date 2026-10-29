async page => {
 const assert=(x,m)=>{if(!x)throw Error(m)};
 await page.reload();await page.waitForFunction(()=>!operationBusy);await page.locator('[data-tab="inbox"]').click();await page.waitForFunction(()=>!operationBusy);await page.waitForSelector('#documents [data-open]');await page.locator('[data-tab="review"]').click();
 await page.locator('[data-tab="inbox"]').click();await page.locator('[data-state="view"]').first().click();
 await page.waitForSelector('#review-content:not(.hidden)');
 const result=[];
 for(const [width,height] of [[1440,900],[1366,768],[1024,768],[768,1024],[320,780]]){
 await page.setViewportSize({width,height});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Review overflow '+width);
 await page.locator('[data-jump="review-stage-2"]').first().click();
 assert(await page.evaluate(()=>document.activeElement.id==='preview'||document.activeElement.id==='review-stage-2'),'Navigation focus');
 await page.screenshot({path:`output/playwright/review-${width}.png`});result.push(width);
 }
 const rendered=await page.evaluate(()=>renderCalc({as_of:'2026-09-30',ruleset:'synthetic',annual:[{year:2025,due:'100.00',paid:'40.00',arrears:'60.00'},{year:2026,due:'100.00',paid:'100.00',arrears:'0.00'}],allocations:[{receipt:'T1',year:2025,amount:'40.00'},{receipt:'T2',year:null,amount:'20.00',type:'多缴'}],segments:[],due:'200.00',paid:'160.00',arrears:'40.00',overpaid:'20.00',issues:[],coverage:'合成展示测试'}));
 assert(rendered.includes('多缴／未归入年度')&&rendered.includes('60.00')&&rendered.includes('40.00'),'Server values preserved');
 await page.setViewportSize({width:1440,height:900});
 await page.evaluate(html=>document.querySelector('#calculation').innerHTML=html,rendered);
 await page.locator('#calculation').scrollIntoViewIfNeeded();await page.screenshot({path:'output/playwright/review-calculation.png'});
 return {passed:true,widths:result,amounts:'Server values and overpayment preserved',note:'Synthetic rendering fixture; not financial acceptance'};
}
