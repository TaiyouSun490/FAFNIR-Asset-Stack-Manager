# Stackforge

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
- Sync the signed-in Unity Editor account's complete visible and hidden My Assets
  list through the included Editor bridge, then index product names and tags for
  local retrieval.
- Search GitHub through its official REST API and OpenUPM through its registry API.
- Generate official Asset Store search links for missing capabilities.
- Save an Asset Store product as a purchase-unconfirmed candidate with the
  optional Chrome extension.
- Keep owned, purchase-unconfirmed, and community results in separate lanes so
  one source cannot crowd out the others.
- Prepare an approval-gated, exact-version OpenUPM manifest change with stale
  state checks and rollback support.
- Keep the catalog in a local SQLite database.

Stackforge does not purchase products, scrape Asset Store web pages, export a
Unity OAuth token, or automatically treat a local cache file as proof of
ownership. Asset Store products remain manual-install items. GitHub search
results require further package inspection before they can become executable
install plans.

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
game-stack scan-cache

# Import an export created by the Unity Editor bridge
game-stack sync-my-assets

# Search one lane
game-stack catalog --scope community --query networking
```

Set `GITHUB_TOKEN` if you need a higher GitHub Search API rate limit. Stackforge
does not load `.env` files automatically.

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
3. Click **My Assetsを同期**.
4. In Stackforge's Catalog, click **所有アセットを同期**.

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
