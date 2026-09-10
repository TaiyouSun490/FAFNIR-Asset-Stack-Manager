// Run against a local Fafnir server. All acquisition requests are simulated.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.FAFNIR_UI_URL || 'http://127.0.0.1:8770';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');
const owned = {id:'asset_store:1', source:'asset_store', external_id:'1', title:'Owned Test Animator',
  ownership:'owned', ownership_evidence:{verified:true,kind:'unity_editor_my_assets'},
  description:'Character animation package', categories:[], url:'https://assetstore.unity.com/packages/slug-1',
  metadata:{asset_store_details:{visuals:{main_image_url:'https://assetstorev1-prd-cdn.unity3d.com/test.png',gallery:[]}}}};
const market = {...owned,id:'asset_store:2',title:'Unowned Test Animator',ownership:'candidate',ownership_evidence:{verified:false}};

(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => localStorage.setItem('fafnir.showAssetImages','true'));
    const calls = [];
    let cached = false, failDownload = false, importState = 'cancelled', holdDownload = false;
    let malformedCatalog = false, pendingCatalog = null;
    await page.route('https://assetstorev1-prd-cdn.unity3d.com/**', route => route.fulfill({contentType:'image/png',body:png}));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      calls.push({path,body:route.request().postDataJSON()});
      let value;
      if (path === '/api/status') value = {version:'test',catalog:{},requirement_categories:[],rag_index:{},asset_store_details:{}};
      else if (path === '/api/catalog') {
        if (malformedCatalog) return route.fulfill({status:200,contentType:'application/json',body:'{'});
        if (url.searchParams.get('q') === 'stale') { pendingCatalog = route; return; }
        value = {items:[owned,market],count:2,summary:{}};
      }
      else if (path.endsWith('/download/prepare')) value = {plan:{id:'test',requires_approval:!cached},approval_nonce:'test'};
      else if (path.endsWith('/download/start')) {
        if (failDownload) return route.fulfill({status:409,json:{error:{code:'download_bridge_offline',message:'offline'}}});
        value = {job:{id:'test-download',state:'queued'}};
      }
      else if (path.includes('/download/jobs/')) {
        if (!holdDownload) cached = true;
        value = {job:{state:holdDownload?'downloading':'completed',message:holdDownload?'Downloading test':'Download complete',products:[{progress:holdDownload?0.4:1}]}};
      }
      else if (path.endsWith('/import')) value = {job:{id:'test-import',state:'queued'}};
      else if (path.includes('/import/jobs/')) value = {job:{state:importState,message:importState==='cancelled'?'インポートをキャンセルしました':'インポート完了'}};
      else throw new Error('Unexpected API route: '+path);
      await route.fulfill({json:value});
    });
    await page.goto(base+'/?view=catalog');
    await page.getByRole('heading',{name:owned.title,exact:true}).waitFor();
    const ownedCard = page.locator('.catalog-card').filter({has:page.getByRole('heading',{name:owned.title,exact:true})});
    const marketCard = page.locator('.catalog-card').filter({has:page.getByRole('heading',{name:market.title,exact:true})});
    assert.equal(await ownedCard.locator('.asset-thumbnail img').count(),1);
    assert.equal(await marketCard.getByRole('button',{name:'ダウンロード',exact:true}).count(),0);
    await page.getByLabel('商品画像',{exact:true}).uncheck();
    await page.waitForFunction(() => !document.querySelector('.catalog-card .asset-thumbnail img'));
    await ownedCard.getByRole('button',{name:'詳細を見る',exact:true}).click();
    assert.equal(await page.locator('#asset-detail-dialog img').count(),0);
    await page.getByRole('button',{name:'商品画像を見る',exact:true}).click();
    assert.equal(await page.locator('#asset-detail-dialog img').count(),1);
    await page.getByRole('button',{name:'詳細を閉じる'}).click();

    failDownload = true;
    await ownedCard.getByRole('button',{name:'ダウンロード',exact:true}).click();
    await page.getByText('Fafnir入りのUnityを1つ開いてください。ダウンロード先は共通キャッシュです。',{exact:true}).waitFor();
    failDownload = false;
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード',exact:true}).click();
    await page.getByText('ダウンロード完了。インポートできます。',{exact:true}).waitFor();
    const startCount = calls.filter(call => call.path.endsWith('/download/start')).length;
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'インポート…',exact:true}).click();
    await page.getByText('インポート先のUnityプロジェクトを入力してください。',{exact:true}).waitFor();
    await page.getByLabel('インポート先のUnityプロジェクト',{exact:true}).fill('C:\\Test\\SelectedProject');
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'インポート…',exact:true}).click();
    await page.getByText('インポートをキャンセルしました',{exact:true}).waitFor();
    assert.equal(calls.filter(call => call.path.endsWith('/download/start')).length,startCount);
    assert.equal(calls.find(call => call.path.endsWith('/import')).body.project_path,'C:\\Test\\SelectedProject');
    importState = 'imported';
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'インポート…',exact:true}).click();
    await page.getByText('インポート完了',{exact:true}).waitFor();
    await page.getByRole('button',{name:'詳細を閉じる'}).click();

    cached = false; holdDownload = true;
    await ownedCard.getByRole('button',{name:'ダウンロード',exact:true}).click();
    await page.getByText('Downloading test',{exact:true}).waitFor();
    await page.getByRole('button',{name:'詳細を閉じる'}).click();
    await ownedCard.getByRole('button',{name:'詳細を見る',exact:true}).click();
    await page.getByText('Downloading test',{exact:true}).waitFor();
    holdDownload = false;
    await page.getByText('ダウンロード完了。インポートできます。',{exact:true}).waitFor();
    await page.setViewportSize({width:390,height:844});
    const bounds = await page.locator('#asset-detail-dialog').boundingBox();
    assert(bounds.width <= 390 && bounds.x >= 0);
    assert.equal(await page.locator('#asset-detail-dialog').evaluate(node => node.scrollWidth > node.clientWidth),false);
    assert.deepEqual(errors,[]);
    await page.getByRole('button',{name:'詳細を閉じる'}).click();
    malformedCatalog = true;
    await page.getByRole('button',{name:'絞り込む',exact:true}).click();
    await page.getByText('サーバー応答を読み取れませんでした。もう一度お試しください。',{exact:true}).waitFor();
    assert.equal(await page.locator('.catalog-card').count(),2);
    malformedCatalog = false;
    await page.locator('#catalog-query').fill('stale');
    await page.getByRole('button',{name:'絞り込む',exact:true}).click();
    while (!pendingCatalog) await new Promise(resolve=>setTimeout(resolve,10));
    await page.locator('#catalog-query').fill('fresh');
    await Promise.all([page.waitForResponse(r=>r.url().includes('q=fresh')),page.getByRole('button',{name:'絞り込む',exact:true}).click()]);
    await pendingCatalog.fulfill({json:{items:[],count:0,summary:{}}});
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.catalog-card').count(),2);
    console.log('PASS: thumbnails toggle, ownership, download retry, cache reuse, target selection, import cancellation/retry, reopen progress, mobile layout');
    if (process.argv.includes('--mock-only')) return;

    // Inspect the real catalog without triggering download/import.
    const actual = await browser.newPage({viewport:{width:1280,height:900}});
    await actual.addInitScript(() => localStorage.setItem('fafnir.showAssetImages','false'));
    await actual.goto(base+'/?view=catalog');
    await actual.locator('#catalog-query').fill('Look Animator');
    await actual.getByRole('button',{name:'絞り込む',exact:true}).click();
    await actual.getByRole('heading',{name:'Look Animator',exact:true}).first().waitFor();
    await actual.getByLabel('商品画像',{exact:true}).check();
    await actual.locator('.catalog-card .asset-thumbnail img').first().waitFor({timeout:60000}).catch(() => {});
    await actual.waitForFunction(() => [...document.querySelectorAll('.asset-thumbnail img')].some(img => img.complete && img.naturalWidth > 0), {timeout:20000}).catch(() => {});
    assert.equal(await actual.locator('.catalog-card').count(),3);
    await actual.screenshot({path:'build/asset-buttons-ux-desktop.png',fullPage:true});
    console.log('Real catalog screenshot: build/asset-buttons-ux-desktop.png');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
