<p align="center">
  <img src="game_stack_planner/static/fafnir-header-logo.png" alt="Fafnir — Asset Stack Manager" width="920">
</p>

<p align="center">
  <strong>所有しているUnityアセットから、ゲームの実装構成を組み立てるローカルファーストのMCPツール。</strong>
</p>

<p align="center">
  日本語 ｜ <a href="README.en.md">English</a> ｜
  <a href="docs/USER_GUIDE.md">利用ガイド</a> ｜
  <a href="docs/MEDIA_CHECKLIST.md">スクリーンショット撮影表</a>
</p>

Fafnirは、「作りたいゲーム」を文章で渡すと、Unityの所有アセットを優先して候補を探し、
各候補の用途、互換性、不足機能、代替案をCodexやClaude Codeが判断できる形で返します。
手持ちで足りない役割だけ、GitHub、OpenUPM、購入未確認のAsset Store候補から補完します。

> **Alpha:** 推薦スコアは比較材料です。互換性、安全性、ライセンスを保証するものではありません。
> Unity版、Render Pipeline、対象Platform、ライセンス、導入差分は採用前に確認してください。

## 何ができるか

- Unity Editorへログインしているアカウントの**表示中・非表示を含むMy Assets**をローカルへ同期する
- 日本語の用途説明から、英語名を含む所有アセットを多言語Embeddingで検索する
- Unity ProjectのVersion、Render Pipeline、Input System、導入済みpackageと候補を照合する
- 所有アセット、購入未確認のAsset Store候補、GitHub／OpenUPMを混ぜずに比較する
- キャッシュ済み`.unitypackage`を展開・実行せず、script、asmdef、依存、pluginを静的検査する
- Codex／Claude CodeへMCPで根拠を渡し、具体的な用途と組み合わせを判断させる
- OpenUPM候補の導入差分を作り、確認後だけ固定versionで適用する

Fafnirは商品を購入せず、Unity認証tokenやCookieを取得せず、Asset Storeの検索結果一覧を巡回しません。
Asset Store商品の購入、Download、ImportはUnityの公式UIで行います。

## 全体の流れ

```text
Unity My Assets ─┐
Unity Project ───┼─> FafnirのローカルSQLite＋RAG索引
公開商品情報 ───┘                    │
                                     ├─> Web UI / CLI
                                     └─> MCP ─> Codex / Claude Code
                                                  │
                                                  └─> 用途・互換性・不足機能
```

Fafnirは検索と根拠整理を担当します。最終的な設計判断はMCP client側のLLMが行います。
ローカルWeb UIは大量の候補を目視確認するときの補助画面であり、MCP利用時は必須ではありません。

## 最短セットアップ

### 1. Fafnirをインストール

Python 3.12以上を使用します。

```powershell
git clone https://github.com/TaiyouSun490/FAFNIR-Asset-Stack-Manager.git
cd FAFNIR-Asset-Stack-Manager
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
fafnir --version
```

### 2. Unity My Assetsを同期

Unity Package Managerの **Add package from disk** で、次のファイルを選びます。

```text
unity_package/com.taiyousun.stackforge/package.json
```

Unity Hub／Editorへログインし、**Tools > Fafnir > My Assets Sync** を開いて
**My Assetsを同期**を押します。以後はUnity起動中に既定6時間ごとに同期します。
新しいアセットを購入した直後だけ、同じボタンで即時同期してください。

同期されるのは商品ID、表示名、Asset Store tag、購入・付与時刻、非表示状態、Unity版、
出力時刻です。Unity認証token、Cookie、画像、レビュー本文、アセット本体は出力しません。

### 3-A. Codexから使う

```powershell
codex mcp add fafnir -- fafnir mcp
codex mcp get fafnir
```

source checkoutを直接使う場合は、仮想環境のPythonを絶対pathで登録します。

```powershell
codex mcp add fafnir -- `
  C:\path\to\FAFNIR-Asset-Stack-Manager\.venv\Scripts\python.exe `
  -m game_stack_planner mcp
```

### 3-B. Claude Codeから使う

```powershell
claude mcp add --transport stdio --scope user fafnir -- fafnir mcp
claude mcp get fafnir
```

### 3-C. Web UIから使う

```powershell
fafnir ui
```

既定で`http://127.0.0.1:8770/`を開きます。Fafnirはloopbackだけで待ち受けます。

### 4. 索引状態を確認

UIまたはMCPの起動時に、固定revisionの`intfloat/multilingual-e5-small`を初回取得し、
所有アセットの索引をバックグラウンドで作成します。

```powershell
fafnir --json rag-status
```

- `hybrid_dense`: 現行世代の全件Embeddingが完成
- `lexical_fallback`: Dense索引の準備中。所有確認済み文書だけで暫定検索

部分的なベクトルをDense検索結果として返すことはありません。回線断やFafnir終了後も状態を保存し、
次回起動時に続きから処理します。

## AIへの依頼例

```text
Fafnirを使い、所有アセットを優先して、吹雪で孤立した山岳観測所を舞台にした
一人称ホラー探索ゲームの実装構成を作って。
停電、無線機の周波数パズル、雪上の足跡、吹雪、徘徊する怪異、体温管理、
持ち物管理、チェックポイント保存が必要。
各アセットの用途とUnity 6での互換性を説明し、不足機能だけ外部候補から補って。
```

短い検索も可能です。

```text
Fafnirで「ダンスやエモート時のカートゥーンVFX」を所有アセットから探して。
各候補を何に使えるか、Render Pipeline情報と一緒に説明して。
```

