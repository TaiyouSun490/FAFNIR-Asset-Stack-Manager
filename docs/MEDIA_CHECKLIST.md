# Fafnir 画面素材チェックリスト

README、Qiita、SNSでFafnirの動作を説明するために必要な画面素材を、優先順にまとめます。
画像が揃うまではREADMEへ壊れたimage linkや仮画像を置きません。

保存先は`docs/images/`、file名は次の表の名前を推奨します。

## 最優先

| 優先 | File | 撮る画面 | 伝えること |
| --- | --- | --- | --- |
| A | `compose-input.png` | STACK画面へ具体的なゲーム要件を入力した状態 | 自然文から始められる |
| A | `compose-result.png` | 生成後の要件、選定候補、不足要件が同時に分かる範囲 | 単なる商品検索ではなく実装構成を返す |
| A | `assets-owned.png` | ASSETS画面を所有／ローカルで絞り、RAG状態が見える状態 | 所有一覧と索引をローカル管理する |
| A | `unity-my-assets-sync.png` | Unity EditorのTools > Fafnir > My Assets Sync | 所有一覧の取得経路が明確である |
| A | `mcp-result.png` | CodexまたはClaude Codeが、Fafnirの候補を用途・互換性・不足へ整理した回答 | UIを開かずMCPから使える |

READMEへ最初に追加するなら、`compose-result.png`と`mcp-result.png`の2枚で十分です。
同じ内容を重複して見せる画像は増やしません。

## あると理解が早い素材

| 優先 | File | 撮る画面 | 用途 |
| --- | --- | --- | --- |
| B | `rag-ready.png` | 文書数、vector数、coverage、`hybrid_dense`が読める索引状態 | 「索引が本当に完成しているか」を示す |
| B | `project-compatibility.png` | Unity Project解析後のVersion、Pipeline、Input、警告 | 互換性確認が商品名検索とは別にあることを示す |
| B | `install-plan.png` | `manifest.json`差分、固定version、risk、承認button | AIが勝手に導入しないことを示す |
| B | `asset-detail.png` | 1商品の用途、公開互換性、所有証拠、注意点 | 推薦の根拠が確認できることを示す |
| C | `asset-store-capture.png` | Chrome拡張で現在の商品を候補登録する画面 | 拡張機能は補助経路であることを示す |
| C | `cache-inspection.png` | `.unitypackage`の非展開検査結果 | script、asmdef、DLL、依存を事前確認できることを示す |

## 動画・GIF

15～25秒の`quick-demo.gif`またはMP4を1本用意すると、静止画より利用順が伝わります。

推奨sequence:

1. STACK画面へゲーム要件を貼る。
2. Unity Projectのpathを指定するか、未指定のまま構成を生成する。
3. 所有候補が先に表示される。
4. 不足機能だけ外部候補へ分かれていることを見せる。
5. 候補の用途・互換性・注意点を開く。

待機時間は編集で短縮して構いません。ただし、実際には非同期処理である索引生成や公開情報取得を、
即時完了したように見せる編集は避けます。

## 撮影に使う共通シナリオ

README、Qiita、動画で別々のゲーム例を使うと結果の比較が難しくなります。次のpromptへ統一します。

```text
所有アセットを優先して、吹雪で孤立した山岳観測所を舞台にした一人称ホラー探索ゲームを作りたい。
停電した施設、無線機を使う周波数パズル、雪上に残る足跡、視界を遮る吹雪、徘徊する怪異、
体温管理、持ち物管理、チェックポイント保存が必要。
各アセットの用途とUnity 6での互換性を説明し、不足機能だけ外部候補から補って。
```

## 撮影条件

- PNGは原寸または2倍解像度、文字が読める大きさで撮る。
- README用は横幅1440px前後、SNS用は別途1200×630pxへ再構成する。
- Browser zoomは100%に揃える。
- mouse cursor、tooltip、toastが説明対象を隠していないことを確認する。
- user名、絶対path、認証情報、個人的なProject名を写さない。
- 所有数を公開したくない場合はstatsをcropする。数値を画像編集で改ざんしない。
- RAG画面は`hybrid_dense`または`lexical_fallback`を隠さず写す。
- 商品の価格、rating、互換性は撮影時点の情報であることが分かるようにする。
- AI回答を撮る場合は、Fafnir toolを使ったことと、根拠不足を断定していないことを確認する。

## 製品ロゴとOGP

- README header: `game_stack_planner/static/fafnir-header-logo.png`
- App用compact logo: 必要になった時点で同じdesignから別途切り出す
- GitHub／Qiita／X用OGP: 1200×630px、製品ロゴ、短い説明、UI結果の一部だけを配置する

OGPへ長い機能一覧や細かいUI textを入れません。推奨copy:

```text
OWNED ASSETS → IMPLEMENTATION STACK
Local-first Unity asset search for Codex and Claude
```
