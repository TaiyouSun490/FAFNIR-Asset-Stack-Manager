# Fafnir AI導入フロー

> 対象: OpenUPM導入、差分確認、approval、rollbackの安全条件を確認する上級利用者・開発者。
> Fafnirの基本操作は[利用ガイド](USER_GUIDE.md)を参照してください。

Fafnirは、AIが自由なshell commandやURLを生成して実行する方式ではありません。
AIが選べる入力は、ローカルカタログに保存された `candidate_id` と対象Unity
projectだけです。決定的なinstallerが候補を再取得し、source別の閉じたactionへ
変換します。

## 現在の自動適用範囲

| source | 計画 | 現在の動作 |
| --- | --- | --- |
| OpenUPM | `upm_registry_add` | 許容ライセンスと完全SemVerがある候補だけ、固定registryと完全package-name scopeをmanifestへ追加 |
| GitHub | `inspect_repository` | 検査待ち。検索結果のbranchやREADMEだけでは導入しない |
| Asset Store | 所有確認済み商品のダウンロード計画 | MCP経由で起動中のUnity Bridgeへ取得を依頼。購入・Add to My Assetsは公式UI、本番Importは別手順 |
| Asset Store cache | Unity標準のImport確認画面 | 所有商品にリンクしたキャッシュを静的検査し、指定した接続中プロジェクトでファイル選択画面を開く。Import/CancelはUnityで選択 |
| local | `noop` | 対象projectに同じpackageがある場合だけ導入済み |

OpenUPM適用が変更するのは `Packages/manifest.json` の `dependencies` と
`scopedRegistries`だけです。`packages-lock.json` と `Library` は直接編集しません。
Unityを次に開いたとき、Package Managerがnetworkから解決します。

## 承認付きCLI

```powershell
# 1. read-onlyの計画とmanifest差分を作る
fafnir install-plan openupm:com.example.package --project C:\Projects\MyGame

# 2. 出力されたplan idと一回限りのnonceで、確認済み計画だけを適用する
fafnir install-apply <plan-id> --approval-nonce <approval-nonce>

# 3. Unityで解決後、jobを確認する
fafnir install-status <job-id>

# 4. 必要なら、出力された一回限りのrollback nonceでmanifest原文を復元する
fafnir install-rollback <job-id> --rollback-nonce <rollback-nonce>
```

GUIでは候補カードの「導入内容を確認」から同じフローを使います。HTTPのinstall
POSTは、`Origin`が現在のloopback `Host` とhostname・portまで一致する場合だけ
受け付けます。Chrome拡張やOrigin無しのrequestから導入を実行することはできません。

## 安全条件

- 計画は1 candidate × 1 project。複数候補の一括適用はしません。
- OpenUPM URLは `https://package.openupm.com` 固定です。
- versionは `1.2.3` のような完全SemVerだけです。`latest`、range、tagは拒否します。
- 既存packageの版や別scoped registryと競合した場合は上書きせず停止します。
- 計画後にmanifestのSHA-256が変わった場合は適用を拒否します。
- approval nonceは計画内容、project、manifest preimageへ結合した短命・一回限りです。
- 書き込みはmanifestと同じdirectoryの一時fileからatomic replaceします。
- rollbackは現在のmanifestが適用直後のhashと一致する場合だけ実行します。

rollbackが保証するのは `Packages/manifest.json` の復元までです。Unityでpackageの
Editor codeが実行された後に `Assets` や `ProjectSettings` へ生じた副作用は戻せません。

## ローカルAsset Storeアセットの検証

ダウンロードには導入先プロジェクトの起動は不要です。Fafnir Bridgeを含むUnity Editorが
起動済みならそれを使い、Unityの共通キャッシュへ保存します。接続済みなら毎回My Assetsを
開いたりログインし直す必要はありません。Bridge未接続ならBridge入りプロジェクトを1つ開き、
認証エラーが返ったときだけHub/Editorのログイン状態を復旧します。Unityの自動起動や
専用常駐ダウンローダーは現時点では未実装です。

MCPのbridge情報は`readiness`（`offline` / `sign_in_required` / `busy` / `connected`）と
`next_action`を返します。旧Bridgeの認証状態は`unknown`として扱います。Editorのログイン状態と
Asset Storeへのリクエスト成功は別なので、接続済みでも取得時の認証エラーはあり得ます。
`starting`は転送開始待ちです。今回確認されたUnity Connect認証エラーは
`unity_authentication_required`として返し、単なるタイムアウトと区別します。

`scan-cache --inspect`は`.unitypackage`を展開・実行せず、内容種別、asmdef、UPM依存、
Render Pipeline/Input参照、ネイティブプラグインを検査します。所有商品と一意に対応した
キャッシュは、対象Unityプロジェクトの版・pipeline・input・platform・既存GUIDと照合できます。

```powershell
fafnir --json validate-asset asset_store:12345 --project C:\Projects\MyGame
fafnir --json validate-asset asset_store:12345 --project C:\Projects\MyGame --compile
```

`--compile`は明示指定時だけ、対象と同じUnity Editorで一時プロジェクトを作り、対象projectの
`Packages/manifest.json`と`.unitypackage`を入れてcompiler errorを確認します。本番projectは
変更しません。空のstaging環境なので、シーン描画、操作性、ランタイム挙動は保証しません。

## 次の段階

GitHub候補は、GitHub APIで候補repository内の `package.json`、package root、license、
完全40桁commit SHAを検査できた場合だけ、UnityのGit dependency候補へ昇格させます。
branch、tag、短縮SHA、任意Git URL、README内commandは実行しません。

ローカル `.unitypackage` の非展開検査とstaging compileは実装済みです。本番projectへの
自動importは、変更対象preview、`Assets` / `Packages` / `ProjectSettings` のsnapshot、
復元検証、本番適用前の再承認を実装するまで有効化しません。

参考:

- [Unity Git dependencies](https://docs.unity3d.com/6000.0/Documentation/Manual/upm-git.html)
- [Unity Package Manager Client.Add](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/PackageManager.Client.Add.html)
- [Unity command-line `-importPackage`](https://docs.unity3d.com/6000.0/Documentation/Manual/EditorCommandLineArguments.html)
- [Unity Asset Store Terms](https://unity.com/legal/as-terms)
