// Run against a dedicated acceptance server, never a production database.
async page => {
  const assert=(ok,message)=>{if(!ok)throw Error(message)};
  const base=new URL(page.url()).origin;
  const json=async path=>(await page.request.get(base+path)).json();
  const until=async check=>{const deadline=Date.now()+120000;while(Date.now()<deadline){if(await check())return;await page.waitForTimeout(100)}throw Error('Timed out waiting for actual UI/API state')};
  const settle=()=>until(()=>page.evaluate(()=>!operationBusy));
  await page.goto(base+'/#view=inbox');await settle();
  const head={'X-FDE-Role':'reviewer','X-FDE-Actor':encodeURIComponent('本机经办员')};
  const finished=async (identifier,dataset)=>{
    const deadline=Date.now()+120000;let r;
    while(Date.now()<deadline){const s=await json('/api/demo/status');r=s.runs.find(x=>x.id===(identifier||s.selected)&&(!dataset||x.dataset===dataset));if(r&&!['queued','running','reporting'].includes(r.state))break;await page.waitForTimeout(500)}
    assert(r?.state==='completed',JSON.stringify(r));
    await page.locator('#demo-report h3').waitFor();return r;
  };
  const history=async()=>{const summary=page.locator('#demo-history').locator('xpath=..').locator('summary');if(!await summary.evaluate(e=>e.parentElement.open))await summary.click()};
  await page.locator('#demo-practice').click();
  await page.waitForTimeout(800);
  const started=await json('/api/demo/status');
  const stillRunning=['queued','running','reporting'].includes(started.runs.find(r=>r.id===started.selected).state);
  if(stillRunning){assert(await page.locator('#demo-reset').isDisabled(),'Reset disabled while running');assert((await page.request.get(base+'/api/demo/'+started.selected+'/reset-plan')).status()===409,'API prevents running reset')}
  await page.reload();
  const practice=await finished(started.selected),pr=await json('/api/demo/'+practice.id+'/report');
  assert(pr.actual.documents_imported===50&&pr.actual.pages_imported===139,'Practice import dimensions');
  assert(pr.scoring.strict.denominator===731&&pr.scoring.strict.correct===634,'Practice computed fixed-denominator score');
  assert(pr.actual.published===0&&pr.external_calls_new===0&&pr.batches.length===10,'Practice boundaries and five-document batches');
  assert((await json('/api/status')).documents.length===50,'Practice visible in real list');
  const duplicate=await page.request.post(base+'/api/demo/start',{headers:head,data:{dataset:'practice',mode:'replay'}});
  assert((await duplicate.json()).run.id===practice.id,'Completed duplicate reuses run');
  await page.screenshot({path:'output/playwright/demo-final-practice.png',fullPage:false});
  await page.locator('#demo-evaluation').click();
  const evaluation=await finished(null,'evaluation'),er=await json('/api/demo/'+evaluation.id+'/report');
  assert(er.actual.documents_imported===30&&er.actual.pages_imported===84&&er.batches.length===6,'Evaluation dimensions and batches');
  assert(!er.scoring.available&&er.external_calls_new===0&&er.actual.published===0,'Evaluation no invented accuracy or release');
  assert((await json('/api/status')).documents.length===30,'Evaluation visible');
  const download=page.waitForEvent('download');await page.locator('#demo-report').getByText('下载测试报告（Markdown）').click();await (await download).saveAs('output/playwright/demo-final-report-downloaded.md');
  const doc=(await json('/api/status')).documents[0],detail=await json('/api/documents/'+doc.id);
  assert((await page.request.get(base+'/api/pages/'+doc.id+'/'+detail.pages[0].id)).status()===200,'Test evidence image served');
  await settle();await page.getByRole('button',{name:'待核验与问题清单',exact:true}).click();await settle();
  assert(await page.locator('#demo-global-space').isVisible(),'Test space visible outside inbox');
  await page.getByRole('button',{name:'合同管理',exact:true}).first().click();await settle();await page.locator('#demo-reset').waitFor();
  await page.locator('#demo-reset').click();
  assert((await page.getByRole('dialog').innerText()).includes('30 份文档'),'Reset exact impact count');
  await page.keyboard.press('Escape');await settle();assert((await json('/api/status')).documents.length===30,'Cancel retains data');
  await page.locator('#demo-reset').click();await page.getByRole('button',{name:'确认归档测试数据',exact:true}).click();
  await until(async()=>!(await json('/api/status')).test_space);
  const formal=await json('/api/status');assert(formal.documents.length===1&&formal.documents[0].name==='SYNTHETIC-FORMAL-PROTECT','Formal sample retained');
  await history();await page.locator(`[data-demo-activate="${practice.id}"]`).click();
  await until(async()=>(await json('/api/status')).documents.length===50);await settle();
  await history();await page.locator(`[data-demo-restore="${evaluation.id}"]`).click();
  await until(async()=>(await json('/api/status')).documents.length===30);await settle();
  const widths=[];for(const width of [320,768,1024,1440]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No horizontal page overflow at '+width);widths.push(width)}
  await page.screenshot({path:'output/playwright/demo-final-evaluation.png',fullPage:false});
  return {practice:{id:practice.id,elapsed:pr.elapsed_seconds,strict:pr.scoring.strict,format:pr.scoring.normalized_format,precision:pr.scoring.precision,coverage:pr.scoring.coverage},evaluation:{id:evaluation.id,elapsed:er.elapsed_seconds,accuracy:'N/A'},reset:'cancel, archive, cross-run preservation, restore passed',sourceImage:200,reportDownloaded:true,widths,newCloudCalls:0};
}
