# Fafnir Asset Stack Manager

Fafnir Asset Stack Managerは、作りたいゲームの説明とローカルのUnityプロジェクトから、必要機能と実装候補を整理する独立ツールです。カタログはローカルのSQLiteデータベースへ保存します。

## 起動

```powershell
cd C:\path\to\stackforge
.\venv\Scripts\Activate.ps1
game-stack ui
```

ブラウザで `http://127.0.0.1:8770/` が開きます。ブラウザを自動で開かない場合は `game-stack ui --no-browser` を使います。

GUIでは次の順に使います。

1. 作りたいゲームを文章で入力する。
2. ターゲットと優先方針を選ぶ。
3. 必要ならUnityプロジェクトのルート（`Packages` と `ProjectSettings` があるフォルダー）を入力して「診断」を押す。
4. 「構成を生成」を押す。
5. 要件、3種類の選定候補、候補ごとの根拠と注意点を比較する。

## データソース

- Local: `Packages/manifest.json`、`packages-lock.json`、Unityのプロジェクト設定から導入済み構成を読み取ります。
- GitHub: 公式REST APIで公開リポジトリを検索します。環境変数 `GITHUB_TOKEN` がある場合は認証付きで利用します。
- OpenUPM: 公式パッケージレジストリの検索APIを利用します。
- Unity Asset Store（所有・購入未確認候補）: 所有確認はUnity Editorブリッジ、商品説明・版・互換性表・依存・価格・集計評価は公式公開商品ページから取得します。検索結果一覧は巡回せず、ローカルカタログにある商品IDだけを低速・差分更新します。
- Unity My Assets（所有一覧）: 付属のUnity Editorブリッジが、ログイン中のPackage Managerサービスから所有商品をページング取得します。

### 所有アセットの自動同期

Unity Package Managerの「Add package from disk」で
`unity_package/com.taiyousun.stackforge/package.json` を選びます。Unity Hub / Editorへ
ログインした状態で `Tools > Fafnir > My Assets Sync` を開きます。既定ではUnity起動中に
6時間ごとに同期し、画面で1〜168時間へ変更できます。購入直後は「My Assetsを同期」で即時更新
できます。FafnirのUIまたはMCPを起動しておけば、変更を検出して全件を
ローカルSQLiteとRAGへ差分登録します。非表示にした商品も別ページとして取得し、商品IDで
重複を除きます。画面の「今すぐ同期を確認」は修復用で、通常の二度目の操作ではありません。

自動同期はUnityのMy Assets応答に含まれる商品名とタグをまず登録し、その後、公式公開
商品ページの説明・主要機能・互換性情報を検索文書へ差分反映します。商品ID、購入日時、
非表示状態、同期時刻は所有状態の管理用メタデータとして保持します。Unity認証トークン、
Cookie、画像、レビュー本文、ダウンロードしたアセットの実データは出力・登録しません。

同期ファイルは既定で次へ書き出されます。

```text
%LOCALAPPDATA%\game-stack-planner\unity-my-assets.json
```

### 手動の購入済みRAG登録

Assetsの「商品候補を登録」で状態を「購入済み」にすると、購入済みRAGへ登録できます。AIは自分で入力した次の情報に加え、公式の公開商品説明・主要機能を検索に使います。

- AI用の別名
- メモ
- 選択した機能カテゴリ

価格・集計評価・互換性表は構造化された選別根拠として保持しますが、画像、レビュー本文、
ダウンロードしたアセットの中身はRAGへ渡しません。公開情報を取得できない廃止・非公開商品は、
ユーザー入力とUnity Editor同期由来の商品名・タグだけで検索します。

手動登録時には「私はこの商品を購入済みです（自己申告）」の確認が必須です。これはUnityによる購入確認ではありません。Unity Editor同期の証拠種別は `unity_editor_my_assets`、手動登録は `user_asserted` として区別します。また、ローカルキャッシュに `.unitypackage` が存在するだけでは所有済みRAGへ登録されません。

構成結果の「外部補完」から「候補を追加」を選ぶと、その検索の
機能カテゴリ（ネットワーク、セーブ、XRなど）を引き継いで保存します。保存した商品は
次回以降の構成案で、所有済み・検討中の候補として再利用されます。同一商品は
Asset Storeの商品IDで識別し、レビュー画面や追跡パラメータ付きURLも正規の商品URLへ
まとめるため、重複登録されません。

検索結果や手動保存商品は、既定では次へ蓄積されます。

```text
%LOCALAPPDATA%\game-stack-planner\catalog.sqlite3
```

`--db` を使うと別のデータベースを指定できます。

## CLI

```powershell
# Unityプロジェクトを診断
game-stack scan C:\Projects\MyGame

# オンライン検索を含む構成案
game-stack recommend --prompt "4人協力型ローグライト" --project C:\Projects\MyGame --platform pc

# 保存済みデータだけで構成案
game-stack recommend --prompt "Quest向けVRパズル" --platform quest --offline

# カタログ検索
game-stack catalog --source openupm --query networking

# 購入済みRAGを検索
game-stack --json rag-search --query "装備 重量" --limit 10

# UI/MCP起動時にモデル取得と差分索引を自動実行。進捗確認と明示修復
game-stack --json rag-status
game-stack --json rag-index

# Unity Editorが作成した標準同期ファイルを取り込む
game-stack sync-my-assets

# Asset Store商品を手動保存
game-stack pin "https://assetstore.unity.com/packages/..." --title "商品名" --ownership owned

# 公式公開メタデータの進捗確認・差分更新
game-stack --json asset-details-status
game-stack --json asset-details-sync --limit 20

# ローカルunitypackageを展開せず検査
game-stack --json scan-cache --inspect

# 所有アセットをUnityプロジェクトへ照合（コンパイルは明示指定時だけ）
game-stack --json validate-asset asset_store:12345 --project C:\Projects\MyGame
game-stack --json validate-asset asset_store:12345 --project C:\Projects\MyGame --compile
```

各コマンドに `--json` を付けると機械可読JSONで出力します。

通常はUIまたはMCP起動時に、商品名・タグ・別名・メモ・カテゴリと公式公開商品説明を
多言語Embeddingへ自動変換し、正規化済みベクトルをローカルSQLiteへ保存します。
検索時は質問も同じ固定世代でEmbedding化してコサイン類似度順に返します。
現行世代が全件揃うまでは所有確認済み文書だけを`lexical_fallback`として返し、
Dense類似度とは明確に区別します。部分ベクトルは検索へ混ぜません。`rag-index`は明示的な修復用です。
ベクトル、認証情報、ローカルパス、画像、レビュー本文、アセット実データはMCP応答へ含めません。

## UIとMCPの使い分け

通常のCodex利用ではローカルUIを開く必要はありません。MCPから状態確認、所有アセット検索、
構成案の根拠取得、商品詳細更新、プロジェクト互換性検査、承認付き導入まで実行できます。
UIは大量の候補を目視比較したい場合、同期を手動修復したい場合、導入差分を画面で確認したい
場合の補助作業台です。Asset Storeサイトは購入、ライセンス原文・レビュー本文の確認、Unity公式
導入フローに必要です。

## 推奨結果の見方

- 手持ち・導入済み優先: 既存プロジェクトをなるべく崩さない構成。
- OSS・無料優先: OpenUPMとライセンスを確認できるGitHub候補を重視した構成。
- 保守性・低リスク: 更新時期、ライセンス、互換性警告の少なさを重視した構成。

スコアは採用判断そのものではありません。Unityバージョン、Render Pipeline、対象プラットフォーム、ライセンス、最終更新を確認するための比較材料です。
