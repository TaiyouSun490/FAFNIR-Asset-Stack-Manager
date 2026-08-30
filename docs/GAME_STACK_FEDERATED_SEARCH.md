# Stackforge federated search

Stackforgeの検索価値は、同じゲーム機能に対して次の3レーンを並べて比較できることです。

1. **手持ち・ローカル** — ユーザーが所有を明示したAsset Store商品、Unityのローカルキャッシュ、現在のUnityプロジェクトにあるパッケージ
2. **購入未確認候補** — 公式Asset Store検索からユーザーが保存した商品
3. **GitHub / OpenUPM** — 公開GitリポジトリとUPMパッケージ

「購入未確認」は「未購入」と同義ではありません。StackforgeはAsset Storeアカウントの全所有台帳を自動取得しないため、確認できない商品を未購入と断定しません。

## 所有・利用可能性の状態

- `confirmed_owned`: ユーザーが所有済みとして明示した商品。現段階の証拠表示は「自己申告」です。
- `locally_cached`: このPCの `Asset Store-5.x` に `.unitypackage` がある商品。ダウンロード済みですが、現在のアカウントの全My Assetsや購入証明とは限りません。
- `confirmed_owned`: Unity Editorブリッジがログイン中のMy Assets応答で確認した商品、またはユーザーが明示的に自己申告した商品。両者は `ownership_evidence.kind` で区別します。
- `project_present`: 指定Unityプロジェクトの依存関係で確認したパッケージ。
- `unknown`: 購入・所有状態を確認していない候補。

これらは検索scopeとは別の証拠状態です。所有・キャッシュ・導入済みは関連性が成立した候補だけに加点されます。

## ローカルAsset Storeキャッシュ

GUIのCatalogから「ローカルキャッシュを取込」を実行するか、CLIを使います。

```powershell
game-stack scan-cache
game-stack scan-cache --path "D:\UnityCache\Asset Store-5.x"
game-stack catalog --scope owned_assets --query inventory
```

標準では次を確認します。

- `ASSETSTORE_CACHE_PATH` で設定された場所
- `%APPDATA%\Unity\Asset Store-5.x`

走査対象は `.unitypackage` のファイル名・相対フォルダー・サイズ・更新時刻だけです。アーカイブを展開・実行・全文hashせず、シンボリックリンクやreparse pointも辿りません。絶対キャッシュパスはAPIレスポンスへ返しません。

## Asset Store専用Chrome拡張

拡張は [browser_extension/unity_asset_store](../browser_extension/unity_asset_store) にあります。BOOTH版とは別の拡張ID、別のNative Messaging host `jp.game_stack_planner` を使います。

この拡張は、ユーザーがボタンを押した現在の商品ページを1件だけ「購入未確認候補」として保存します。DOM、本文、画像、価格、Cookie、検索一覧を取得せず、`scripting` とhost permissionsも要求しません。

```powershell
.\docs\install_game_stack_native_messaging_host.ps1 -ExtensionId <拡張ID>
```

Native hostはChrome拡張originを1つだけ許可し、URL・message schema・機能キーを本体側でも再検証します。Stackforge HTTP serverのloopback Origin制限は緩和しません。

## Asset Storeアクセス方針

未所持候補のAsset Store検索は公式検索リンクを開き、ユーザーが選択したURLだけを保存します。Webページの自動巡回や検索結果スクレイピングはしません。所有一覧は別経路として、ログイン済みUnity EditorのPackage ManagerサービスをEditorブリッジがページングし、最小メタデータだけをローカルへ書き出します。GitHubとOpenUPMはそれぞれの公開検索APIを本体が利用します。

Unityの現行条件は変更される可能性があるため、運用時は [Unity Asset Store Terms](https://unity.com/legal/as-terms) を確認してください。
