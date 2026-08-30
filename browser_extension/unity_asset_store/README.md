# Stackforge Asset Store Capture & RAG

Unity Asset Storeの商品ページを、購入状態未確認の候補として保存するか、購入済み（自己申告）としてローカルのStackforge RAGへ1件ずつ登録するChrome拡張です。BOOTH用拡張とは別の拡張・別のNative Messaging hostです。

## 安全境界

- ユーザーが拡張のボタンを押した現在タブだけを対象にします。
- Native Hostへ送信するのは公式商品URL、タブタイトル、ユーザーが入力した呼び名・メモ・用途と、明示的な購入自己申告だけです。
- RAG/AI検索へ見せる追加情報は、ユーザー自身が入力した呼び名・メモ・用途だけです。ページ本文や商品アセットをAI入力にはしません。
- DOM、本文、アセット本体、画像、価格、Cookie、検索一覧は取得しません。
- `scripting`、host permissions、外部fetchは使いません。
- 候補保存の状態は「購入未確認候補」のままです。
- 購入済みRAG登録は、商品ページ上で確認チェックを入れてボタンを押した場合だけ実行します。これは購入証明を検証した結果ではなく、ユーザーの自己申告です。
- 購入済みRAG登録には、用途・自分用の呼び名・メモのいずれか1つ以上が必須です。
- RAG登録しても、Unity Package Managerやローカルキャッシュに当該アセットが存在するとは限りません。ローカルでの利用可否は別途確認します。

## インストール

1. `pip install -e .` などで `game-stack-native-host.exe` を利用可能にします。
2. Chromeの `chrome://extensions` でデベロッパーモードを有効にし、「パッケージ化されていない拡張機能を読み込む」からこのフォルダーを選びます。
3. 表示された拡張IDを指定して、PowerShellで次を実行します。

```powershell
.\docs\install_game_stack_native_messaging_host.ps1 -ExtensionId <32文字の拡張ID>
```

4. Unity Asset Storeの商品ページを開き、拡張から用途を選んで候補保存します。
5. 購入済みRAGへ登録する場合は、必要に応じて自分用の呼び名を入力し、購入済みの自己申告チェックを入れて専用ボタンを押します。

ローカルDBは既定で `%LOCALAPPDATA%\game-stack-planner\catalog.sqlite3` を使用します。
