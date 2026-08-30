"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let lastResult = null;
let toastTimer = null;
let requirementCategories = [];
let activeInstallPlan = null;
let activeInstallNonce = "";
const INSTALL_STATUS_LABELS = {
  ready: "導入準備完了",
  manual: "手動導入が必要",
  needs_inspection: "導入可否の検査が必要",
  already_present: "導入済み",
  blocked: "導入できません",
  applied_waiting_for_unity: "適用済み・Unityの解決待ち",
};
const INSTALL_STATUS_COPY = {
  ready: "固定された変更差分とリスクを確認してから、導入を承認してください。",
  manual: "自動実行はしません。表示された確認事項に沿って手動で導入してください。",
  needs_inspection: "package.json、導入パス、固定コミットを検査できるまで自動導入しません。",
  already_present: "この候補は対象プロジェクトにすでに導入されています。",
  blocked: "安全条件を満たしていないため、この計画は実行できません。",
  applied_waiting_for_unity: "manifest.jsonへ適用しました。Unityによる依存関係の解決とコンパイル結果を確認してください。",
};
const SEARCH_LANES = [
  {key: "owned_assets", label: "手持ち・ローカル"},
  {key: "asset_store_market", label: "購入未確認候補"},
  {key: "community", label: "GitHub / OpenUPM"},
];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch {
    return "";
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? {"Content-Type": "application/json"} : {},
    ...options,
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(value.error?.message || `HTTP ${response.status}`);
  }
  return value;
}

function showToast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 2400);
}

function sourceLabel(source) {
  return {local: "LOCAL", github: "GITHUB", openupm: "OPENUPM", asset_store: "ASSET STORE", asset_store_cache: "ASSET CACHE"}[source] || source.toUpperCase();
}

function inventoryLabel(candidate) {
  if (candidate.ownership_evidence?.kind === "unity_editor_my_assets") {
    return "Unity My Assets";
  }
  return {
    confirmed_owned: "自己申告の所有",
    locally_cached: "ローカルキャッシュ",
    project_present: "プロジェクト導入済み",
    unknown: candidate.scope === "asset_store_market" ? "購入未確認" : "",
  }[candidate.inventory_state] || "";
}

function chips(values, target, className = "") {
  values.filter(Boolean).forEach((value) => {
    target.append(element("span", `chip ${className}`.trim(), value));
  });
}

function installActionSpec(candidate) {
  const specs = {
    openupm: {label: "導入内容を確認", disabled: false},
    github: {label: "導入可否を調べる", disabled: false},
    asset_store: {label: "手動導入", disabled: false},
    asset_store_cache: {label: "手動導入", disabled: false},
    local: {label: "導入済み", disabled: true},
  };
  return specs[candidate.source] || {label: "導入可否を調べる", disabled: false};
}

function installActionButton(candidate) {
  const spec = installActionSpec(candidate);
  const button = element("button", "install-action-button", spec.label);
  button.type = "button";
  button.disabled = spec.disabled;
  if (spec.disabled) {
    button.setAttribute("aria-label", `${candidate.title}：導入済み`);
  } else {
    button.addEventListener("click", () => prepareInstall(candidate, button));
  }
  return button;
}

function detailText(value, fallback = "—") {
  if (Array.isArray(value)) return value.filter(Boolean).join(" / ") || fallback;
  if (value && typeof value === "object") return JSON.stringify(value, null, 2);
  if (value === undefined || value === null || value === "") return fallback;
  return String(value);
}

function safeInstallStatus(value) {
  return Object.hasOwn(INSTALL_STATUS_LABELS, value) ? value : "blocked";
}

function renderInstallStatus(status) {
  const safeStatus = safeInstallStatus(status);
  const node = $("#install-status");
  node.className = `install-status status-${safeStatus}`;
  node.textContent = INSTALL_STATUS_LABELS[safeStatus];
  $("#install-state-copy").textContent = INSTALL_STATUS_COPY[safeStatus];
  return safeStatus;
}

