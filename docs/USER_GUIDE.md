# Fafnir 利用ガイド

Fafnirは、Unityの所有アセットを起点に実装候補を組み立てるローカルの
**Asset Stack Manager**です。

## 最初に読むところ

初回は次の順番だけ実行すれば使い始められます。

1. [インストール](#2-インストール)
2. [Unity所有アセットを同期](#4-unity所有アセットを同期)
3. Codexなら[Codexから使う](#7-codexから使う)、Claude Codeなら[Claude-codeから使う](#8-claude-codeから使う)
4. [AIへの依頼例](#9-aiへの依頼例)をそのまま試す

入口は目的に応じて1つだけ選びます。

| 入口 | 使う場面 | 必須か |
| --- | --- | --- |
| MCP | Codex／Claude Codeに用途、組み合わせ、不足機能を判断させる | AI連携時に使用 |
| Web UI | 候補を一覧で見る、同期状態や導入差分を目視確認する | 任意 |
| CLI | offline検索、JSON出力、自動化、診断・修復 | 任意 |

Web UIを起動してからMCPを使う必要はありません。3つの入口は同じローカルDBを共有します。

改名前からのDB、`game_stack_planner`、`game-stack-planner`データフォルダ、
`com.taiyousun.stackforge` Unity package ID、`stackforge.*` schemaは互換性のため
変更しません。既存環境の再同期やDB移行は不要です。

同じPython環境に旧distributionが残る場合だけ、一度
`python -m pip uninstall -y stackforge-unity`を実行してから
`python -m pip install -e ".[dev]"`で入れ直します。
Web UI、CLI、MCPの3つの入口は同じローカルカタログを使用します。

## 1. データの所在

FafnirのRAGはクラウド上の共有DBではありません。Fafnirを実行したユーザーの
PCに、Fafnir自身がSQLite DBとベクトル索引を作成します。

| データ | 既定の保存先 | Git管理 | AIクライアントへ送る内容 |
| --- | --- | --- | --- |
| カタログ・RAG文書・ベクトル | `%LOCALAPPDATA%\game-stack-planner\catalog.sqlite3` | 対象外 | 検索に一致した候補の限定メタデータのみ |
| Unity My Assets出力 | `%LOCALAPPDATA%\game-stack-planner\unity-my-assets.json` | 対象外 | 所有候補として選ばれた限定メタデータのみ |
| 埋め込みモデル | `~/.cache/huggingface/hub` | 対象外 | 送信しない |
| 商品公開メタデータ | 上記SQLite内 | 対象外 | MCPツールが返した候補分のみ |

`LOCALAPPDATA`と`APPDATA`がない環境では、DBとMy Assets出力は
`~/.local/share/game-stack-planner/`へ保存されます。モデルキャッシュは
`HF_HUB_CACHE`または`HF_HOME`で変更できます。

DBはCodex用、Claude用に分かれているわけではありません。同じOSユーザーで既定設定を
使う場合、UI、CLI、Codex、Claude Codeは同じDBを共有します。クライアント別または
プロジェクト別に分離したい場合は、起動コマンドへグローバルオプション
`--db C:\path\to\catalog.sqlite3`を追加してください。

FafnirはDB全体、ベクトル、認証情報、画像、`.unitypackage`の内容、ローカルパスを
MCP応答へ含めません。ただし、AIへ質問したときは、MCPツールが返した商品名、タグ、
説明の抜粋、互換性、スコアなどの検索結果が、そのAIサービスへのモデル入力に含まれる
場合があります。完全なオフライン利用にはCLIの`--offline`を使用し、外部AIへMCP接続
しないでください。

## 2. インストール

Python 3.12以上を使用します。

```powershell
git clone https://github.com/TaiyouSun490/FAFNIR-Asset-Stack-Manager.git
cd FAFNIR-Asset-Stack-Manager
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

動作確認:

```powershell
fafnir --version
fafnir --json rag-status
```

## 3. 最初の起動

```powershell
fafnir ui
```

既定では`http://127.0.0.1:8770/`を開きます。外部ネットワークには公開せず、loopback
だけで待ち受けます。UIを使わない場合は起動不要です。

初回起動時の処理:

1. FafnirがローカルSQLite DBを作成する。
2. Unity My Assets出力があれば差分を取り込む。
3. 固定リビジョンの`intfloat/multilingual-e5-small`をローカルへ取得する。
4. 所有アセットの文書とベクトル索引を作成する。
5. 取得可能なAsset Store公開商品情報をバックグラウンド更新する。

回線切断時は処理状態をDBへ残し、次回のUIまたはMCP起動時に再開します。部分索引を
Dense検索として使用することはありません。完成までは`lexical_fallback`、完成後は
`hybrid_dense`と表示されます。

## 4. Unity所有アセットを同期

新規Projectへの導入・更新は、MCPの診断 → 導入計画 → 差分承認 → 適用、またはCLIの
bridge-doctor / bridge-plan / bridge-apply を利用できます。
対象Editorを閉じて適用し、起動後のコンパイルとMy Assets同期まで確認します。
[診断・導入・復元手順](UNITY_BRIDGE_SETUP.md)を参照してください。
以下はソースから手動導入する場合の手順です。

Unity Package Managerの **Add package from disk** で次を指定します。

```text
unity_package/com.taiyousun.stackforge/package.json
```

Unity Editorで **Tools > Fafnir > My Assets Sync** を開きます。

- 既定ではUnity起動中に6時間ごとに同期する。
- 新規購入直後は **My Assetsを同期** を押す。
- Fafnir起動中なら変更を検知して差分索引する。
- Fafnirを閉じていた場合は次回起動時に取り込む。

Unityの認証トークンはエクスポートしません。My Assets出力に含むのは商品ID、表示名、
タグ、購入・付与時刻、非表示状態、Unity版、出力時刻だけです。

## 5. UIの使い方

1. **ゲーム要件**へゲームの前提、必要機能、対象環境を入力する。
2. **対象環境**と**選定基準**を選ぶ。
3. 必要ならUnity Projectのパスを指定して**解析**する。
4. 所有アセット以外も調べる場合だけ**外部候補を含める**を有効にする。
5. **構成を生成**を押す。
6. 要件、選定候補、代替候補、不足要件を確認する。

Unity Projectを指定すると、Unity版、Render Pipeline、Input System、導入済みpackageを
選定前に照合します。キャッシュ済み`.unitypackage`は展開・実行せず静的検査でき、
明示操作した場合だけ一時Projectでコンパイルを検証します。本番Projectは変更しません。

## 6. CLIの使い方

```powershell
# プロジェクト解析
fafnir scan C:\Projects\MyGame

# 所有アセット優先の構成
fafnir recommend `
  --prompt "閉鎖された海底研究施設の一人称ホラー脱出ゲーム" `
  --project C:\Projects\MyGame `
  --platform pc `
  --budget owned_first

# 保存済みカタログだけを使う
fafnir recommend --prompt "鍵と暗証番号のパズル" --offline

# RAG状態と検索
fafnir --json rag-status
fafnir --json rag-search --query "浸水した通路の足音" --limit 10

# My Assetsとローカルキャッシュ
fafnir sync-my-assets
fafnir scan-cache --inspect
```

## 7. Codexから使う

Fafnirはローカルstdio MCP serverとして動作します。Fafnir自体にOpenAI API keyは
不要です。

インストール済みコマンドを使う場合:

```powershell
codex mcp add fafnir -- fafnir mcp
codex mcp get fafnir
```

source checkoutを直接使う場合:

```powershell
codex mcp add fafnir -- `
  C:\path\to\stackforge\.venv\Scripts\python.exe `
  -m game_stack_planner mcp
```

CodexのChatGPT desktop app、CLI、IDE extensionは同じCodex hostのMCP設定を共有します。
設定後にクライアントを再起動し、`/mcp`またはMCP設定画面で`fafnir`を確認します。

公式資料: [OpenAI Codex MCP](https://developers.openai.com/codex/mcp)

## 8. Claude Codeから使う

インストール済みコマンドをユーザー単位で登録する場合:

```powershell
claude mcp add --transport stdio --scope user fafnir -- fafnir mcp
claude mcp get fafnir
```

source checkoutを直接使う場合:

```powershell
claude mcp add --transport stdio --scope user fafnir -- `
  C:\path\to\stackforge\.venv\Scripts\python.exe `
  -m game_stack_planner mcp
```

チームで共有する場合は、各PC固有の絶対パスを直接commitせず、環境に合う起動コマンドを
`.mcp.json`へ設定します。Claude Codeはproject scopeのMCP serverを初回に承認するまで
実行しません。

```json
{
  "mcpServers": {
    "fafnir": {
      "type": "stdio",
      "command": "C:\\path\\to\\stackforge\\.venv\\Scripts\\python.exe",
      "args": ["-m", "game_stack_planner", "mcp"]
    }
  }
}
```

確認:

```powershell
claude mcp list
```

公式資料: [Claude Code MCP](https://code.claude.com/docs/en/mcp)

## 9. AIへの依頼例

```text
Fafnirを使い、所有アセット中心でこのゲームの実装構成を作って。
各候補の用途、互換性、不足機能、代替候補を分けて説明して。
```

```text
Fafnirで「浸水した通路の材質別足音」を検索して。
所有済みだけを対象にし、検索方式とスコアも示して。
```

```text
このUnity Projectに対して候補を互換性判定して。
導入はせず、manifest差分とリスクだけ提示して。
```

AIが主に使用するMCP tools:

- `fafnir_status`: DB、同期、RAG、商品詳細の状態
- `search_owned_asset_rag`: 所有アセットの意味検索
- `search_unity_assets`: 保存カタログの構造化検索
- `retrieve_game_stack_evidence`: ゲーム要件から比較材料を取得
- `get_unity_asset_candidate`: 1候補の根拠を取得
- `validate_cached_asset_for_project`: キャッシュとProjectの互換性検査
- `prepare_candidate_install`: 書き込み前の導入計画と差分

書き込み系は計画と承認を分離しています。Asset Storeの購入・importは自動化しません。

## 10. RAG DBの構築主体

構築するのは、CodexやClaudeのサービスではなくローカルで起動されたFafnir
プロセスです。

```text
Unity My Assets / 手動候補 / 公開商品情報
                    ↓
       Fafnir local process
                    ↓
 catalog.sqlite3: 文書 + 世代管理されたベクトル
                    ↓
      MCP検索結果だけをAI clientへ返す
```

UIとMCPは通常起動時に差分を検出し、必要な場合だけ索引workerを起動します。索引世代は
モデルID、固定revision、前処理方式、最大token数で分離されます。別モデルのベクトルや
未完成世代を検索へ混ぜません。

独立DBの例:

```powershell
# UI
fafnir --db D:\FafnirData\project-a.sqlite3 ui

# Codex / Claude用MCPも同じ独立DBへ向ける
fafnir --db D:\FafnirData\project-a.sqlite3 mcp
```

独立DBでは既定My Assetsファイルの自動取込を行わないため、必要なら明示的に同期します。

```powershell
fafnir --db D:\FafnirData\project-a.sqlite3 sync-my-assets `
  --path "$env:LOCALAPPDATA\game-stack-planner\unity-my-assets.json"
```

## 11. 新しいアセットを購入した後

1. Unity Editorの **Tools > Fafnir > My Assets Sync** で即時同期する。
2. Fafnir UIまたはMCPを起動したまま待つか、後で起動する。
3. `fafnir --json rag-status`で`ready`とcoverageを確認する。
4. 必要なら対象Unity Projectを指定して互換性を再評価する。

通常は`rag-index`を手動実行する必要はありません。索引が失敗または中断した場合の修復に
だけ使用します。

## 12. トラブルシューティング

### `rag-status`が`preparing_model`または`building`

初回モデル取得または索引中です。UI/MCPを終了しても既定DBのworkerは継続できます。
回線断後は次回起動時に再開します。

### `lexical_fallback`と表示される

Dense索引が100%未満です。検索自体は所有確認済み文書だけで継続します。

### CodexまたはClaudeからtoolが見えない

```powershell
fafnir mcp
codex mcp get fafnir
claude mcp get fafnir
```

最初のコマンドが起動したまま待機すればstdio server自体は正常です。終了は`Ctrl+C`。
その後、登録した実行ファイルの絶対パスと仮想環境を確認してください。

### DBをバックアップしたい

Fafnir、Codex、ClaudeのFafnir MCPを終了してから、`catalog.sqlite3`と同じ名前の
`-wal`、`-shm`が存在する場合は3ファイルを一緒にコピーしてください。

## 候補画像とダウンロード・インポート

候補カード、構成案、ASSETS一覧に商品画像と操作ボタンを表示します。ヘッダーの
「商品画像」をオフにすると画像を読み込みません。「商品画像を見る」から個別表示もできます。
古い候補の画像情報は、表示中のカードから順番に補完します。画像の取得に失敗しても
商品ページやダウンロード操作は利用できます。

- **詳細を見る**：商品画像・説明・商品ページと導入先をまとめて確認。
- **ダウンロード**：所有確認済みの商品をUnityの共通キャッシュへ取得。導入先指定は不要。
- **インポート…**：導入先を指定して取得・静的検査を行い、接続中の対象Unityに標準の
  Import Package画面を開く。キャッシュがあれば再ダウンロードしません。

Unityのファイル一覧でImportまたはCancelを選ぶと、Webの詳細画面へ結果が返ります。
インポート完了はファイル取り込みの完了で、コンパイルやシーンでの動作保証ではありません。
画像を見ただけでは取得・導入を実行しません。未購入・所有未確認の商品には取得ボタンを
表示しません。詳細画面を閉じてもUnity側の処理は続き、同じブラウザタブで開き直すと
進捗を再表示します。

Unityの標準確認画面は
[AssetDatabase.ImportPackageのinteractiveモード](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/AssetDatabase.ImportPackage.html)
を使います。無人で全ファイルを上書きする自動Importは行いません。

## 13. Gitへ含まれないもの

`.gitignore`はSQLite DB、WAL/SHM、Unity My Assets出力、索引log、モデルweightを除外します。
通常の`git push`でローカルRAG DBや所有一覧が送信されることはありません。commit前には
次で確認できます。

```powershell
git status --short
git ls-files "*.sqlite" "*.sqlite3" "*.safetensors" "unity-my-assets.json"
```
