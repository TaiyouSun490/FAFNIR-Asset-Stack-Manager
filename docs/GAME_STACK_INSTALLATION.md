# Stackforge AI導入フロー

Stackforgeは、AIが自由なshell commandやURLを生成して実行する方式ではありません。
AIが選べる入力は、ローカルカタログに保存された `candidate_id` と対象Unity
projectだけです。決定的なinstallerが候補を再取得し、source別の閉じたactionへ
変換します。

## 現在の自動適用範囲

| source | 計画 | 現在の動作 |
| --- | --- | --- |
| OpenUPM | `upm_registry_add` | 許容ライセンスと完全SemVerがある候補だけ、固定registryと完全package-name scopeをmanifestへ追加 |
| GitHub | `inspect_repository` | 検査待ち。検索結果のbranchやREADMEだけでは導入しない |
| Asset Store | `manual_asset_store` | 商品ページ、購入、Add to My Assets、Download、Importは公式UIで人が行う |
| Asset Store cache | `manual_unitypackage` | キャッシュ候補を表示するが、現在は自動importしない |
| local | `noop` | 対象projectに同じpackageがある場合だけ導入済み |

OpenUPM適用が変更するのは `Packages/manifest.json` の `dependencies` と
`scopedRegistries`だけです。`packages-lock.json` と `Library` は直接編集しません。
Unityを次に開いたとき、Package Managerがnetworkから解決します。

## 承認付きCLI

```powershell
# 1. read-onlyの計画とmanifest差分を作る
game-stack install-plan openupm:com.example.package --project C:\Projects\MyGame

# 2. 出力されたplan idと一回限りのnonceで、確認済み計画だけを適用する
game-stack install-apply <plan-id> --approval-nonce <approval-nonce>

# 3. Unityで解決後、jobを確認する
game-stack install-status <job-id>

# 4. 必要なら、出力された一回限りのrollback nonceでmanifest原文を復元する
game-stack install-rollback <job-id> --rollback-nonce <rollback-nonce>
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

## 次の段階

GitHub候補は、GitHub APIで候補repository内の `package.json`、package root、license、
完全40桁commit SHAを検査できた場合だけ、UnityのGit dependency候補へ昇格させます。
branch、tag、短縮SHA、任意Git URL、README内commandは実行しません。

ローカル `.unitypackage` の自動importは、対象Unity versionのEditorを使ったstaging
projectでのpreview、`Assets` / `Packages` / `ProjectSettings` のsnapshot、compile/log
確認、本番適用前の再承認を実装してから有効化します。

参考:

- [Unity Git dependencies](https://docs.unity3d.com/6000.0/Documentation/Manual/upm-git.html)
- [Unity Package Manager Client.Add](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/PackageManager.Client.Add.html)
- [Unity command-line `-importPackage`](https://docs.unity3d.com/6000.0/Documentation/Manual/EditorCommandLineArguments.html)
- [Unity Asset Store Terms](https://unity.com/legal/as-terms)
