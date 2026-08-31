# Stackforge

[日本語の利用ガイド](docs/USER_GUIDE.md)

Stackforge is a local-first Unity game stack planner. It turns a game idea and
an optional Unity project into an evidence-backed comparison across three
independent lanes:

- assets and packages you already have;
- Unity Asset Store products you have not confirmed as purchased;
- public GitHub repositories and OpenUPM packages.

The local web UI is in Japanese. The Python package, CLI, and JSON responses can
also be used from automation.

> **Alpha:** recommendation scores are comparison aids, not compatibility or
> security guarantees. Review licenses, Unity versions, render pipelines,
> platform support, and the exact project diff before adopting third-party code.

## What works today

- Scan `Packages/manifest.json`, `packages-lock.json`, and Unity project settings.
- Detect downloaded `.unitypackage` files in the local `Asset Store-5.x` cache.
- Inspect cached `.unitypackage` archives without extracting or executing them,
  including scripts, asmdefs, dependencies, pipeline/input markers, and plugins.
- Sync the signed-in Unity Editor account's complete visible and hidden My Assets
  list through the included Editor bridge, then index product names and tags for
  local retrieval.
- Search GitHub through its official REST API and OpenUPM through its registry API.
- Generate official Asset Store search links for missing capabilities.
- Save an Asset Store product as a purchase-unconfirmed candidate with the
  optional Chrome extension.
- Differentially enrich known Asset Store products from Unity's public product
  pages with descriptions, publisher/category, versions, compatibility,
  dependencies, price, and aggregate rating.
- Compare official and locally inspected compatibility data against a selected
  Unity project; optionally compile in a disposable staging project.
- Keep owned, purchase-unconfirmed, and community results in separate lanes so
  one source cannot crowd out the others.
- Prepare an approval-gated, exact-version OpenUPM manifest change with stale
  state checks and rollback support.
- Keep the catalog in a local SQLite database.
- Build local multilingual text embeddings for AI-safe owned-asset fields and
  rank RAG results by cosine similarity; report explicit index state until the
  complete current generation is ready.
- Expose the local catalog and approval-gated installer as MCP tools so an MCP
  client such as Codex can judge concrete uses and combinations itself.

Stackforge does not purchase products, export a Unity OAuth token, or
automatically treat a local cache file as proof of ownership. It fetches only
public metadata for product IDs already in the local catalog through a resumable,
rate-limited queue; it does not enumerate search results, use cookies, or
download asset contents. Asset Store products remain manual-install items.

## Quick start

Python 3.12 or newer is required.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
game-stack ui
```

Open `http://127.0.0.1:8770/` if the browser does not open automatically.
Stackforge listens on loopback only.

```powershell
# Inspect a Unity project
game-stack scan C:\Projects\MyGame

# Compare all three source lanes
game-stack recommend `
  --prompt "Quest向け4人協力ローグライト" `
  --project C:\Projects\MyGame `
  --platform quest

# Work entirely from the saved local catalog
game-stack recommend --prompt "2D deckbuilder" --offline

# Inspect downloaded Asset Store packages without claiming purchase ownership
game-stack scan-cache --inspect

# Import an export created by the Unity Editor bridge
game-stack sync-my-assets

# UI and MCP startup automatically download the pinned model once and
# build/update the owned-asset embedding index in the background.
# Inspect progress or explicitly repair/rebuild the index:
game-stack --json rag-status
game-stack --json rag-index

# Public product metadata coverage and an explicit bounded refresh
game-stack --json asset-details-status
game-stack --json asset-details-sync --limit 20

# Validate one cached product against a project; staging compile is opt-in
game-stack --json validate-asset asset_store:12345 --project C:\Projects\MyGame
game-stack --json validate-asset asset_store:12345 --project C:\Projects\MyGame --compile

# Search one lane
game-stack catalog --scope community --query networking
```

Set `GITHUB_TOKEN` if you need a higher GitHub Search API rate limit. Stackforge
does not load `.env` files automatically.

### MCP integration

Stackforge can run as a local stdio MCP server. The MCP client performs the LLM
reasoning; Stackforge never needs an OpenAI API key.

```powershell
codex mcp add stackforge -- game-stack mcp
codex mcp get stackforge
```

For a source checkout, use the checkout's Python interpreter and module command
instead of relying on a globally installed `game-stack` executable:

```powershell
codex mcp add stackforge -- `
  C:\path\to\.venv\Scripts\python.exe -m game_stack_planner mcp
```

