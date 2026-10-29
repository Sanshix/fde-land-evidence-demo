// Run after archive-workbench.browser.js. axe source is a local development-only dependency.
async page => {
  const assert=(ok,message)=>{if(!ok)throw Error(message)};
  const settle=()=>page.waitForFunction(()=>!operationBusy);
  const fixture=await page.evaluate(()=>window.archiveLedgerFixture);
  await page.reload();await settle();await page.evaluate(data=>window.archiveLedgerFixture=data,fixture);
  if(fixture?.length){await page.route('**/api/stats',r=>r.fulfill({json:{'宗地数':1,'欠缴宗地数':1,'欠缴合计':fixture[0].payload.arrears,'累计收缴率':'0%','已到期未续':0,'90天内到期':0}}))}
  if(fixture?.length)await page.route('**/api/ledger?*',r=>r.fulfill({json:fixture}));
  await page.addScriptTag({path:'/tmp/fde-a11y/node_modules/axe-core/axe.min.js'});
  const results=[];
  for(const name of ['inbox','governance','review','ledger','measure']){
    await page.locator(`[data-tab="${name}"]`).click();await settle();
    if(name==='measure'){
      await page.locator('#role').evaluate(e=>{e.value='admin';e.dispatchEvent(new Event('change',{bubbles:true}))});await settle();
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>window.scrollTo(0,0));await page.waitForTimeout(200);
    await page.screenshot({animations:'disabled',path:`output/playwright/archive-${name}-1440.png`});
    const axe=await page.evaluate(()=>window.axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}}));
    results.push({view:name,violations:axe.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))})),incomplete:axe.incomplete.map(v=>v.id)});
    for(const width of [320,768,1024,1440]){
      await page.setViewportSize({width,height:900});await page.waitForTimeout(200);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`Page overflow ${name} ${width}`);
      if(name==='review')assert(await page.locator('.review-grid').evaluate((e,width)=>getComputedStyle(e).gridTemplateColumns.split(' ').length===(width<=1050?1:2),width),`Review columns at ${width}`);
      if(width===320)await page.screenshot({animations:'disabled',path:`output/playwright/archive-${name}-320.png`});
    }
  }
  await page.emulateMedia({reducedMotion:'reduce',forcedColors:'active'});
  await page.locator('[data-tab="review"]').click();await settle();
  await page.screenshot({path:'output/playwright/archive-forced-colors.png'});
  assert(await page.evaluate(()=>matchMedia('(prefers-reduced-motion: reduce)').matches&&matchMedia('(forced-colors: active)').matches),'Accessibility display modes active');
  await page.emulateMedia({reducedMotion:'no-preference',forcedColors:'none'});
  // Native keyboard focus into the skip link and main content, without changing the route.
  const before=page.url();await page.locator('.skip-link').focus();await page.keyboard.press('Enter');
  assert(await page.locator('#main-content').evaluate(e=>e===document.activeElement),'Skip link focuses main');
  assert(page.url()===before,'Skip link preserves route context');
  await page.setViewportSize({width:1440,height:1000});
  // Browser-level page zoom via the Chrome keyboard shortcut. Report whether supported,
  // rather than treating CSS zoom or viewport resizing as equivalent evidence.
  const widthBefore=await page.evaluate(()=>innerWidth);
  await page.keyboard.press('Meta+Equal');await page.keyboard.press('Meta+Equal');await page.keyboard.press('Meta+Equal');await page.keyboard.press('Meta+Equal');
  const widthAfter=await page.evaluate(()=>innerWidth);await page.keyboard.press('Meta+Digit0');
  await page.unroute('**/api/ledger?*');await page.unroute('**/api/stats');
  return {views:results,responsiveWidths:[320,768,1024,1440],displayModes:'reduced motion + forced colors',keyboard:'skip link; dialog and select covered by workflow suite',nativeZoom:{widthBefore,widthAfter,supported:widthAfter<widthBefore},realScreenReader:'not exercised'};
}
