# Game Stack Planner

Game Stack Planner（画面名: Stackforge）は、作りたいゲームの説明とローカルのUnityプロジェクトから、必要機能と実装候補を整理する独立ツールです。カタログはローカルのSQLiteデータベースへ保存します。

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
4. 「構成案をつくる」を押す。
5. 機能ブループリント、3種類の推奨プラン、候補ごとの根拠と注意点を比較する。

## データソース

- Local: `Packages/manifest.json`、`packages-lock.json`、Unityのプロジェクト設定から導入済み構成を読み取ります。
- GitHub: 公式REST APIで公開リポジトリを検索します。環境変数 `GITHUB_TOKEN` がある場合は認証付きで利用します。
- OpenUPM: 公式パッケージレジストリの検索APIを利用します。
- Unity Asset Store（未所持候補）: Webページの自動取得・スクレイピングは行わず、公式検索リンクを生成します。候補はURL、名前、メモを保存できます。
- Unity My Assets（所有一覧）: 付属のUnity Editorブリッジが、ログイン中のPackage Managerサービスから所有商品をページング取得します。

### 所有アセットの自動同期

Unity Package Managerの「Add package from disk」で
`unity_package/com.taiyousun.stackforge/package.json` を選びます。Unity Hub / Editorへ
ログインした状態で `Tools > Stackforge > My Assets Sync` を開き「My Assetsを同期」を
押してください。その後StackforgeのCatalogで「所有アセットを同期」を押すと、全件が
ローカルSQLiteとRAGへ登録されます。非表示にした商品も別ページとして取得し、商品IDで
重複を除きます。

自動同期でAI検索に使うのは、UnityのMy Assets応答に含まれる商品名とタグだけです。
商品ID、購入日時、非表示状態、同期時刻は所有状態の管理用メタデータとして保持します。
Unity認証トークン、Asset Store本文・画像・価格・レビュー、ダウンロードしたアセットの
中身は出力・登録しません。

同期ファイルは既定で次へ書き出されます。

```text
%LOCALAPPDATA%\game-stack-planner\unity-my-assets.json
```

### 手動の購入済みRAG登録

Catalogの「Asset Store商品を保存」で状態を「購入済み」にすると、購入済みRAGへ登録できます。AIが検索に使えるのは、自分で入力した次の情報だけです。

- AI用の別名
- メモ
- 選択した機能カテゴリ

手動登録ではAsset Storeの商品名・ページ本文・画像・価格・レビューや、ダウンロードしたアセットの中身はRAGへ渡しません。公式URLと商品名は通常のローカルカタログには保持されますが、AI向け検索結果には含まれません。Unity Editor同期由来のレコードだけは、上記の商品名・タグを検索語として使います。

手動登録時には「私はこの商品を購入済みです（自己申告）」の確認が必須です。これはUnityによる購入確認ではありません。Unity Editor同期の証拠種別は `unity_editor_my_assets`、手動登録は `user_asserted` として区別します。また、ローカルキャッシュに `.unitypackage` が存在するだけでは所有済みRAGへ登録されません。

構成案の「Asset Storeで補完」から「見つけた商品を保存」を選ぶと、その検索の
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

# Unity Editorが作成した標準同期ファイルを取り込む
game-stack sync-my-assets

# Asset Store商品を手動保存
game-stack pin "https://assetstore.unity.com/packages/..." --title "商品名" --ownership owned
```

各コマンドに `--json` を付けると機械可読JSONで出力します。

## 推奨結果の見方

- 手持ち・導入済み優先: 既存プロジェクトをなるべく崩さない構成。
- OSS・無料優先: OpenUPMとライセンスを確認できるGitHub候補を重視した構成。
- 保守性・低リスク: 更新時期、ライセンス、互換性警告の少なさを重視した構成。

スコアは採用判断そのものではありません。Unityバージョン、Render Pipeline、対象プラットフォーム、ライセンス、最終更新を確認するための比較材料です。