function renderInstallPlan(payload) {
  const plan = payload?.plan;
  if (!plan || !plan.id) throw new Error("導入計画の応答が不完全です。");
  activeInstallPlan = plan;
  activeInstallNonce = payload.approval_nonce || "";
  const status = renderInstallStatus(plan.status);
  $("#install-title").textContent = plan.candidate?.title || "導入内容を確認";
  $("#install-kind").textContent = detailText(plan.kind);
  $("#install-target").textContent = detailText(
    [plan.project?.path, plan.project?.unity_version].filter(Boolean),
  );
  $("#install-package").textContent = detailText(plan.package?.name, "対象外");
  $("#install-version").textContent = detailText(plan.package?.version, "対象外");
  $("#install-license").textContent = detailText(plan.candidate?.license, "未確認");
  $("#install-manifest-diff").textContent = detailText(
    plan.manifest?.diff,
    "manifest.json の自動変更はありません。",
  );
  const risks = $("#install-risks");
  risks.replaceChildren();
  const riskItems = Array.isArray(plan.risks) ? plan.risks.filter(Boolean) : [];
  if (riskItems.length) {
    riskItems.forEach((risk) => risks.append(element("li", "", detailText(risk))));
  } else {
    risks.append(element("li", "install-no-risk", "追加のリスクは報告されていません。"));
  }
  $("#install-verification").textContent = detailText(
    plan.verification,
    "Unityを開き、Package Managerの解決結果とコンパイル結果を確認してください。",
  );
  $("#install-rollback").textContent = detailText(
    plan.rollback_limit,
    "適用後にmanifest.jsonが別途変更された場合、自動ロールバックはできません。",
  );
  $("#install-message").textContent = status === "ready" && !activeInstallNonce
    ? "承認情報がないため実行できません。計画を作り直してください。"
    : "";
  $("#install-result").textContent = "";
  $("#install-result-section").classList.add("hidden");
  const confirm = $("#confirm-install-button");
  confirm.hidden = status !== "ready";
  confirm.disabled = status !== "ready" || !activeInstallNonce;
  confirm.textContent = "承認して導入";
  $("#install-dialog").showModal();
}

