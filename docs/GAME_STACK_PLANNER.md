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
- Unity Asset Store: 自動取得・スクレイピングは行わず、公式検索リンクを生成します。候補や購入済み商品はURL、名前、メモを手動保存できます。

### 購入済みアセットのRAG登録

Catalogの「Asset Store商品を保存」で状態を「購入済み」にすると、購入済みRAGへ登録できます。AIが検索に使えるのは、自分で入力した次の情報だけです。

- AI用の別名
- メモ
- 選択した機能カテゴリ

Asset Storeの商品名・ページ本文・画像・価格・レビューや、ダウンロードしたアセットの中身はRAGへ渡しません。公式URLと商品名は通常のローカルカタログには保持されますが、AI向け検索結果には含まれません。

登録時には「私はこの商品を購入済みです（自己申告）」の確認が必須です。これはUnityによる購入検証ではありません。また、ローカルキャッシュに `.unitypackage` が存在するだけでは購入済みRAGへ登録されません。登録済み件数はCatalog左側の「購入済みRAG」で確認でき、各商品には同名のバッジが表示されます。

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

# Asset Store商品を手動保存
game-stack pin "https://assetstore.unity.com/packages/..." --title "商品名" --ownership owned
```

各コマンドに `--json` を付けると機械可読JSONで出力します。

## 推奨結果の見方

- 手持ち・導入済み優先: 既存プロジェクトをなるべく崩さない構成。
- OSS・無料優先: OpenUPMとライセンスを確認できるGitHub候補を重視した構成。
- 保守性・低リスク: 更新時期、ライセンス、互換性警告の少なさを重視した構成。

スコアは採用判断そのものではありません。Unityバージョン、Render Pipeline、対象プラットフォーム、ライセンス、最終更新を確認するための比較材料です。
