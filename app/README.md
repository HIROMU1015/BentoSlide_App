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

リポジトリ直下の`start_bentoslide_app.cmd`をダブルクリックします。必要な場合だけfrontendをbuildし、Bento編集stageでは既存Work editorも起動した後、build済み画面をPython backendから`http://127.0.0.1:4180/`で配信します。HTML DesignはApp自身のsandboxed previewを使うため、別projectが既存preview portを利用中でも横取りしません。Node.jsがないPCでは、公式Node.js LTS zipを`output/app-tools/`へダウンロードし、公式SHA-256と照合したportable copyだけを使用します。

停止は`stop_bentoslide_app.cmd`です。Appが起動した既存preview/editorだけを識別して停止し、先に動いていた既存workspaceは残します。

## API境界

- `GET /api/project`, `/api/state`, `/api/slides`: `deck.yaml`をUI向けview modelへ変換します。
- `GET /api/html/review`: 人が読むsummary、impact、affected slideとopaque action tokenだけを返します。
- `POST /api/html/review/apply`: 全affected slideの確認を検証してから、既存approve/apply/browser-check関数を順に呼びます。
- `POST /api/html/review/approve-deck`: 既存whole-deck approvalを呼びます。
- `GET /api/bento`: 現在stageで既存Bento Work editorを利用できるか返します。

Frontendへproposal digestやrevisionは返しません。action tokenはprocess-localかつ現在のproposal状態へ固定され、再起動や状態変化で無効になります。
