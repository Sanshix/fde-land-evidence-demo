// Run against a fresh loopback instance with FDE_DISABLE_WORKER=1 and a temporary
// FDE_DATA_DIR; never against the business database. Open that URL using:
// playwright-cli -s=workbench open http://127.0.0.1:8877
// playwright-cli -s=workbench run-code --filename=tests/workbench.browser.js
// Real API workflow + synthetic network failures; no cloud model calls.
async (page) => {
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const settled = () => page.waitForFunction(() => ![...document.querySelectorAll('.tab')].some(e => e.inert));
  const click = async selector => { await page.locator(selector).click(); await settled(); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  assert(/^http:\/\/(127\.0\.0\.1|localhost):/.test(page.url()), 'Use an isolated loopback server');
  const health = await (await page.request.get(new URL('/healthz',page.url()).href)).json();
  const initial = await (await page.request.get(new URL('/api/status',page.url()).href)).json();
  assert(!health.worker_enabled && initial.documents.length === 0, 'Requires a fresh database and disabled worker');
  await page.goto(new URL('/',page.url()).href); await settled();
  assert(await page.locator('#intake').evaluate(e => e.open), 'Empty state opens upload form');
  const base64 = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 800; canvas.height = 1000;
    const ctx = canvas.getContext('2d'); ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 800, 1000);
    ctx.fillStyle = 'black'; ctx.font = '20px sans-serif';
    ctx.fillText('SYNTHETIC UI TEST - NOT A REAL CONTRACT', 30, 40);
    ctx.fillText('Area: 10 | Annual rent: 120 | Parcel: 9-9-999', 30, 80);
    return canvas.toDataURL('image/png').split(',')[1];
  });
  await page.locator('#name').fill('合成验收材料 · 不是真实合同');
  await page.locator('#files').setInputFiles({name: 'synthetic.png', mimeType: 'image/png', buffer: Buffer.from(base64, 'base64')});
  // A delayed response makes the duplicate-submission guard observable.
  let uploads = 0;
  await page.route('**/api/import', async route => { uploads++; await new Promise(resolve => setTimeout(resolve, 300)); await route.continue(); });
  await page.locator('#upload button').click();
  assert(await page.locator('#upload button').isDisabled(), 'Upload disables its submit button while pending');
  await page.locator('#upload').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settled(); await page.unroute('**/api/import');
  assert(uploads === 1, 'Repeated submit must not upload twice');
  await page.locator('[data-field="tenant"]').fill('未保存的测试');
  await page.locator('[data-tab="inbox"]').click();
  await page.getByRole('dialog').waitFor();
  await click('#unsaved-cancel');
  assert(await page.locator('[data-field="tenant"]').inputValue() === '未保存的测试', 'Cancel retains input');
  await page.locator('#reload-doc').click(); await page.getByRole('dialog').waitFor();
  await click('#unsaved-discard');
  assert(await page.locator('[data-field="tenant"]').inputValue() === '', 'Discard reloads saved content');
  const fields = {tenant:'模拟测试人',parcel_no:'9-9-999',address:'测试街1号',area:'10',use:'住宅',grade:'一级',start:'2026-01-01',end:'2026-12-31',mode:'固定',annual:'120',phone:'10000000000'};
  for (const [key, value] of Object.entries(fields)) {
    await page.locator(`[data-field="${key}"]`).fill(value);
    await page.locator(`[data-cite="${key}"]`).locator('..').locator('.select-trigger').click();
    await page.getByRole('option').last().click();
  }
  await page.locator('#reason').fill('合成数据仅验证流程，不代表真实材料核验。');
  await page.route('**/api/documents/*/draft', route => route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({detail:'合成故障：暂时无法保存'})}));
  await page.locator('[data-tab="inbox"]').click();
  await page.locator('#unsaved-save').click();
  await page.locator('#unsaved-error:not([hidden])').waitFor();
  assert(await page.getByRole('dialog').isVisible(), 'Save failure keeps the dialog open');
  assert(await page.locator('[data-field="tenant"]').inputValue() === '模拟测试人', 'Save failure retains data');
  await click('#unsaved-cancel'); await page.unroute('**/api/documents/*/draft');
  await page.locator('[data-field="tenant"]').focus();
  await page.waitForTimeout(5500);
  assert(await page.locator('[data-field="tenant"]').evaluate(e => e === document.activeElement), 'Polling preserves input focus');
  assert(await page.locator('[data-field="tenant"]').inputValue() === '模拟测试人', 'Polling preserves input');
  await page.locator('[data-tab="inbox"]').click(); await click('#unsaved-save');
  await click('#documents [data-open]');
  assert(await page.locator('[data-field="tenant"]').inputValue() === '模拟测试人', 'Draft survives navigation');
  // Failed API operations leave persistent feedback beside the action.
  await page.route('**/api/documents/*/preview', route => route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({detail:'合成故障：核算服务暂不可用'})}));
  await click('#preview');
  assert((await page.locator('.field-panel > .action-feedback').innerText()).includes('核算服务暂不可用'), 'Persistent local error');
  assert(await page.locator('#confirm').isDisabled(), 'Failed preview cannot register');
  await page.unroute('**/api/documents/*/preview');
  await click('#preview');
  assert(await page.locator('#confirm').isDisabled(), 'Preview alone cannot register');
  for (const id of ['material-clear','evidence-clear','payments-clear','calculation-clear']) await page.locator('#'+id).check();
  assert(await page.locator('#confirm').isEnabled(), 'Four confirmations and source citations allow registration');
  await page.locator('[data-field="area"]').fill('11');
  assert(await page.locator('#confirm').isDisabled(), 'Changed field invalidates preview');
  assert(!await page.locator('#calculation-clear').isChecked(), 'Changed field clears calculation confirmation');
  await page.locator('[data-field="area"]').fill('10'); await click('#preview');
  await page.locator('#calculation-clear').check(); await page.locator('#confirm').click(); await page.locator('#action-dialog[open]').waitFor(); await click('#action-submit');
  assert((await page.locator('#doc-meta').innerText()).includes('已登记'), 'Real API registration succeeds');
  await click('[data-tab="inbox"]'); await click('[data-state="published"]');
  assert((await page.locator('.amount-total').innerText()).includes('120.00'), 'Ledger shows deterministic rent');
  // Cover all list states with a clearly synthetic GET-only response.
  const fixture = await (await page.request.get(new URL('/api/status',page.url()).href)).json();
  const sample = fixture.documents[0];
  fixture.documents = ['queued','recognizing','needs_human','held','published','withdrawn'].map((state, index) => ({...sample, id:'synthetic-state-'+index, name:'状态验收 '+state, state}));
  await page.route('**/api/status', route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(fixture)}));
  await click('[data-tab="inbox"]'); await click('#refresh');
  for (const [key, count] of [['needs_human',1],['processing',2],['held',1],['published',1]]) {
    assert(await page.locator(`[data-filter="${key}"] b`).innerText() === String(count), 'Correct counter: '+key);
    await click(`[data-filter="${key}"]`);
    assert(await page.locator('#documents tbody tr').count() === count, 'Correct filter: '+key);
  }
  await click('#all-documents');
  assert(await page.locator('#documents tbody tr').count() === 6, 'All materials includes withdrawn');
  await page.locator('#documents button').first().focus(); await page.waitForTimeout(5500);
  assert(await page.locator('#documents button').first().evaluate(e => e === document.activeElement), 'Polling preserves row focus');
  for (const [width,height] of [[1440,900],[1280,800],[390,844],[320,780],[768,1024],[1024,768]]) {
    await page.setViewportSize({width,height});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'No page overflow at '+width);
    await page.screenshot({path:`output/playwright/workbench-${width}.png`});
  }
  await page.unroute('**/api/status'); await page.setViewportSize({width:1440,height:900});
  await click('[data-tab="measure"]');
  assert(!await page.locator('#backup').isVisible(), 'Reviewer cannot see admin controls');
  await page.getByRole('combobox',{name:'角色',exact:true}).click(); await page.keyboard.press('End'); await page.keyboard.press('Enter'); await settled();
  assert(await page.locator('#backup').isVisible(), 'Administrator sees backup');
  await page.getByRole('combobox',{name:'角色',exact:true}).click(); await page.keyboard.press('Home'); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter'); await settled();
  assert(!await page.locator('#backup').isVisible(), 'Viewer cannot see admin controls');
  await click('[data-tab="inbox"]'); await click('#open-upload');
  assert(await page.locator('#upload button').isDisabled(), 'Viewer cannot upload');
  assert(errors.length === 0, 'No runtime exceptions: '+errors.join(', '));
  return {passed:true, scenarios:['upload deduplication','unsaved cancel/discard/save/failure','polling focus','persistent error','preview invalidation','registration and ledger','six states and filters','six responsive widths','role controls and keyboard select'], runtimeErrors:errors};
}
