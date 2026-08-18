# BentoSlide App prototype

このディレクトリは、既存BentoSlide engineの前に置くlocalhost専用GUIです。workflow、revision、writer lease、transaction、browser evidence、Bento保存は再実装せず、既存Python関数と既存Work editorへ委譲します。

```text
React + TypeScript
        ↓ HTTP / JSON
FastAPI Application API
        ↓ Python calls
Existing deck_workflow / Work editor / transaction engine
```

## 開発起動

Backend:

```powershell
python -m app.backend.main
```

Frontend:

```powershell
Set-Location app/frontend
npm install
npm run dev
```

Viteは`127.0.0.1:5173`で起動し、`/api`を`127.0.0.1:4180`へ転送します。

## Windowsで起動

リポジトリ直下の`start_bentoslide_app.cmd`をダブルクリックします。必要な場合だけfrontendをbuildし、Bento編集stageでは既存Work editorも起動した後、build済み画面をPython backendから`http://127.0.0.1:4180/`で配信します。HTML DesignはApp自身のsandboxed previewを使うため、別projectが既存preview portを利用中でも横取りしません。対応するNode.jsは`22.22.2`以降の22系、`24.15.0`以降の24系、または26以降です。対応Node.jsがないPCでは、公式Node.js LTS zipを`output/app-tools/`へダウンロードし、公式SHA-256と照合したportable copyだけを使用します。

停止は`stop_bentoslide_app.cmd`です。Appが起動した既存preview/editorだけを識別して停止し、先に動いていた既存workspaceは残します。

## API境界

- `GET /api/project`, `/api/state`, `/api/slides`: `deck.yaml`をUI向けview modelへ変換します。
- `GET /api/storyboard`: `REQUEST.md`、3つのplanning文書、任意のvisual plan、section／chapter順を読み取り専用view modelとして返します。
- `POST /api/storyboard/initialize`: `initialized`で一次資料を確認し、既存の初期化処理へ委譲します。
- `POST /api/storyboard/submit`: `planning`でplanning文書とsection／chapterが揃っていることを検証し、確認したrevisionのまま構成案を確認待ちにします。
- `POST /api/storyboard/approve`: `awaiting_plan_approval`で確認したrevisionを再検証し、変化がなければHTML制作へ進めます。
- `GET /api/html/review`: 人が読むsummary、impact、affected slideとopaque action tokenだけを返します。
- `POST /api/html/review/apply`: 全affected slideの確認を検証してから、既存approve/apply/browser-check関数を順に呼びます。
- `POST /api/html/review/approve-deck`: 既存whole-deck approvalを呼びます。
- `POST /api/convert`: `{ "confirmed": true }`を受け、承認済みHTMLの変換を1件だけバックグラウンドで開始します。
- `GET /api/convert/status`: 実際の変換段階、完了ステップ、失敗理由、再試行可否を返します。
- `GET /api/bento`: 現在stageで既存Bento Work editorを利用できるか返します。
- `GET /api/bento/lifecycle/status`: Bento承認・完成処理の実際の段階、完了ステップ、再試行可否、現在利用できる操作を返します。
- `POST /api/bento/content/review`: authoring editorを安全に停止し、既存の内容確認開始処理後にauthoring editorを再開します。
- `POST /api/bento/content/approve`: editor停止後に現行revisionを既存処理で承認し、finalをtransactionで初期化してfinalization editorを開きます。過去のfinalと不一致な場合は、既存の専用archive/restart経路を使います。
- `POST /api/bento/final/approve`: finalization editorを停止し、現行revisionのみを承認して`complete`へ進めます。
- `POST /api/bento/final/open`: 既存のstage-aware launcherで完成版を開きます。
- `POST /api/bento/final/reopen`: 既存の再開処理で最終承認を無効化し、finalization editorを再開します。
- `GET /api/ai/status`: Codex SDKの利用可否、利用できる操作、実処理段階、失敗理由、再試行可否だけを返します。
- `POST /api/ai/proposals`: `{ "confirmed": true, "slideId": "...", "action": "shorten", "instruction": "..." }`を受け、確認用のwhole-deck候補を1件だけバックグラウンド生成します。

5つのBento lifecycle POSTはすべて`{ "confirmed": true }`のみを受け付け、`202 Accepted`でバックグラウンド処理を開始します。既知のstage不一致、重複実行、検証できないeditor sessionは`409 Conflict`で拒否します。処理中はAppがstatusを定期取得し、実際の段階と完了数を表示します。

Frontendへproposal digest、revision、任意のartifact path、PIDは返しません。action tokenはprocess-localかつ現在のproposal状態へ固定され、再起動や状態変化で無効になります。

## App内のStoryboard確認フロー

`initialized`、`planning`、`awaiting_plan_approval`では、左にsection別のスライド一覧、中央に構成カード、右に依頼・説明方針・全体ストーリー・スライド構成と選択中の詳細を表示します。Markdown本文はHTMLとして挿入せず、文字列・段落・箇条書きへ変換して表示します。Visual planがある場合だけ、推奨ビジュアルの種類と意図をカードとInspectorへ添えます。

Inspectorに表示する操作は現在stageの1つだけです。