MCPで主に使用するtool:

| Tool | 用途 |
| --- | --- |
| `fafnir_status` | DB、My Assets同期、RAG、商品詳細取得の状態 |
| `search_owned_asset_rag` | 所有アセットの意味検索 |
| `retrieve_game_stack_evidence` | ゲーム要件に対する3レーンの比較材料 |
| `get_unity_asset_candidate` | 1候補の根拠と公開互換性情報 |
| `compare_asset_store_candidates` | 商品ID付きの実画像で複数候補を比較（ページ分割） |
| `validate_cached_asset_for_project` | キャッシュ済みpackageとProjectの照合 |
| `prepare_candidate_install` | 書き込み前の導入計画と差分 |

カタログの「比較に追加」→「画像で候補を比較」で、複数候補の画像・所有状態・版・容量を
並べ、候補ごとに選択／保留／見送りを記録できます。取得時は正確な商品の画像と取得計画を
別画面で確認します。選択だけでは取得せず、無回答は承認になりません。
[画像付き比較・承認の仕様と検証手順](docs/VISUAL_ASSET_APPROVAL.md)を参照してください。

## データはどこに保存され、何がAIへ渡るか

Unityブリッジの導入・更新は、対象Projectを指定して
diagnose_unity_bridge → prepare_unity_bridge_install → 差分承認 →
apply_reviewed_unity_bridge_install で行えます。MCP接続だけではUnityへの導入済みとは
判定しません。[導入・診断・復元手順](docs/UNITY_BRIDGE_SETUP.md)を参照してください。
ブリッジは配布物に同梱され、開発者個人の絶対パスには依存しません。

| データ | 既定の保存先 | AI clientへ送る内容 |
| --- | --- | --- |
| カタログ、RAG文書、ベクトル | `%LOCALAPPDATA%\game-stack-planner\catalog.sqlite3` | 検索に一致した候補の限定メタデータのみ |
| Unity My Assets出力 | `%LOCALAPPDATA%\game-stack-planner\unity-my-assets.json` | 採用候補として返された限定メタデータのみ |
| Embedding model | Hugging Faceのローカルcache | 送信しない |
| `.unitypackage`内容 | Unityのローカルcache | 絶対path、source本文、binaryを送信しない |

DB全体、ベクトル、認証情報はMCP応答へ含めません。
ブリッジ導入・診断の応答には、確認に必要な対象Projectのローカルpath、差分、
ファイルhash、一回限りの承認値が含まれます。公開issueやPRへ転記しないでください。
ただし、MCPが返した商品名、tag、説明の抜粋、互換性、scoreは、接続先AI serviceのmodel入力に
含まれる場合があります。完全なoffline利用にはCLIの`--offline`を使い、外部AIへ接続しないでください。
画像レビュー／比較で返す公開商品画像もAI clientへ渡ります。画像取得を避ける場合は
`visual_review=off`／`include_images=false`を指定してください。

通常の`git push`でローカルDBやMy Assets出力が送信されないよう、対象fileは`.gitignore`済みです。

## UI・CLI・MCPの使い分け

| 入口 | 向いている用途 |
| --- | --- |
| MCP | Codex／Claude Codeに用途、組み合わせ、不足機能を考えさせる |
| Web UI | 大量候補の閲覧、同期状態の確認、導入差分の目視確認 |
| CLI | 自動化、offline検索、JSON出力、診断と修復 |

代表的なCLI:

```powershell
fafnir scan C:\Projects\MyGame
fafnir recommend --prompt "2D deckbuilder" --offline
fafnir --json rag-search --query "浸水した通路の材質別足音" --limit 10
fafnir scan-cache --inspect
fafnir --json asset-details-status
```

## 現在の制限

- 推薦結果は互換性、security、licenseの保証ではない
- 公開商品pageがない廃止・非公開商品は、My Assets由来の名前とtagだけで検索する
- Unity Editor bridgeは内部Package Manager serviceへのadapterであり、Unity更新への追従が必要になる場合がある
- Asset Store商品の購入・Download・Importは自動化しない
- GitHub候補はpackage root、license、固定commitを確認できるまで自動導入しない
- `.unitypackage`の本番Projectへの自動importは行わない

## 詳細資料

- [利用ガイド：インストール、UI、CLI、Codex、Claude Code、トラブル対応](docs/USER_GUIDE.md)
- [検索sourceと所有証拠の扱い](docs/GAME_STACK_FEDERATED_SEARCH.md)
- [承認付き導入とrollbackの安全条件](docs/GAME_STACK_INSTALLATION.md)
- [構成生成の詳細](docs/GAME_STACK_PLANNER.md)
- [RAG実装状況と残る評価](docs/RAG_REMAINING_WORK.md)
- [Brand guide](docs/BRAND.md)
- [必要な画面素材と撮影条件](docs/MEDIA_CHECKLIST.md)

旧名からの互換性維持のため、module名`game_stack_planner`、data folder
`game-stack-planner`、Unity package ID`com.taiyousun.stackforge`、`stackforge.*` schema、
`game-stack` command aliasは変更していません。既存DBのmigrationやMy Assetsの再同期は不要です。

## 開発

```powershell
python -m pytest -q
```

## License

[MIT](LICENSE)。Unity、Unity Asset Store、GitHub、OpenUPM、Chromeは各社の製品または商標です。
Fafnirは各社の公式製品ではなく、提携・承認を受けたものでもありません。
