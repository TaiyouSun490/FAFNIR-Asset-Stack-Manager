# セールで積んだUnityアセット1,584件を、ローカルRAG＋MCPでAIに選ばせる

想定タグ: `Unity` `Python` `MCP` `RAG` `生成AI`

<!-- Qiita投稿時、ここにFafnirのヘッダー画像を挿入 -->

Unity Asset Storeのセールで買ったものの、存在を忘れているアセットはないでしょうか。

自分のライブラリにも、背景、効果音、Editor拡張、シェーダー、ゲームシステムなどが大量にありました。ところが新しいゲームを考えるたびにAsset Storeを検索し直し、似たものをまた買いそうになります。

そこで、所有アセットをローカルへ同期し、作りたいゲームを文章で伝えるとCodexやClaude Codeが実装候補を組み立てられるツール **Fafnir Asset Stack Manager** を作りました。

- GitHub: [TaiyouSun490/FAFNIR-Asset-Stack-Manager](https://github.com/TaiyouSun490/FAFNIR-Asset-Stack-Manager)
- ライセンス: MIT
- Python: 3.12以上
- 現在はAlpha版

例えば、次のような依頼を想定しています。

> 所有アセットを優先して、吹雪で孤立した山岳観測所を舞台にした一人称ホラー探索ゲームを作りたい。停電した施設、無線機を使う周波数パズル、雪上に残る足跡、視界を遮る吹雪、徘徊する怪異、体温管理、持ち物管理、チェックポイント保存が必要。各アセットの用途とUnity 6での互換性を説明し、不足機能だけGitHub・OpenUPM・Asset Store候補から補って。

Fafnirはこの文章をそのままゲームの要件として扱い、次の3レーンを混ぜずに取得します。

1. 所有確認済みのUnity Asset Storeアセットと導入済みpackage
2. GitHub・OpenUPMの公開候補
3. 購入未確認のAsset Store候補

最終的な構成判断はFafnir内の固定ルールではなく、MCPで接続したCodexやClaude Codeが行います。

## 作った理由

欲しかったのは、名前の似た商品を返す検索画面ではありませんでした。

- このゲームのどの機能に使えるのか
- 手持ちのアセット同士をどう組み合わせるのか
- Unity 6、Render Pipeline、Input Systemなどの条件に合うのか
- 手持ちで足りない部分は何か
- OSSを追加するならライセンスや更新状態はどうか

この判断まで含めて初めて、積んでいたアセットを実装へ戻せます。

一方で、LLMへ所有一覧1,584件を毎回すべて渡すのは現実的ではありません。そこで「検索する部分」と「設計を判断する部分」を分けました。

```text
Unity My Assets / 公開商品情報 / Unity Project
                       ↓
        FafnirのローカルSQLite＋ベクトル索引
                       ↓
       関連する候補と根拠だけをMCPで返す
                       ↓
             Codex / Claude Code
                       ↓
      用途、組み合わせ、不足機能、注意点を説明
```

## RAGは誰のDBを検索しているのか

ここは実装中に自分でも混乱した点です。

FafnirのRAG用DBを構築するのは、OpenAIやAnthropicのサービスではありません。Fafnirを実行しているユーザーのPC上で、Fafnir自身がSQLite DBとベクトル索引を作ります。

Windowsでの既定保存先は次のとおりです。

```text
%LOCALAPPDATA%\game-stack-planner\catalog.sqlite3
```

Codex用DBとClaude用DBが別々に作られるわけでもありません。同じOSユーザー、同じ設定なら、Web UI、CLI、Codex、Claude Codeは同じローカルカタログを使います。

MCP応答へDB全体を渡すことはありません。検索に一致した商品の名前、タグ、説明の抜粋、互換性情報、スコアなど、判断に必要な候補だけを返します。ベクトル、Unityの認証情報、ローカルパス、画像、レビュー本文、`.unitypackage`の中身は返しません。

なお、MCPの検索結果は接続先AIサービスへの入力に含まれる場合があります。外部AIへ一切送信したくない場合は、MCPへ接続せずCLIのオフライン検索を使う必要があります。

## 所有アセットの同期

付属のUnity packageをPackage Managerの **Add package from disk** で追加し、Unity Editorの **Tools > Fafnir > My Assets Sync** を開きます。

```text
unity_package/com.taiyousun.stackforge/package.json
```

Editorブリッジは、ログイン中のUnity Editorが持つMy Assets一覧をページング取得します。表示中の商品だけでなく、非表示にした商品も別ページから取得し、商品IDで重複を除きます。

同期ファイルに含めるのは、商品ID、表示名、Asset Storeタグ、購入・付与時刻、非表示状態、Unity版、出力時刻です。OAuthトークンやCookieは出力しません。

既定ではUnity起動中に6時間ごとに同期します。新しく購入した直後は **My Assetsを同期** を押せばよく、Fafnir側で改めて手動同期する必要はありません。Fafnirが停止していた場合も、次回起動時に変更を取り込みます。

ただし、このブリッジはUnity Editor内部のPackage Managerサービスに対するadapterです。Unityの公開リファレンスソースを参考にしていますが、内部APIであるため将来のUnity更新で追従が必要になる可能性があります。

## ベクトル索引を「あること」にしない

RAGを最初に試したとき、検索精度以前にベクトル索引が作られていない状態がありました。UIだけは動き、検索も何らかの結果を返すため、利用者から見るとDense検索が機能しているのか判別できません。

現在は次のライフサイクルにしています。

- UIまたはMCP起動時に、同期ファイルの差分を自動で取り込む
- 固定revisionの`intfloat/multilingual-e5-small`を初回だけ取得する
- 独立したバックグラウンドprocessで索引を作る
- MCPを短時間で終了しても索引処理を継続する
- 回線が切れた場合は状態を保存し、次回起動時に再開する
- 現行世代が100%揃うまで部分的なDense索引を検索に使わない
- 準備中は、所有確認済み文書だけの`lexical_fallback`と明示する
- 完成後は`hybrid_dense`と検索方式を明示する

モデルIDだけでなく、固定revision、前処理方式、最大token数から索引世代を分離しています。別モデルで作ったベクトルや、古い本文から作ったベクトルを現在の検索結果へ混ぜないためです。

状態はCLIから確認できます。

```powershell
fafnir --json rag-status
```

手元のカタログでは、所有アセット文書1,584件に対して1,584件の現行ベクトルが作られ、coverage 1.0になりました。公開商品ページから詳細を取得できたものは750件です。残り834件は公開sitemapに商品ページが見つからないため、My Assets由来の名前とタグなど、実際に得られた情報だけで索引しています。説明を推測して補うことはしません。

## MCPには検索をさせ、LLMには判断をさせる

Fafnir自体にOpenAI API keyは必要ありません。ローカルのstdio MCP serverとして起動します。

Codexへの登録例です。

```powershell
codex mcp add fafnir -- fafnir mcp
codex mcp get fafnir
```

Claude Codeの場合は次のように登録できます。

```powershell
claude mcp add --transport stdio --scope user fafnir -- fafnir mcp
claude mcp get fafnir
```

主なMCP toolは次のとおりです。

- `fafnir_status`: DB、同期、RAG、商品詳細取得の状態
- `search_owned_asset_rag`: 所有アセットの意味検索
- `retrieve_game_stack_evidence`: ゲーム要件に対する3レーンの比較材料
- `get_unity_asset_candidate`: 単一候補の詳細な根拠
- `validate_cached_asset_for_project`: キャッシュ済みアセットとProjectの互換性検査
- `prepare_candidate_install`: 導入前の計画と差分

重要なのは、Fafnirが検索スコアだけで採用品を決めないことです。

検索は候補を狭める用途には向いています。しかし「浸水した通路にこの環境アセットを使い、この音響assetで材質別足音を作り、セーブだけはOSSで補う」といった実装設計には、複数候補と制約を読む推論が必要です。そこでFafnirは根拠を返すところまでに留め、LLM clientへ判断を委ねます。

## 先に作ったBOOTH向け検索がうまくいかなかった理由

この設計になる前に、BOOTHのアバター商品から3種類のコーディネートを自動生成する別ツールも作っていました。

こちらは、映画館の内装やソフトドリンクの小物を「衣装」として選ぶことがありました。ベクトル索引がないことだけが原因ではありません。商品説明に「Unity」「テクスチャ」「オリジナル3Dモデル」などの共通語が多く、背景、小物、衣装、アバターの分類を誤ったあと、固定ロジックがその候補を衣装枠へ押し込んでいたためです。

さらに、BOOTHのアバター組み合わせでは次の判断が必要です。

- 商品がアバター本体、衣装、髪、texture、小物のどれか
- 特定素体へ装着できるか
- 見た目のstyle、色、speciesが合うか
- サムネイルに複数商品が写っている場合、販売対象はどれか

これはAsset Store商品を「足音」「セーブ」「水表現」などの実装用途へ割り当てる問題より難しく、構造化されていない情報から互換性と外見まで確定しなければなりません。

失敗から得た教訓は単純でした。

> RAGは判断器ではなく、判断材料を絞るための取得層として使う。

カテゴリが不明なら不明のまま返す。互換性情報がなければ使用可能と断定しない。画像EmbeddingやVLMも候補抽出には使えても、商品種別や装着互換性の最終的な証拠にはしない。Fafnirではこの境界を意識しています。

## インストールと起動

```powershell
git clone https://github.com/TaiyouSun490/FAFNIR-Asset-Stack-Manager.git
cd FAFNIR-Asset-Stack-Manager
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
fafnir ui
```

Web UIは既定で`http://127.0.0.1:8770/`を開き、loopbackだけで待ち受けます。ただしCodexやClaude CodeからMCP経由で使用するなら、通常はWeb UIを開く必要はありません。

CLIだけでも検索できます。

```powershell
fafnir recommend `
  --prompt "閉鎖された海底研究施設の一人称ホラー脱出ゲーム" `
  --project C:\Projects\MyGame `
  --platform pc `
  --budget owned_first

fafnir --json rag-search --query "浸水した通路の材質別足音" --limit 10
```

## 現時点の限界

FafnirはまだAlpha版です。

- 検索スコアは互換性や安全性の保証ではない
- Asset Storeのライセンス原文、レビュー本文、購入操作は公式サイトで確認する
- Asset Store商品のimportはUnityの公式フローで行う
- GitHub候補はライセンス、commit、依存関係を確認する
- My Assets同期adapterは将来のUnity変更で修正が必要になる可能性がある
- 実カタログに対するRecall@5、MRR、無関係queryの棄却率は、今後さらに評価データを増やす必要がある

「検索結果が返った」ことを完成条件にせず、該当アセットがない質問で空結果にできるか、日本語の用途説明から英語の商品名を探せるか、セーブ機能を求めたときに環境モデルが上位を占めないかを継続して検証しています。

## まとめ

作ってみて、RAGそのものは特別な推論機構ではないと実感しました。前処理で文書をEmbeddingへ変換しても、やっていることは関連する情報の取得です。入力データの所有根拠、索引の世代管理、検索できない状態の表示、そして検索後に誰が判断するかを設計しなければ、ベクトルDBがあっても良い結果にはなりません。

Fafnirでは、所有アセットと索引をローカルに置き、関連する根拠だけをMCPで渡し、最終的な実装構成はCodexやClaude Codeに考えさせる形にしました。

積んだアセットをもう一度使える候補へ戻したい方は、試してもらえると嬉しいです。Issueや改善案も歓迎します。

- [FAFNIR Asset Stack Manager](https://github.com/TaiyouSun490/FAFNIR-Asset-Stack-Manager)