```text
initialized
  -> 構成作成を開始
planning
  -> 構成案を提出
awaiting_plan_approval
  -> この構成を承認
```

各POSTは`{ "confirmed": true, "actionToken": "..." }`を必要とします。action tokenは`deck.yaml`、表示対象の各planning文書、visual plan、section／chapterの順序と状態を、path・有無・byte長・個別SHA-256を持つcanonical recordへ固定したprocess-local値です。提出・承認では同じartifact群のOSレベルwriter leaseを取得して再照合し、画面表示後または遷移直前に内容が変わった場合は`409 Conflict`として`deck.yaml`を変更しません。必要な文書とsection／chapterが揃うまでは提出・承認操作を表示しません。ReactやApplication APIは`deck.yaml`とplanning文書を直接変更せず、既存の`deck_workflow`関数だけを呼びます。

承認直後の`html_authoring`でHTMLがまだ存在しない間は「HTMLを準備しています」と表示します。この状態ではHTML review、slide preview、AI Actionsを要求しません。HTMLデザインが既存経路で作成された後に「状態を更新」すると、通常のHTML Design確認へ切り替わります。

## App内のBento承認フロー

AppのInspectorには現在のstageで安全な次の1操作だけが表示されます。

```text
bento_authoring
  -> 内容確認へ進む
content_review
  -> この内容を承認して最終調整へ進む
bento_finalization
  -> 最終版を承認して完成
complete
  -> 完成版を開く / 最終調整を再開
```

各操作は確認ダイアログ後に開始します。承認の前に既存Work editorを停止するため、未保存の編集がないことを確認してから進めてください。処理中は旧iframeを隠し、新しいsessionを検証できた後だけ再表示します。失敗した場合は成功扱いにせず、安全な途中stageから「再試行」で続行できます。

App内でバックグラウンド実行するBento lifecycle actionは、1リポジトリにつき同時に1件です。各workflowコマンドとWork editorのwriter lease/transaction/session identity検証は既存実装が保護します。App API自身は`deck.yaml`、Bento HTML/JSON、registry、final成果物を直接書き換えません。

## AI Actions（任意）

通常のAppとテストはCodex SDKなしでも起動できます。AI Actionsを利用するPCだけ、通常依存関係に加えて次をインストールしてください。

```powershell
python -m pip install -r requirements-ai.txt
```

公式`openai-codex` Python SDKは既存のローカルCodex認証を再利用します。Codexへ未サインインの場合は、先にCodexアプリ等でサインインしてからBentoSlide Appを再起動してください。モデルはローカルCodex設定を既定とし、運用上固定する場合だけBackend起動前に`BENTOSLIDE_AI_MODEL`を設定します。モデル名、認証情報、thread IDはFrontendへ返しません。

AI Actionsを利用できるのは、`whole_deck`方式の`html_review`で、未解決の変更案がないときだけです。対象は選択中のスライドで、操作は次の4種類です。

- `shorten`: 既存情報を増やさず短くする
- `add-diagram`: bitmapや外部assetを増やさず、編集可能なHTML/CSS/SVG図を提案する
- `improve-structure`: 対象と明示された関連スライドの構成を整える
- `custom`: 入力した指示に沿って候補を作る（指示必須）

各ジョブはgit管理外の`.bento-ai/runs/<job>/`へ隔離され、現在のHTML／registry、選択・操作情報、許可されたprimary source、必要な仕様だけをコピーします。SDKはその作業領域を`workspace_write`で使用し、ネットワークは無効です。出力は完全なcandidate HTML／registry／結果JSONとして検証されます。対象外スライド、既存ID、式、数値、asset、provenance、入力ファイルに不正な変更があれば、既存のproposal登録処理を呼ぶ前に失敗します。

成功しても現在案、承認、変換は自動実行されません。Appは既存の「変更案」previewへ切り替えるだけです。人が影響スライドを確認し、「この変更案全体を反映」を明示的に実行したときだけ、既存approve → apply → browser check経路が動きます。

### AI Actionsのトラブルシューティング

- 「Codex SDKが見つかりません」: 上記`requirements-ai.txt`を、Backendが使用するPythonへインストールします。
- 「Codexへサインイン」: ローカルCodexでサインイン後、Backendを再起動します。認証情報をApp画面へ入力する必要はありません。
- 「現在の状態では利用できません」: HTML全体の確認画面へ戻り、既存の変更案を確認・反映または取り消します。
- 候補検証の失敗: 対象や補足指示を狭くして再試行します。失敗した候補が現在案へ反映されることはありません。
- Backend再起動: 実行中だったジョブは成功扱いにせず、再試行可能な失敗として表示します。

## 現在の制約

- AppはWindowsの既存PowerShell launcherでWork editorを起動・停止します。Bento lifecycle操作のeditor連携はWindows専用です。
- 保存自体は既存Work editorが担当します。React側にBento編集機能は再実装していません。
- AI ActionsはoptionalなCodex SDK機能です。ネットワークなしの隔離領域で候補だけを生成し、画像生成、Bento直接編集、自動承認、自動反映、自動変換は行いません。
- Storyboardは今回、確認・提出・承認までの読み取り専用です。App内でplanning文書を編集する機能、スライドの追加・削除・並べ替え、visual plan編集、AIによる構成作成は未実装です。
