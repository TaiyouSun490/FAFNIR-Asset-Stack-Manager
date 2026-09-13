"use strict";

// Preferences are local to this browser, never execution authority or reusable nonces.
const ASSET_REVIEW_KEY = "fafnir.assetReview.v1";
function readAssetReview() {
  try {
    const value = JSON.parse(localStorage.getItem(ASSET_REVIEW_KEY) || "null");
    if (value && Array.isArray(value.ids) && value.decisions && typeof value.decisions === "object")
      return {ids: [...new Set(value.ids.filter(id => typeof id === "string"))].slice(0, 5000), decisions: value.decisions};
  } catch { /* Corrupt history must not confer download authority. */ }
  return {ids: [], decisions: {}};
}
function saveAssetReview(value) { localStorage.setItem(ASSET_REVIEW_KEY, JSON.stringify(value)); }
function recordAssetReview(candidate, decision, evidence, plan = null) {
  const saved = readAssetReview();
  if (plan && !saved.ids.includes(candidate.id) && saved.ids.length < 5000) saved.ids.push(candidate.id);
  saved.decisions[candidate.id] = {title: candidate.title, decision, evidence, at: new Date().toISOString(),
    // This latest-decision record cannot start anything; a nonce is never persisted here.
    plan: plan ? {id: plan.id, items: plan.items, expires_at_utc: plan.expires_at_utc} : null};
  saveAssetReview(saved);
  updateAssetComparisonCount();
}
function assetSize(bytes) {
  return Number.isFinite(bytes) && bytes > 0 ?
    (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${(bytes / 1e6).toFixed(1)} MB`) : "容量未確認";
}
function updateAssetComparisonCount() {
  const button = $("#compare-assets-button");
  if (button) button.textContent = `画像で候補を比較（${readAssetReview().ids.length}）`;
}
function assetCompareButton(candidate) {
  const button = element("button", "install-action-button"); button.type = "button";
  const draw = () => button.textContent = readAssetReview().ids.includes(candidate.id) ? "比較から外す" : "比較に追加";
  draw();
  button.addEventListener("click", () => {
    try {
      const saved = readAssetReview();
      if (saved.ids.includes(candidate.id)) saved.ids = saved.ids.filter(id => id !== candidate.id);
      else {
        if (saved.ids.length >= 5000) throw new Error("候補が5000件に達しました。比較対象を整理してください。");
        saved.ids.push(candidate.id);
      }
      saveAssetReview(saved); draw(); updateAssetComparisonCount();
    } catch (error) { button.textContent = `保存できません：${error.message}`; }
  });
  return button;
}

function reviewProductImage(container, candidate) {
  let cancelled = false, requestRevision = 0;
  const imageArea = element("div", "review-product-image");
  const status = element("p", "muted", "商品画像を確認中…"); status.setAttribute("role", "status");
  const previous = element("button", "", "前の画像"); previous.type = "button";
  const next = element("button", "", "次の画像"); next.type = "button";
  const retry = element("button", "", "画像を再取得"); retry.type = "button"; retry.hidden = true;
  const nav = element("div", "asset-buttons"); nav.append(previous, next, retry);
  container.append(imageArea, status, nav, element("p", "muted", "商品紹介画像です。配置結果・互換性・性能の検証ではありません。"));
  let urls = [], index = 0;
  const draw = () => {
    imageArea.replaceChildren(); previous.disabled = index === 0; next.disabled = index >= urls.length - 1;
    if (!urls.length) return;
    const image = element("img"); image.alt = `${candidate.title}の商品画像 ${index + 1}`;
    image.referrerPolicy = "no-referrer";
    const link = element("a"); link.href = urls[index]; link.target = "_blank"; link.rel = "noopener noreferrer";
    image.addEventListener("error", () => {
      status.textContent = "画像を取得できません。画像なしは不適合の証拠ではありません。"; retry.hidden = false;
    });
    image.src = urls[index]; link.append(image); imageArea.append(link);
    status.textContent = `${index + 1} / ${urls.length}（画像を押すと拡大）`;
  };
  const load = async () => {
    const revision = ++requestRevision; retry.hidden = true;
    try {
      urls = (candidate.images || []).map(item => item.candidate_id === candidate.id ? assetImageUrl(item.source_url) : "").filter(Boolean);
      if (!urls.length) urls = assetImages(candidate);
      if (!urls.length) {
        const preview = await getAssetPreview(candidate);
        if (cancelled || revision !== requestRevision) return;
        if (preview.candidate?.id !== candidate.id) throw new Error("商品画像の候補IDが一致しません。");
        urls = assetImages(preview.candidate);
      }
      if (cancelled || revision !== requestRevision) return;
      index = Math.min(index, Math.max(0, urls.length - 1)); draw();
      if (!urls.length) { status.textContent = "公開商品画像がありません。公式ページで確認できます。"; retry.hidden = false; }
    } catch (error) { if (!cancelled) { status.textContent = error.message; retry.hidden = false; } }
  };
  previous.addEventListener("click", () => { index--; draw(); });
  next.addEventListener("click", () => { index++; draw(); });
  retry.addEventListener("click", () => { assetPreviews.delete(candidate.id); load(); });
  load();
  return () => { cancelled = true; requestRevision++; };
}

async function openAssetComparison() {
  $("#asset-comparison-dialog")?.close();
  const dialog = element("dialog", "asset-comparison-dialog"); dialog.id = "asset-comparison-dialog";
  dialog.setAttribute("aria-labelledby", "asset-comparison-title");
  const header = element("div", "modal-head"), title = element("h2", "", "画像付き候補比較"); title.id = "asset-comparison-title";
  const close = element("button", "", "閉じる"); close.type = "button"; close.addEventListener("click", () => dialog.close());
  header.append(title, close); dialog.append(header,
    element("p", "muted", "用途と見た目を比較して、候補ごとに選択・保留・見送り。複数選択できます。選択だけでは取得せず、取得・Import・シーン採用は別確認です。"));
  const message = element("p", "", "候補を確認中…"); message.setAttribute("role", "status");
  const nav = element("div", "asset-buttons"), back = element("button", "", "前の候補"), next = element("button", "", "次の候補");
  back.type = next.type = "button"; nav.append(back, next);
  const grid = element("div", "asset-comparison-grid"); dialog.append(message, nav, grid);
  const ids = readAssetReview().ids.slice(); let offset = 0, generation = 0, cleanups = [];
  const clearImages = () => { cleanups.forEach(stop => stop()); cleanups = []; };
  const render = async () => {
    const version = ++generation; clearImages(); back.disabled = next.disabled = true;
    if (!ids.length) { message.textContent = "カタログの「比較に追加」で候補を選んでください。"; return; }
    try {
      const result = await api("/api/asset-store/compare", {method:"POST", body:JSON.stringify({candidate_ids:ids, offset, limit:6})});
      if (!dialog.isConnected || version !== generation) return;
      message.textContent = `${offset + 1}–${offset + result.items.length} / ${result.total} 件`;
      grid.replaceChildren(); back.disabled = offset === 0; next.disabled = result.next_offset == null;
      for (const candidate of result.items) {
        const card = element("article", "asset-comparison-card"); card.dataset.candidateId = candidate.id;
        card.append(element("h3", "", candidate.title), element("p", "muted", candidate.id));
        cleanups.push(reviewProductImage(card, candidate));
        card.append(element("p", "", candidate.description || "説明未取得"),
          element("p", "", `所有: ${candidate.ownership} / ${candidate.ownership_evidence?.verified === true ? "確認済み" : "未確認"}`),
          element("p", "", `版 ${candidate.version || "未確認"} / ${assetSize(candidate.download_size_bytes)}`),
          element("p", "muted", `Unity ${candidate.unity_version || "未確認"} / Pipeline ${candidate.render_pipeline || "未確認"} / License ${candidate.license || "未確認"}`));
        const link = element("a", "", "公式商品ページ ↗"); link.href = safeUrl(candidate.url); link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
        const decision = element("p", "review-decision"); decision.setAttribute("role", "status");
        const drawDecision = () => {
          const record = readAssetReview().decisions[candidate.id];
          decision.textContent = record ? `${record.decision}：${record.evidence}` : "未回答";
        };
        drawDecision(); const buttons = element("div", "asset-buttons");
        for (const label of ["選択", "保留", "見送り"]) {
          const button = element("button", "", label); button.type = "button";
          button.addEventListener("click", () => {
            try { recordAssetReview(candidate, label, "候補比較でユーザーが回答。取得承認とは別です。"); drawDecision(); }
            catch (error) { decision.textContent = `保存できません：${error.message}`; }
          }); buttons.append(button);
        }
        const acquire = element("button", "", "この候補の取得内容を確認"); acquire.type = "button";
        acquire.disabled = !["owned", "installed"].includes(candidate.ownership) || candidate.ownership_evidence?.verified !== true;
        acquire.addEventListener("click", () => openAssetDetails(candidate, "download"));
        card.append(decision, buttons, acquire); grid.append(card);
      }
    } catch (error) { if (version === generation) message.textContent = `比較を読み込めません：${error.message}。閉じて再度お試しください。`; }
  };
  back.addEventListener("click", () => { offset -= 6; render(); }); next.addEventListener("click", () => { offset += 6; render(); });
  dialog.addEventListener("close", () => { generation++; clearImages(); dialog.remove(); }, {once:true});
  document.body.append(dialog); dialog.showModal(); await render();
}

function confirmAssetDownload(view, prepared) {
  const plan = prepared.plan, item = plan?.items?.[0];
  if (!plan?.id || !prepared.approval_nonce || !Array.isArray(plan.items) || plan.items.length !== 1 || item?.candidate_id !== view.candidate.id
      || String(item.product_id) !== view.candidate.id.split(":")[1]
      || !Number.isFinite(Date.parse(plan.expires_at_utc)))
    throw new Error("取得計画の商品ID・期限が確認できません。取得は開始していません。");
  const expires = Date.parse(plan.expires_at_utc);
  // Persist before waiting, including if the tab closes or reloads without a dialog event.
  // Reopening requires a new plan and explicit response; never resume from browser storage.
  recordAssetReview(view.candidate, "保留", "取得確認待ち。未回答・画面を離れた場合は取得しません。再確認が必要です。", plan);
  if (!view.dialog.isConnected) return Promise.resolve(false);
  return new Promise(resolve => {
    const dialog = element("dialog", "asset-approval-dialog"); dialog.id = "asset-approval-dialog";
    dialog.setAttribute("aria-labelledby", "asset-approval-title");
    const title = element("h2", "", "この1商品の取得を承認しますか？"); title.id = "asset-approval-title";
    dialog.append(title, element("h3", "", item.title || view.candidate.title), element("p", "", item.candidate_id));
    const stopImages = reviewProductImage(dialog, view.candidate);
    dialog.append(element("p", "", `取得する版: ${item.version || "未確認"} / ${assetSize(item.download_size_bytes)}`),
      element("p", "", "保存先: Unity共通キャッシュ。この確認に購入・プロジェクトImport・シーン採用は含みません。"),
      element("p", "muted", `回答期限: ${new Date(expires).toLocaleString()}。無回答は保留としてこのブラウザに記録します。`));
    const status = element("p"); status.setAttribute("role", "status"); dialog.append(status);
    const actions = element("div", "asset-buttons"); dialog.append(actions);
    let settled = false, timer;
    const finish = (approved, label, evidence) => {
      if (settled) return;
      if (approved && Date.now() >= expires) { approved = false; label = "保留"; evidence = "確認期限切れ。取得していません。"; }
      try { recordAssetReview(view.candidate, label, evidence, plan); }
      catch (error) {
        approved = false; evidence = `回答を保存できません。取得はしていません：${error.message}`;
      }
      settled = true; clearTimeout(timer); stopImages(); view.dialog.removeEventListener("close", parentClosed);
      view.status.textContent = evidence; dialog.close(); dialog.remove(); resolve(approved);
    };
    const parentClosed = () => finish(false, "保留", "確認画面を閉じたため保留。取得していません。");
    for (const [label, approved] of [["この1件を取得", true], ["保留", false], ["見送る", false]]) {
      const button = element("button", "button secondary", label); button.type = "button";
      button.addEventListener("click", () => finish(approved, label,
        approved ? "ユーザーが表示された1商品のキャッシュ取得を承認。" : `${label}。取得していません。`)); actions.append(button);
    }
    dialog.addEventListener("cancel", event => { event.preventDefault(); parentClosed(); });
    dialog.addEventListener("close", () => { if (!settled) parentClosed(); });
    view.dialog.addEventListener("close", parentClosed);
    document.body.append(dialog); dialog.showModal();
    timer = setTimeout(() => finish(false, "保留", "確認期限切れ。取得していません。元の候補を保存しました。"), Math.max(0, expires - Date.now()));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  $("#compare-assets-button")?.addEventListener("click", openAssetComparison);
  updateAssetComparisonCount();
});