async function prepareInstall(candidate, button) {
  const projectPath = $("#project-path").value.trim();
  if (!projectPath) {
    showToast("先にUnityプロジェクトのパスを入力してください");
    $("#project-path").focus();
    return;
  }
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "確認中…";
  try {
    const payload = await api("/api/install/prepare", {
      method: "POST",
      body: JSON.stringify({candidate_id: candidate.id, project_path: projectPath}),
    });
    renderInstallPlan(payload);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function closeInstallDialog() {
  $("#install-dialog").close();
}

async function executeInstall(event) {
  event.preventDefault();
  if (!activeInstallPlan || activeInstallPlan.status !== "ready" || !activeInstallNonce) return;
  const confirm = $("#confirm-install-button");
  confirm.disabled = true;
  confirm.textContent = "適用中…";
  $("#install-message").textContent = "承認済みの計画を検証して適用しています。";
  try {
    const payload = await api("/api/install/execute", {
      method: "POST",
      body: JSON.stringify({plan_id: activeInstallPlan.id, approval_nonce: activeInstallNonce}),
    });
    const job = payload?.job || {};
    const status = renderInstallStatus(job.status || "blocked");
    $("#install-message").textContent = INSTALL_STATUS_COPY[status];
    $("#install-result").textContent = detailText(job.result, "結果の詳細はありません。");
    $("#install-result-section").classList.remove("hidden");
    confirm.hidden = true;
    activeInstallNonce = "";
    showToast(status === "applied_waiting_for_unity"
      ? "manifest.jsonへ適用しました。Unityで確認してください"
      : INSTALL_STATUS_LABELS[status]);
  } catch (error) {
    activeInstallNonce = "";
    $("#install-message").textContent = `${error.message} 計画を作り直してください。`;
    confirm.disabled = true;
    confirm.textContent = "計画を作り直してください";
  }
}

function updateStats(summary = {}) {
  $("#stat-total").textContent = summary.total ?? 0;
  $("#stat-owned").textContent = summary.owned_assets ?? 0;
  $("#stat-market").textContent = summary.asset_store_market ?? 0;
  $("#stat-community").textContent = summary.community ?? 0;
  $("#rag-count").textContent = summary.asset_rag_owned ?? 0;
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    $("#health").classList.add("ready");
    $("#health span").textContent = `LOCAL · v${status.version}`;
    requirementCategories = Array.isArray(status.requirement_categories)
      ? status.requirement_categories
      : [];
    renderPinCategories();
    const llmConfigured = status.capabilities?.llm_configured === true;
    $("#use-llm").disabled = !llmConfigured;
    $("#llm-copy").textContent = llmConfigured
      ? "ゲーム案と候補の商品名・タグをOpenAI APIへ送信（明示的にオン）"
      : "OPENAI_API_KEYを設定してサーバーを再起動すると利用できます";
    updateStats(status.catalog);
  } catch {
    $("#health span").textContent = "接続エラー";
  }
}

function renderPinCategories(selected = []) {
  const root = $("#pin-categories");
  if (!root) return;
  const selectedKeys = new Set(selected);
  root.replaceChildren();
  requirementCategories.forEach((category) => {
    const label = element("label", "category-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "pin-category";
    input.value = category.key;
    input.checked = selectedKeys.has(category.key);
    label.append(input, element("span", "", category.title));
    root.append(label);
  });
}

function updatePinOwnershipFields() {
  const owned = $("#pin-ownership").value === "owned";
  const fields = $("#pin-rag-fields");
  const alias = $("#pin-user-alias");
  const confirmation = $("#pin-purchase-confirmation");
  fields.classList.toggle("hidden", !owned);
  fields.disabled = !owned;
  confirmation.required = owned;
  $("#pin-submit-button").textContent = owned
    ? "購入済みRAGへ登録"
    : "ローカルへ保存";
  if (!owned) {
    alias.value = "";
    confirmation.checked = false;
  }
}

function openPinDialog(categoryKey = "") {
  $("#pin-form").reset();
  renderPinCategories(categoryKey ? [categoryKey] : []);
  updatePinOwnershipFields();
  const category = requirementCategories.find((item) => item.key === categoryKey);
  $("#pin-category-hint").textContent = category
    ? "「" + category.title + "」の候補として保存します。必要なら他の機能も選べます。"
    : "この商品で補える機能を選ぶと、次回の構成案で候補として再利用できます。";
  $("#pin-message").textContent = "";
  $("#pin-dialog").showModal();
  $("#pin-url").focus();
}

function renderProject(project) {
  const box = $("#project-summary");
  box.replaceChildren();
  box.classList.remove("hidden");
  box.append(element("strong", "", project.product_name || project.name));
  box.append(element("div", "", `${project.unity_version || "Unity version不明"} · ${project.render_pipeline.toUpperCase()} · Input: ${project.input_backend}`));
  const chipBox = element("div", "chips");
  chips([`${project.packages.length} packages`, ...project.warnings.map(() => "WARNING")], chipBox);
  box.append(chipBox);
  if (project.warnings.length) box.append(element("div", "risk", project.warnings.join(" / ")));
}

function renderRequirements(result) {
  const root = $("#requirements");
  root.replaceChildren();
  result.requirements.forEach((req) => {
    const card = element("article", "requirement");
    const head = element("header");
    head.append(element("h4", "", req.title));
    head.append(element("span", "priority", req.priority));
    card.append(head, element("p", "", req.rationale));
    root.append(card);
  });
}

function renderPlans(result) {
  const root = $("#plans");
  root.replaceChildren();
  result.plans.forEach((plan, index) => {
    const card = element("article", `plan ${index === 0 ? "recommended" : ""}`);
    card.append(element("div", "coverage", `${plan.coverage}%`));
    card.append(element("h4", "", plan.title));
    card.append(element("p", "", plan.description));
    const list = element("div", "stack-items");
    plan.selected.forEach((item) => {
      const row = element("article", "stack-item");
      const heading = element("h5", "", item.candidate.title);
      const meta = element("div", "meta");
      chips([
        sourceLabel(item.candidate.source),
        inventoryLabel(item.candidate),
        ...item.requirement_titles,
      ], meta);
      row.append(heading, meta);
      if (Array.isArray(item.usage)) {
        item.usage.forEach((usage) => {
          row.append(element("p", "stack-use", `${usage.role}：${usage.use_case}`));
          if (usage.integration) row.append(element("p", "stack-integration", `組み込み：${usage.integration}`));
        });
      } else {
        row.append(element("p", "stack-use", `${item.requirement_titles.join(" / ")}を担当する候補`));
      }
      list.append(row);
    });
    if (!plan.selected.length) list.append(element("p", "muted", "関連性を確認できる候補が不足しています"));
    card.append(list);
    if (plan.missing?.length) card.append(element("p", "risk", `未充足：${plan.missing.join(" / ")}`));
    root.append(card);
  });
}

function candidateCard(item, index) {
  const candidate = item.candidate;
  const card = element("article", "candidate");
  card.append(element("div", "rank", String(index + 1).padStart(2, "0")));
  const body = element("div");
  const title = element("h5");
  const url = safeUrl(candidate.url);
  if (url) {
    const link = element("a", "", candidate.title);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    title.append(link);
  } else {
    title.textContent = candidate.title;
  }
  body.append(title);
  const meta = element("div", "meta");
  chips([
    sourceLabel(candidate.source),
    inventoryLabel(candidate),
    candidate.license || "",
    candidate.version ? `v${candidate.version}` : "",
  ], meta, candidate.ownership !== "candidate" ? "owned" : "source");
  body.append(meta);
  if (item.reasons?.length) body.append(element("div", "reason", `根拠：${item.reasons.join(" / ")}`));
  if (item.risks?.length) body.append(element("div", "risk", `確認：${item.risks.join(" / ")}`));
  const actions = element("div", "candidate-actions");
  actions.append(installActionButton(candidate));
  body.append(actions);
  card.append(body, element("div", "score", String(Math.round(item.score))));
  return card;
}

function renderRecommendations(result) {
  const root = $("#recommendations");
  root.replaceChildren();
  result.requirements.forEach((req) => {
    const group = element("section", "recommendation-group");
    group.append(element("h4", "", req.title));
    const options = result.recommendations?.[req.key] || [];
    const list = element("div", "recommendation-list");
    if (!options.length) list.append(element("p", "muted", "この要件に関連すると確認できる候補はまだありません。"));
    options.slice(0, 12).forEach((item, index) => list.append(candidateCard(item, index)));
    group.append(list);
    root.append(group);
  });
}

function renderAssetSearches(result) {
  const root = $("#asset-searches");
  root.replaceChildren();
  result.asset_store_searches.forEach((search) => {
    const link = element("a", "", `${search.title}を検索 ↗`);
    link.href = safeUrl(search.url);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const save = element("button", "", "見つけた商品を保存");
    save.type = "button";
    save.addEventListener("click", () => openPinDialog(search.requirement));
    const row = element("div", "asset-search");
    row.append(link, save);
    root.append(row);
  });
}

function renderResult(result) {
  lastResult = result;
  $("#empty-state").classList.add("hidden");
  $("#results").classList.remove("hidden");
  renderRequirements(result);
  renderPlans(result);
  renderRecommendations(result);
  renderAssetSearches(result);
  if (result.project) renderProject(result.project);
  const states = Object.entries(result.source_status)
    .map(([name, state]) => `${name}: ${state.status}`)
    .join(" · ");
  $("#form-message").textContent = `${result.elapsed_ms} ms · ${states}`;
}

async function scanProject() {
  const path = $("#project-path").value.trim();
  if (!path) {
    $("#form-message").textContent = "Unityプロジェクトのパスを入力してください。";
    return;
  }
  const button = $("#scan-button");
  button.disabled = true;
  try {
    const value = await api("/api/project/scan", {
      method: "POST",
      body: JSON.stringify({path}),
    });
    renderProject(value.project);
    updateStats(value.catalog);
    localStorage.setItem("stackforge.projectPath", path);
    showToast("Unityプロジェクトを診断しました");
  } catch (error) {
    $("#form-message").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function analyze() {
  const prompt = $("#prompt").value.trim();
  if (!prompt) {
    $("#form-message").textContent = "作りたいゲームを入力してください。";
    $("#prompt").focus();
    return;
  }
  const button = $("#analyze-button");
  button.disabled = true;
  button.querySelector("span").textContent = "検索・分析中…";
  $("#form-message").textContent = "要件を分解し、候補を照合しています。";
  const preferences = {
    projectPath: $("#project-path").value.trim(),
    platform: $("#platform").value,
    budget: $("#budget").value,
  };
  localStorage.setItem("stackforge.preferences", JSON.stringify(preferences));
  try {
    const result = await api("/api/recommend", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        project_path: preferences.projectPath,
        platform: preferences.platform,
        budget: preferences.budget,
        remote: $("#remote").checked,
        use_llm: $("#use-llm").checked,
      }),
    });
    renderResult(result);
    window.scrollTo({top: $(".workspace").offsetTop - 80, behavior: "smooth"});
  } catch (error) {
    $("#form-message").textContent = error.message;
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "構成案をつくる";
  }
}

function catalogCard(candidate) {
  const card = element("article", "catalog-card");
  const labels = element("div");
  chips([sourceLabel(candidate.source)], labels, "source");
  chips([inventoryLabel(candidate)], labels, "owned");
  if (candidate.metadata?.rag_indexed === true) {
    chips(["購入済みRAG"], labels, "rag");
  }
  chips(candidate.categories || [], labels);
  card.append(labels, element("h3", "", candidate.title));
  card.append(element("p", "", candidate.description || "説明は保存されていません。"));
  const footer = element("footer");
  const info = element("span", "meta", candidate.license || candidate.version || "");
  footer.append(info);
  const actions = element("div", "catalog-actions");
  actions.append(installActionButton(candidate));
  const url = safeUrl(candidate.url);
  if (url) {
    const link = element("a", "", "開く ↗");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    actions.append(link);
  }
  footer.append(actions);
  card.append(footer);
  return card;
}

async function loadCatalog() {
  const params = new URLSearchParams({
    q: $("#catalog-query").value.trim(),
    source: $("#catalog-source").value,
    ownership: $("#catalog-ownership").value,
    scope: $("#catalog-scope").value,
    limit: "200",
  });
  try {
    const value = await api(`/api/catalog?${params}`);
    $("#catalog-count").textContent = value.count;
    const root = $("#catalog-items");
    root.replaceChildren();
    if (!value.items.length) root.append(element("p", "muted", "該当するアイテムはありません。"));
    value.items.forEach((item) => root.append(catalogCard(item)));
    updateStats(value.summary);
  } catch (error) {
    showToast(error.message);
  }
}

async function scanAssetStoreCache() {
  const button = $("#scan-cache-button");
  const message = $("#cache-message");
  button.disabled = true;
  message.textContent = "ローカルキャッシュを確認中…";
  try {
    const result = await api("/api/asset-store/cache/scan", {
      method: "POST",
      body: JSON.stringify({path: $("#cache-path").value.trim()}),
    });
    const suffix = result.scan.ownership_confirmed ? "" : "（所有未確認）";
    message.textContent = `${result.scan.found}件を取り込みました${suffix}`;
    updateStats(result.catalog);
    await loadCatalog();
    showToast("Unityローカル資産を更新しました");
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function showView(viewName, {scope = "", load = true} = {}) {
  const safeView = viewName === "catalog" ? "catalog" : "planner";
  $$(".tab").forEach((item) => item.classList.toggle(
    "active",
    item.dataset.view === safeView,
  ));
  $$(".view").forEach((view) => view.classList.remove("active"));
  $(`#${safeView}-view`).classList.add("active");
  if (safeView === "catalog") {
    $("#catalog-scope").value = SEARCH_LANES.some((lane) => lane.key === scope)
      ? scope
      : "";
    if (load) loadCatalog();
  }
}

async function syncUnityMyAssets() {
  const button = $("#sync-my-assets-button");
  const message = $("#my-assets-message");
  button.disabled = true;
  message.textContent = "Unity Editorの同期ファイルを確認中…";
  try {
    const result = await api("/api/asset-store/my-assets/sync", {
      method: "POST",
      body: JSON.stringify({path: $("#my-assets-path").value.trim()}),
    });
    message.textContent = `${result.sync.imported}件を所有アセットとして同期しました`;
    updateStats(result.catalog);
    await loadCatalog();
    const url = new URL(window.location.href);
    url.searchParams.set("view", "catalog");
    url.searchParams.set("scope", "owned_assets");
    url.searchParams.delete("sync");
    window.history.replaceState({}, "", url);
    showToast("Unity My Assetsを同期しました");
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function pinAsset(event) {
  event.preventDefault();
  const message = $("#pin-message");
  const ownership = $("#pin-ownership").value;
  const owned = ownership === "owned";
  const purchaseConfirmation = $("#pin-purchase-confirmation");
  const userAlias = $("#pin-user-alias").value;
  const notes = $("#pin-notes").value;
  const categories = $$("#pin-categories input:checked").map((input) => input.value);
  if (owned && !userAlias.trim() && !notes.trim() && !categories.length) {
    message.textContent = "AI用の別名、メモ、機能カテゴリのいずれかを入力してください。";
    $("#pin-user-alias").focus();
    return;
  }
  if (owned && !purchaseConfirmation.checked) {
    message.textContent = "購入済みであることを自己申告で確認してください。";
    purchaseConfirmation.focus();
    return;
  }
  message.textContent = "保存中…";
  try {
    const payload = {
      url: $("#pin-url").value,
      title: $("#pin-title").value,
      notes,
      categories,
    };
    if (owned) {
      payload.user_alias = userAlias;
      payload.purchase_confirmation = true;
    } else {
      payload.ownership = ownership;
    }
    const result = await api(owned ? "/api/asset-store/rag" : "/api/catalog/manual", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    updateStats(result.catalog);
    $("#pin-dialog").close();
    $("#pin-form").reset();
    message.textContent = "";
    await loadCatalog();
    showToast(owned
      ? "購入済みアセットをRAGへ登録しました"
      : "Asset Store商品を保存しました");
  } catch (error) {
    message.textContent = error.message;
  }
}

function downloadResult() {
  if (!lastResult) return;
  const blob = new Blob([JSON.stringify(lastResult, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `game-stack-plan-${lastResult.run_id || "draft"}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

function restorePreferences() {
  $("#project-path").value = localStorage.getItem("stackforge.projectPath") || "";
  try {
    const value = JSON.parse(localStorage.getItem("stackforge.preferences") || "{}");
    if (value.projectPath) $("#project-path").value = value.projectPath;
    if (value.platform) $("#platform").value = value.platform;
    if (value.budget) $("#budget").value = value.budget;
  } catch {
    localStorage.removeItem("stackforge.preferences");
  }
}

function bind() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    showView(tab.dataset.view);
  }));
  $$(".examples button").forEach((button) => button.addEventListener("click", () => {
    $("#prompt").value = button.dataset.example;
    $("#prompt").focus();
  }));
  $("#scan-button").addEventListener("click", scanProject);
  $("#analyze-button").addEventListener("click", analyze);
  $("#catalog-search-button").addEventListener("click", loadCatalog);
  $("#scan-cache-button").addEventListener("click", scanAssetStoreCache);
  $("#sync-my-assets-button").addEventListener("click", syncUnityMyAssets);
  $("#catalog-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadCatalog();
  });
  $("#open-pin-button").addEventListener("click", () => openPinDialog());
  $("#close-pin-button").addEventListener("click", () => $("#pin-dialog").close());
  $("#pin-ownership").addEventListener("change", updatePinOwnershipFields);
  $("#pin-form").addEventListener("submit", pinAsset);
  $("#close-install-button").addEventListener("click", closeInstallDialog);
  $("#cancel-install-button").addEventListener("click", closeInstallDialog);
  $("#install-form").addEventListener("submit", executeInstall);
  $("#install-dialog").addEventListener("close", () => {
    activeInstallPlan = null;
    activeInstallNonce = "";
  });
  $("#download-button").addEventListener("click", downloadResult);
  $("#copy-button").addEventListener("click", async () => {
    if (!lastResult) return;
    await navigator.clipboard.writeText(JSON.stringify(lastResult, null, 2));
    showToast("JSONをコピーしました");
  });
}

restorePreferences();
bind();
const initialRoute = new URLSearchParams(window.location.search);
const initialView = initialRoute.get("view") || "planner";
const initialScope = initialRoute.get("scope") || "";
showView(initialView, {scope: initialScope, load: false});
loadStatus().then(async () => {
  if (initialView !== "catalog") return;
  if (initialRoute.get("sync") === "my-assets") {
    await syncUnityMyAssets();
  } else {
    await loadCatalog();
  }
});
