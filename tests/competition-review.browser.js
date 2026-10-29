// Run against an isolated synthetic held alteration document; never a live business record.
async page => {
  const assert=(value,message)=>{if(!value)throw Error(message)};
  if(await page.locator('#action-dialog').evaluate(e=>e.open)){
    await page.locator('#dialog-reason').fill('合成测试：获得清晰原件，继续人工核验');
    await page.locator('#action-submit').click();
    await page.waitForFunction(()=>!operationBusy);
  }
  await page.waitForFunction(()=>!operationBusy);
  assert(await page.locator('#alteration-assistance').isVisible(),'Alteration assistance visible');
  assert(await page.locator('[name="seal-decision"]:checked').count()===0,'Legacy field does not silently preselect a human decision');
  assert(await page.locator('#seal-region-note').innerText().then(t=>t.includes('未取得签章区域')),'Missing seal location explicit');
  assert(await page.locator('[data-alteration-crop]').count()===2,'Both source regions shown');
  await page.waitForFunction(()=>[...document.querySelectorAll('[data-alteration-crop]')].every(c=>c.width!==300));
  const widths=[];
  for(const width of [320,768,1024,1440]){
    await page.setViewportSize({width,height:1000});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No horizontal overflow at '+width);
    widths.push(width);
  }
  await page.locator('[name="seal-decision"][value="unknown"]').check();
  assert(await page.locator('#field-altered_sealed').inputValue()==='无法判断','Unknown remains unknown');
  assert(await page.locator('#confirm').isDisabled(),'Unknown cannot be confirmed');
  assert(await page.locator('#confirm-requirements').innerText().then(t=>t.includes('签章无法判断')),'Actionable unknown reason visible');
  await page.locator('#alteration-assistance').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/competition-alteration-unknown-1440.png'});
  await page.locator('[name="seal-decision"][value="present"]').check();
  assert(await page.locator('#field-altered_sealed').inputValue()==='是','Explicit positive judgment recorded');
  assert(await page.locator('#confirm-requirements').innerText().then(t=>t.includes('判断依据所在原件')),'Source page still required');
  await page.locator('#alteration-source').evaluate(e=>{e.value=e.options[1].value;e.dispatchEvent(new Event('change',{bubbles:true}))});
  assert(await page.evaluate(()=>body().alteration_review.decision==='present'&&!!body().alteration_review.unit_id),'Decision and source in submission');
  await page.locator('#save-draft').click();await page.waitForFunction(()=>!operationBusy);
  await page.locator('#reload-doc').click();await page.waitForFunction(()=>!operationBusy);
  assert(await page.locator('[name="seal-decision"][value="present"]').isChecked(),'Explicit judgment survives draft reload');
  await page.locator('[name="seal-decision"][value="unknown"]').check();
  await page.locator('#hold').click();await page.waitForFunction(()=>!operationBusy);
  assert(await page.locator('#resume-review').isVisible(),'Held document can return to human review');
  assert(await page.evaluate(()=>doc.state==='held'&&doc.draft?.alteration_review?.decision==='unknown'),'Hold preserves unknown draft');
  await page.locator('#resume-review').click();
  await page.locator('#dialog-reason').fill('合成测试：继续核对签章');await page.locator('#action-submit').click();
  await page.waitForFunction(()=>!operationBusy);
  assert(await page.evaluate(()=>doc.state==='needs_human'&&doc.draft?.alteration_review?.decision==='unknown'),'Resume preserves draft without cloud');
  assert(await page.locator('#confirm').isDisabled(),'Resuming does not resolve unknown signature');
  return {passed:true,widths,note:'Synthetic UI exercise, no real signature or model accuracy acceptance'};
}