The server exposes read-only status, catalog/RAG search, exact candidate lookup,
and full game-stack evidence retrieval. Installation remains two-stage: first
prepare and review an exact plan, then apply its one-time approval nonce. Asset
Store items remain manual-install only. Bounded product metadata leaves the local
process only when the connected MCP client includes tool results in a model
request. Credentials, local paths, vectors, images, and downloaded asset
contents are never returned.

Normal UI and MCP startup automatically imports a changed Unity My Assets export,
then starts a detached index worker for the pinned `intfloat/multilingual-e5-small`
model. The worker survives a short-lived MCP session and resumes the official
Hugging Face HTTP cache on slow links. `stackforge_status`, `rag-status`, and the
local UI expose the state, active generation, progress, coverage, and bounded
failure message. Until the dense generation reaches 100% coverage, owned-asset
search remains usable through an explicitly labeled `lexical_fallback`; it never
presents partial vectors or lexical rank as dense similarity. Once ready, results
use `hybrid_dense`: cosine similarity remains visible as `score`, while the final
`rank_score` also preserves strong owned-catalog name, tag, and requirement matches.

`rag-index` is a synchronous CLI repair command. The MCP tool
`reindex_owned_asset_rag` only queues the same work and returns immediately.
Custom E5-compatible models require both `STACKFORGE_TEXT_EMBEDDING_MODEL` and an
immutable `STACKFORGE_TEXT_EMBEDDING_REVISION`. Stored vectors are isolated by
model revision, tokenizer/pooling pipeline, and token limit. Vectors are never
included in MCP or HTTP responses. `STACKFORGE_RAG_MIN_SIMILARITY` changes the
visible default relevance threshold (default `0.72`) without a code edit.

### Is the local web UI required?

No. MCP covers status, retrieval, stack evidence, product-detail refresh,
project compatibility checks, and approval-gated installation. The redesigned
local UI is an optional workbench for visually browsing the asset index and
reviewing large results. The Asset Store website is still needed for purchasing,
reading full license or review text, and importing through Unity's supported UI.

## Chrome extension

The optional extension captures only the current official Asset Store product
URL, tab title, and fields entered by the user. It does not read page HTML,
cookies, descriptions, images, prices, or search listings.

See [browser_extension/unity_asset_store/README.md](browser_extension/unity_asset_store/README.md)
for unpacked-extension and Native Messaging setup.

## Unity My Assets sync

Install the embedded package from
`unity_package/com.taiyousun.stackforge/package.json` with Unity Package
Manager's **Add package from disk** command. Then:

1. Sign in to Unity Hub / Unity Editor.
2. Open **Tools > Stackforge > My Assets Sync**.
3. Keep the default periodic sync enabled (6 hours), adjust its visible interval,
   or click **My Assetsを同期** for an immediate post-purchase refresh.
4. Keep Stackforge running or start it later; it imports the changed export and
   queues differential indexing automatically. The Catalog button is a manual
   repair/check path, not a required second sync step.

The Editor bridge writes a versioned JSON file under the user's local application
data directory. It includes only product ID, display name, Asset Store tags,
purchase/grant time, hidden state, Unity version, and export time. Stackforge
does not require Asset Inventory or a Chrome extension for this workflow.

The bridge is an adapter over Unity Editor's undocumented internal Package
Manager service, so a future Unity release can require an adapter update. Its
design follows Unity's published reference source for the
[service container](https://github.com/Unity-Technologies/UnityCsReference/blob/master/Modules/PackageManagerUI/Editor/Services/ServicesContainer.cs),
[My Assets REST service](https://github.com/Unity-Technologies/UnityCsReference/blob/master/Modules/PackageManagerUI/Editor/Services/AssetStore/AssetStoreRestAPI.cs),
and [purchase result model](https://github.com/Unity-Technologies/UnityCsReference/blob/master/Modules/PackageManagerUI/Editor/Services/AssetStore/AssetStorePurchases.cs).
That reference source is not copied or redistributed by Stackforge.

## Documentation

- [日本語の利用ガイド（UI・CLI・Codex・Claude・ローカルRAG）](docs/USER_GUIDE.md)
- [Planner workflow](docs/GAME_STACK_PLANNER.md)
- [Federated source model](docs/GAME_STACK_FEDERATED_SEARCH.md)
- [Approval-gated installation](docs/GAME_STACK_INSTALLATION.md)

## Development

```powershell
python -m pytest -q
```

The tests cover source separation, URL normalization, cache semantics, project
scanning, recommendation relevance, Native Messaging validation, install-plan
integrity, rollback checks, and static UI contracts.

## License

[MIT](LICENSE). Unity, Unity Asset Store, GitHub, OpenUPM, and Chrome are products
or trademarks of their respective owners. Stackforge is not affiliated with or
endorsed by them.
