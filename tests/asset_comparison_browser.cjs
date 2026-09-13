// All catalogs, images and acquisition endpoints are fixtures. Never uses an account or downloads assets.
const assert = require('node:assert/strict');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
const base = process.env.FAFNIR_UI_URL;
if (!base) throw new Error('FAFNIR_UI_URL must target the isolated fixture server.');
const candidates = Array.from({length:15}, (_,index) => {
  const id = index + 1;
  return {id:`asset_store:${id}`,external_id:String(id),source:'asset_store',title:`空の比較候補 ${id}`,
    description:'商品画像を見比べるテスト用候補。取得・Import・シーン採用は別の確認です。',
    url:`https://assetstore.unity.com/packages/fixture-${id}`,ownership:id===15?'candidate':'owned',
    ownership_evidence:{verified:id!==15,kind:'unity_editor_my_assets'},version:'1.2',unity_version:'6000.0',
    render_pipeline:'built_in',license:'test-only',categories:[],
    metadata:{asset_store_details:{download_size_bytes:136000000,visuals:id===5?null:{
      main_image_url:`https://assetstorev1-prd-cdn.unity3d.com/key-image/fixture-${id}.svg`,
      gallery:[{image_url:`https://assetstorev1-prd-cdn.unity3d.com/key-image/fixture-${id}-detail.svg`}]
    }}}
  };
});
const card = candidate => ({...candidate,download_size_bytes:136000000,
  images:candidate.metadata.asset_store_details.visuals?[{candidate_id:candidate.id,source_url:candidate.metadata.asset_store_details.visuals.main_image_url}]:[]});

