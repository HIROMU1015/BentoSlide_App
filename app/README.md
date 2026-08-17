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

5つのBento lifecycle POSTはすべて`{ "confirmed": true }`のみを受け付け、`202 Accepted`でバックグラウンド処理を開始します。既知のstage不一致、重複実行、検証できないeditor sessionは`409 Conflict`で拒否します。処理中はAppがstatusを定期取得し、実際の段階と完了数を表示します。

Frontendへproposal digest、revision、任意のartifact path、PIDは返しません。action tokenはprocess-localかつ現在のproposal状態へ固定され、再起動や状態変化で無効になります。

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

## 現在の制約

- AppはWindowsの既存PowerShell launcherでWork editorを起動・停止します。Bento lifecycle操作のeditor連携はWindows専用です。
- 保存自体は既存Work editorが担当します。React側にBento編集機能は再実装していません。
- AI Actionsは引き続きplaceholderで、自動編集は行いません。
