"use strict";

const assetOperations = new Map();
const ASSET_API = "/api/install/asset-store";
const assetPreviews = new Map();
let assetPreviewQueue = Promise.resolve();

function getAssetPreview(candidate) {
  if (!assetPreviews.has(candidate.id)) {
    const request = api("/api/asset-store/preview", {method: "POST", body: JSON.stringify({candidate_id: candidate.id})});
    assetPreviews.set(candidate.id, request);
    request.catch(() => assetPreviews.delete(candidate.id));
  }
  return assetPreviews.get(candidate.id);
}

function assetImageUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "assetstorev1-prd-cdn.unity3d.com"
      && !url.username && !url.password && (!url.port || url.port === "443") ? url.href : "";
  } catch { return ""; }
}

function assetImages(candidate) {
  const visuals = candidate.metadata?.asset_store_details?.visuals || {};
  return [...new Set([visuals.main_image_url,
    ...(visuals.gallery || []).map((item) => item.image_url || item.thumbnail_url),
  ].map(assetImageUrl).filter(Boolean))].slice(0, 12);
}

function assetThumbnail(candidate) {
  const fragment = document.createDocumentFragment();
  if (candidate.source !== "asset_store") return fragment;
  const button = element("button", "asset-thumbnail");
  button.type = "button";
  button.setAttribute("aria-label", `${candidate.title}の商品画像・詳細を見る`);
  const url = assetImages(candidate)[0];
  if (url && $("#show-asset-images")?.checked) {
    const img = element("img");
    const loading = element("span", "", "商品画像を読み込み中…");
    img.src = url; img.alt = `${candidate.title}の商品画像`;
    img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    img.addEventListener("load", () => loading.remove(), {once: true});
    img.addEventListener("error", () => button.replaceChildren(element("span", "", "画像を取得できません・詳細を見る")));
    button.append(img, loading);
  } else {
    button.append(element("span", "", url ? "商品画像を見る" : "商品画像・詳細を見る"));
    if (!url && $("#show-asset-images")?.checked && !candidate.metadata?.asset_store_details?.visuals) {
      const observer = new IntersectionObserver((entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        assetPreviewQueue = assetPreviewQueue.then(async () => {
          if (!button.isConnected || !$("#show-asset-images")?.checked) return;
          try {
            const preview = await getAssetPreview(candidate);
            Object.assign(candidate, preview.candidate);
            if (button.isConnected) button.replaceWith(assetThumbnail(candidate));
          } catch { button.textContent = "画像を取得できません・詳細を見る"; }
        });
      }, {rootMargin: "100px"});
      observer.observe(button);
    }
  }
  button.addEventListener("click", () => openAssetDetails(candidate));
  return button;
}

function assetActionButtons(candidate) {
  if (candidate.source !== "asset_store") return installActionButton(candidate);
  const actions = element("div", "asset-buttons");
  const owned = ["owned", "installed"].includes(candidate.ownership)
    && (candidate.ownership_evidence || candidate.metadata?.ownership_evidence)?.verified === true;
  if (owned) {
    for (const [label, mode] of [["ダウンロード", "download"], ["インポート…", "import"]]) {
      const button = element("button", "install-action-button", label);
      button.type = "button";
      button.addEventListener("click", () => openAssetDetails(candidate, mode));
      actions.append(button);
    }
  }
  const detail = element("button", "install-action-button", owned ? "詳細を見る" : "商品ページ・詳細");
  detail.type = "button";
  detail.addEventListener("click", () => openAssetDetails(candidate));
  actions.append(detail);
  return actions;
}

function assetFailure(error) {
  return {
    download_bridge_offline: "Fafnir入りのUnityを1つ開いてください。ダウンロード先は共通キャッシュです。",
    download_sign_in_required: "Unity Hubと起動中のEditorでログイン状態を復旧してください。",
    download_bridge_busy: "Unityが別の処理中です。完了後に再試行できます。",
    verified_ownership_required: "Unity My Assetsとの所有同期を確認してください。",
  }[error.code] || error.message;
}