(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    const errors = [], calls = [];
    let wrongPlan = false, expiring = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://assetstorev1-prd-cdn.unity3d.com/**', route => route.fulfill({contentType:'image/svg+xml',
      body:'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="360"><rect width="800" height="360" fill="#5eafdd"/><ellipse cx="250" cy="150" rx="180" ry="70" fill="#e2f5ff"/><text x="160" y="310" font-size="36" fill="#133951">Product image fixture</text></svg>'}));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const body = route.request().postDataJSON(); calls.push({path,body});
      let value;
      if (path==='/api/status') value={version:'test',catalog:{},requirement_categories:[],rag_index:{},asset_store_details:{}};
      else if (path==='/api/catalog') value={items:candidates,count:candidates.length,summary:{}};
      else if (path==='/api/asset-store/compare') value={items:body.candidate_ids.slice(body.offset,body.offset+body.limit).map(id=>card(candidates.find(c=>c.id===id))),total:body.candidate_ids.length,next_offset:body.offset+body.limit<body.candidate_ids.length?body.offset+body.limit:null};
      else if (path==='/api/asset-store/preview') return route.fulfill({status:422,json:{error:{code:'product_preview_unavailable',message:'画像を取得できません（テスト用の欠損）'}}});
      else if (path.endsWith('/download/prepare')) {
        const c = candidates.find(c=>c.id===body.candidate_ids[0]);
        value={plan:{id:'fixture-plan',requires_approval:true,expires_at_utc:new Date(Date.now()+(expiring?700:600000)).toISOString(),
          items:[{candidate_id:wrongPlan?'asset_store:999':c.id,product_id:wrongPlan?999:Number(c.external_id),title:c.title,version:'2.0-exact',download_size_bytes:123456789}]},approval_nonce:'one-use-test-nonce'};
      } else if (path.endsWith('/download/start')) value={job:{id:'fixture-download'}};
      else if (path.includes('/download/jobs/')) value={job:{state:'completed',message:'download complete',products:[{progress:1}]}};
      else throw new Error(`Unexpected endpoint ${path}`);
      await route.fulfill({json:value});
    });
    await page.goto(base+'/?view=catalog');
    await page.locator('.catalog-card').first().waitFor();
    const getCatalog = n => page.locator('.catalog-card').filter({has:page.getByRole('heading',{name:`空の比較候補 ${n}`,exact:true})});
    for (let i=1;i<=15;i++) await getCatalog(i).getByRole('button',{name:'比較に追加',exact:true}).click();
    assert.equal(calls.filter(c=>c.path.includes('/download/')).length,0);
    await page.locator('#compare-assets-button').click();
    const dialog = page.locator('#asset-comparison-dialog');
    await dialog.getByRole('heading',{name:'空の比較候補 1',exact:true}).waitFor();
    assert.equal(await dialog.locator('article').count(),6);
    assert.equal(await dialog.locator('img').count(),5); // Candidate 5 explicitly lacks an image.
    await dialog.getByText('画像を取得できません（テスト用の欠損）',{exact:true}).waitFor();
    const comparisonCard = n => dialog.locator(`article[data-candidate-id="asset_store:${n}"]`);
    await comparisonCard(1).getByRole('button',{name:'選択',exact:true}).click();
    await comparisonCard(1).getByRole('button',{name:'保留',exact:true}).click();
    await comparisonCard(1).getByRole('button',{name:'選択',exact:true}).click();
    await dialog.getByRole('button',{name:'次の候補',exact:true}).click();
    await comparisonCard(7).getByRole('button',{name:'選択',exact:true}).click();
    await dialog.getByRole('button',{name:'次の候補',exact:true}).click();
    await comparisonCard(15).waitFor();
    assert.equal(await comparisonCard(15).getByRole('button',{name:'この候補の取得内容を確認',exact:true}).isDisabled(),true);
    await dialog.getByRole('button',{name:'閉じる',exact:true}).click();
    await page.reload(); await page.locator('.catalog-card').first().waitFor();
    await page.locator('#compare-assets-button').click();
    await comparisonCard(1).getByText(/^選択：/).waitFor();
    const prefs = await page.evaluate(()=>JSON.parse(localStorage.getItem('fafnir.assetReview.v1')));
    assert.equal(prefs.decisions['asset_store:7'].decision,'選択');
    assert.equal(calls.filter(c=>c.path.includes('/download/')).length,0);
    mkdirSync('build',{recursive:true});
    await page.screenshot({path:'build/asset-comparison-desktop.png'});

    // Decline/hold/timeout/invalid plan must not download or proceed to import.
    await comparisonCard(1).getByRole('button',{name:'この候補の取得内容を確認',exact:true}).click();
    const approval = page.locator('#asset-approval-dialog');
    await approval.getByText('取得する版: 2.0-exact / 123.5 MB',{exact:true}).waitFor();
    assert.equal(await approval.locator('img').count(),1);
    const pending = await page.evaluate(()=>JSON.parse(localStorage.getItem('fafnir.assetReview.v1')).decisions['asset_store:1']);
    assert.equal(pending.decision,'保留');
    assert.match(pending.evidence,/再確認が必要/); // Saved even if the tab disappears without a close event.
    assert.equal(calls.filter(c=>c.path.endsWith('/download/start')).length,0);
    await approval.getByRole('button',{name:'保留',exact:true}).click();
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード',exact:true}).click();
    await approval.getByRole('button',{name:'見送る',exact:true}).click();
    await page.getByLabel('インポート先のUnityプロジェクト',{exact:true}).fill('C:\\Fixture\\Project');
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード＆インポート…',exact:true}).click();
    await approval.getByRole('button',{name:'保留',exact:true}).click();
    assert.equal(calls.filter(c=>c.path.endsWith('/import')||c.path.endsWith('/download/start')).length,0);
    expiring=true;
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード',exact:true}).click();
    await page.locator('#asset-detail-dialog .asset-operation-status').getByText('確認期限切れ。取得していません。元の候補を保存しました。',{exact:true}).waitFor();
    expiring=false; wrongPlan=true;
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード',exact:true}).click();
    await page.getByText('取得計画の商品ID・期限が確認できません。取得は開始していません。',{exact:true}).waitFor();
    assert.equal(calls.filter(c=>c.path.endsWith('/download/start')).length,0);
    wrongPlan=false;
    await page.locator('#asset-detail-dialog').getByRole('button',{name:'ダウンロード',exact:true}).click();
    await approval.getByRole('button',{name:'この1件を取得',exact:true}).waitFor();
    await page.screenshot({path:'build/asset-approval-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await approval.evaluate(node=>node.scrollWidth>node.clientWidth),false);
    await approval.getByRole('button',{name:'この1件を取得',exact:true}).click();
    await page.getByText('ダウンロード完了。インポートできます。',{exact:true}).waitFor();
    assert.equal(calls.filter(c=>c.path.endsWith('/download/start')).length,1);
    assert.equal(calls.filter(c=>c.path.endsWith('/import')).length,0);
    const history = await page.evaluate(()=>localStorage.getItem('fafnir.assetReview.v1'));
    assert(!history.includes('one-use-test-nonce'));
    await page.getByRole('button',{name:'詳細を閉じる',exact:true}).click();
    assert.equal(await dialog.evaluate(node=>node.scrollWidth>node.clientWidth),false);
    await page.screenshot({path:'build/asset-comparison-mobile.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS: 15-candidate comparison, paging, images/missing image, multiple/reversed choices, reload persistence, exact-plan image approval, hold/reject/timeout, mismatch, no import on cancellation, one chosen download, mobile layout.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