async function pollAssetJob(view, kind, id) {
  // Closing the detail dialog does not cancel a download or import in Unity.
  const deadline = Date.now() + 30 * 60 * 1000;
  while (Date.now() < deadline) {
    let response;
    try { response = await api(`${ASSET_API}/${kind}/jobs/${encodeURIComponent(id)}`); }
    catch (error) {
      if (["import_job_not_found", "download_job_not_found", "invalid_job_id"].includes(error.code))
        sessionStorage.removeItem(`fafnir.assetJob.${view.candidate.id}`);
      throw error;
    }
    const {job} = response;
    view.status.textContent = job.message || ({queued: "Unityへの接続待ち…", starting: "転送開始待ち…",
      downloading: "ダウンロード中…"}[job.state] || job.state);
    if (kind === "download" && job.products?.length) {
      view.progress.value = job.products.reduce((sum, item) => sum + (item.progress || 0), 0) / job.products.length;
    }
    if (["completed", "imported", "cancelled", "failed", "expired"].includes(job.state)) {
      sessionStorage.removeItem(`fafnir.assetJob.${view.candidate.id}`);
      if (["failed", "expired"].includes(job.state)) throw new Error(job.message || "処理に失敗しました。");
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("進捗確認を終了しました。Unityの処理は継続します。詳細を開き直すと再確認できます。");
}

async function downloadAsset(view) {
  view.status.textContent = "所有状態とキャッシュを確認中…";
  const prepared = await api(`${ASSET_API}/download/prepare`, {
    method: "POST", body: JSON.stringify({candidate_ids: [view.candidate.id]}),
  });
  if (!prepared.plan.requires_approval) {
    view.status.textContent = "ダウンロード済み。インポートできます。";
    view.importButton.textContent = "インポート…";
    view.progress.value = 1;
    return;
  }
  const started = await api(`${ASSET_API}/download/start`, {
    method: "POST", body: JSON.stringify({plan_id: prepared.plan.id, approval_nonce: prepared.approval_nonce}),
  });
  sessionStorage.setItem(`fafnir.assetJob.${view.candidate.id}`, JSON.stringify({kind: "download", id: started.job.id}));
  await pollAssetJob(view, "download", started.job.id);
  view.status.textContent = "ダウンロード完了。インポートできます。";
  view.progress.value = 1;
  view.download.textContent = "ダウンロード済み";
  view.importButton.textContent = "インポート…";
}

async function runAssetAction(view, mode) {
  if (assetOperations.has(view.candidate.id)) {
    view.status.textContent = "このアセットは処理中です。完了後に詳細を開き直してください。";
    return;
  }
  const project = view.project.value.trim();
  if (mode === "import" && !project) {
    view.status.textContent = "インポート先のUnityプロジェクトを入力してください。";
    view.project.focus(); return;
  }
  assetOperations.set(view.candidate.id, view);
  view.download.disabled = view.importButton.disabled = true;
  try {
    if (mode === "resume") {
      const saved = JSON.parse(sessionStorage.getItem(`fafnir.assetJob.${view.candidate.id}`) || "null");
      if (saved) await pollAssetJob(view, saved.kind, saved.id);
      return;
    }
    await downloadAsset(view);
    if (mode === "import") {
      $("#project-path").value = project;
      localStorage.setItem("stackforge.projectPath", project);
      view.status.textContent = "パッケージと導入先を検査中…";
      const result = await api(`${ASSET_API}/import`, {
        method: "POST", body: JSON.stringify({candidate_id: view.candidate.id, project_path: project,
          platform: $("#platform").value, cache_path: $("#cache-path")?.value.trim() || ""}),
      });
      sessionStorage.setItem(`fafnir.assetJob.${view.candidate.id}`, JSON.stringify({kind: "import", id: result.job.id}));
      await pollAssetJob(view, "import", result.job.id);
    }
  } catch (error) { view.status.textContent = assetFailure(error); }
  finally {
    assetOperations.delete(view.candidate.id);
    view.download.disabled = view.importButton.disabled = false;
  }
}

async function openAssetDetails(candidate, mode = "detail") {
  if ($("#asset-detail-dialog")) $("#asset-detail-dialog").close();
  const dialog = element("dialog", "asset-detail-dialog"); dialog.id = "asset-detail-dialog";
  const content = element("div", "asset-detail-content");
  const head = element("div", "modal-head");
  const heading = element("h2", "", candidate.title); heading.id = "asset-detail-title";
  dialog.setAttribute("aria-labelledby", heading.id);
  const close = element("button", "", "×"); close.type = "button";
  close.setAttribute("aria-label", "詳細を閉じる"); close.addEventListener("click", () => dialog.close());
  head.append(heading, close); content.append(head);
  const gallery = element("div", "asset-gallery");
  const images = element("button", "button secondary", "商品画像を見る"); images.type = "button";
  const drawImages = async () => {
    images.disabled = true;
    try {
      let urls = assetImages(candidate);
      if (!urls.length) {
        images.textContent = "商品画像を確認中…";
        const preview = await getAssetPreview(candidate);
        candidate = preview.candidate;
        urls = assetImages(candidate);
      }
      gallery.replaceChildren();
      for (const url of urls) {
        const img = element("img"); img.src = url; img.alt = `${candidate.title}の商品画像`;
        img.loading = "lazy"; img.referrerPolicy = "no-referrer";
        img.addEventListener("error", () => img.replaceWith(element("p", "muted", "画像を取得できませんでした")));
        gallery.append(img);
      }
      if (!urls.length) gallery.append(element("p", "muted", "公開商品画像がありません。商品ページで確認できます。"));
      images.hidden = true;
    } catch (error) { images.textContent = "画像を再取得"; gallery.textContent = error.message; }
    finally { images.disabled = false; }
  };
  images.addEventListener("click", drawImages);
  content.append(images, gallery, element("p", "asset-description", candidate.description || "説明は商品ページで確認できます。"));
  const link = element("a", "", "商品ページを開く ↗");
  link.href = safeUrl(candidate.url); link.target = "_blank"; link.rel = "noopener noreferrer";
  content.append(link);
  const label = element("label", "asset-project-label", "インポート先のUnityプロジェクト");
  const project = element("input"); project.type = "text"; project.value = $("#project-path").value;
  project.placeholder = "C:\\Projects\\MyGame"; label.append(project); content.append(label);
  const status = element("p", "asset-operation-status", "ダウンロードは共通キャッシュへ保存します。インポート時はUnityでファイル一覧を確認できます。");
  status.setAttribute("role", "status");
  const progress = element("progress"); progress.max = 1; progress.value = 0;
  progress.setAttribute("aria-label", "ダウンロード進捗");
  const actions = element("div", "asset-buttons");
  const download = element("button", "button secondary", "ダウンロード"); download.type = "button";
  const importButton = element("button", "button primary", "ダウンロード＆インポート…"); importButton.type = "button";
  const owned = ["owned", "installed"].includes(candidate.ownership)
    && (candidate.ownership_evidence || candidate.metadata?.ownership_evidence)?.verified === true;
  if (owned) actions.append(download, importButton);
  else status.textContent = "購入・所有確認後にダウンロードできます。商品ページを確認してください。";
  label.hidden = progress.hidden = !owned;
  content.append(status, progress, actions); dialog.append(content); document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), {once: true}); dialog.showModal();
  const view = {candidate, dialog, status, progress, project, download, importButton};
  download.addEventListener("click", () => runAssetAction(view, "download"));
  importButton.addEventListener("click", () => runAssetAction(view, "import"));
  if ($("#show-asset-images")?.checked) drawImages();
  if (assetOperations.has(candidate.id)) {
    Object.assign(assetOperations.get(candidate.id), view);
    download.disabled = importButton.disabled = true;
    status.textContent = "処理中の進捗を再表示しています…";
    return;
  }
  if (owned && sessionStorage.getItem(`fafnir.assetJob.${candidate.id}`)) runAssetAction(view, "resume");
  else if (owned && mode !== "detail") runAssetAction(view, mode);
}
