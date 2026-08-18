# BentoSlide0

論文や既存HTMLから、編集可能なBento Slidesをローカルで制作するための自己完結型リポジトリです。標準経路は、単一の固定サイズHTMLとregistryを視覚設計の正本にし、Chromiumで計算済み座標を取得し、公式Bento runtimeを変更せずネイティブ要素へ変換します。旧coordinate design JSON経路も互換用として維持しています。

## BentoSlide App prototype

既存engineをそのまま利用するReact + FastAPIのデスクトップ向けGUIを`app/`に用意しています。Storyboardの確認・提出・承認、HTML Review、変換、Bento編集と承認を既存workflowへ委譲してApp内から進められます。Windowsでは`start_bentoslide_app.cmd`、停止は`stop_bentoslide_app.cmd`を使用します。従来の`start_deck_workspace.cmd`は引き続き独立して利用できます。構成と開発起動は[app/README.md](app/README.md)を参照してください。

## 最短の使い方

1. このリポジトリを資料ごとに複製します。
2. 一次資料を`sources/private/`へ置きます。
3. 必要なら手持ち画像を`images/user/`へ置きます。抽出画像と生成画像はWorkが`images/extracted/`と`images/generated/`へ整理します。
4. ChatGPT Workへ、作りたい資料を普段の言葉で伝えます。
5. Workが依頼を`REQUEST.md`へ保存し、曖昧でなければsource manifestも準備します。
6. Workが各slideで図の有効性も判断し、必要ならnative図・出典付き原図・生成visualを提案します。
7. 構成を確認したら、Work/GPTが資料全体のHTMLを作ります。
8. 全体previewを見て、気になる箇所を普段の言葉で伝えます。
9. Work/GPTが修正案と、関連slide・構成・共通styleへの影響を先に示します。確認後だけ候補版を適用します。
10. HTML全体を承認してBentoへ変換し、Bento内容を確認します。
11. 最終調整ではレイアウト・styleだけを仕上げます。

ファイル名、section番号、状態更新、ログ、port、変換コマンドはエージェントが`deck.yaml`から判断します。`deck.yaml`はschema v2の唯一の機械状態です。Windowsでは`start_deck_workspace.cmd`がstageに応じてHTML preview、authoring editor、final editor、完成版viewerを選びます。従来の短文コマンドも互換経路として維持しています。

標準の正本は次の順に切り替わります。

```text
sources + planning
  -> deck/deck.preview.html + deck/deck.registry.json
  -> output/presentation.generated.bento.* + generated registry
  -> output/presentation.authoring.bento.* + authoring registry
  -> 承認済みauthoring revision
  -> output/presentation.final.bento.* + frozen final registry + baseline
```

詳細なstageと承認は[workflow/WORKFLOW.md](workflow/WORKFLOW.md)、HTML修正の確認契約は[docs/html-change-review.md](docs/html-change-review.md)、正本ルールは[docs/source-of-truth-policy.md](docs/source-of-truth-policy.md)、保存保証は[docs/artifact-transactions.md](docs/artifact-transactions.md)を参照してください。

図の自動提案、`source-original` / `source-derived` / `generated`、PDF figure切り出し、asset登録、捏造防止ルールは[docs/visual-workflow.md](docs/visual-workflow.md)を参照してください。

## Developer setup

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m scripts.deck_workflow validate
python -m scripts.deck_workflow status --json
```

旧schema v1資料は、変更内容を先に確認してから移行できます。

```powershell
python -m scripts.deck_workflow migrate --dry-run
python -m scripts.deck_workflow migrate
```

後期stageの移行は既存final・sidecar・baseline・revisionを保持し、検証済みmerged registryからfinal registryとregistry baselineをtransactionで作成します。不足時は元artifactを変更せず失敗します。

## HTML-first conversion

schema v2の標準single-file build:

```powershell
python -m scripts.build_bento_from_html `
  --html deck/deck.preview.html `
  --registry deck/deck.registry.json `
  --base Bento_Slides.base.bento.html `
  --output output/presentation.generated.bento.html
```

移行済みのmodular資料では従来の`--html-dir chapters/ --registry-dir chapters/`を使えます。生成物にはHTML/JSON、registry、conversion report、computed layout、resource scan、browser check、`diagnostics/browser-environment.json`、source/Bento screenshotsが含まれます。ローカルresourceはdata URI化され、未解決resource、参照不整合、runtime変化、critical crop失敗、serialize失敗はbuildを失敗させます。source計測とBento確認は1つのChromium process内の分離contextで実行され、loopbackを含むHTTP(S)は遮断されます。Bento runtimeの既知release-manifest probeだけは遮断・記録したまま期待済みとして扱い、それ以外のrequestは失敗します。

編集反復では同じ出力先へ`--incremental`を付けると、slide DOM、関連registry/assets、global CSS/theme、runtime、browser/font environmentが一致するslideだけ`output/.bento-cache/`から再利用します。source/Bento PNGはrecord内SHA-256と一致する場合だけ再利用され、不一致や中断recordはcache missになります。通常buildはcacheを再利用せずfull evidenceを再生成し、workflowの変換・承認gateでも必ずfull build/full validationを使います。cacheは正本でも承認証跡でもありません。

whole-deck HTML承認は、各section DOM、参照registry projection、参照asset content、global CSS/themeから決定論的digestを一括記録します。承認後の変更は該当section（global CSS/themeは全section）を未承認へ戻し、変換を拒否します。修正候補は承認前に正本へ入らず、人向け説明と機械判定を同じdigestへ固定して影響範囲を提示します。適用時は正本と候補を同一lease内で再検証し、適用後の影響slideについてbrowser report・環境fingerprint・screenshotが揃うまで全体承認へ進みません。

## Bento authoringとfinalization

変換・検証後は、まず`bento_authoring`で内容と構造を編集します。Work editorのauthoring modeはBento HTML/JSON/registryの2つのrevisionを同時に検証し、3 artifactを同一transactionで保存します。内容承認はdocument revision、registry revision、および次のcanonical digestへ固定されます。

```text
sha256(UTF-8("bento/content-approval/v1\0" + documentRevision + "\0" + registryRevision))
```

承認後にどちらかが変わると、承認は同じstate transactionで無効化されます。`begin-finalization`は承認済みauthoringをfinal HTML/JSON/registryとdocument/registry baselineへ一括初期化します。

`bento_finalization`ではfinalの`#bento-doc`が正本です。内容・構造・registryは凍結され、geometry、presentation style、theme/background、z-orderだけを変更できます。正確な一括変更には`scripts.apply_bento_final_edits`を使用します。HTML-first変換でfinalを上書きせず、通常運用で`--reset-final`や`--allow-content-edit`を使いません。

Work editorを直接起動する場合:

```powershell
python -m scripts.run_bento_work_editor `
  --mode authoring `
  --source output/presentation.generated.bento.html `
  --target output/presentation.authoring.bento.html `
  --source-registry output/diagnostics/merged-registry.json `
  --target-registry output/presentation.authoring.registry.json `
  --repository . `
  --port 8765
```

stage-aware launcherの利用を推奨します。詳細は[docs/work-editor-finalization.md](docs/work-editor-finalization.md)と[docs/authoring-lifecycle.md](docs/authoring-lifecycle.md)にあります。`window.bento.serialize()`はtoolbar注入後も同期的にHTML文字列を返し、一時UIは保存結果へ入りません。

## Segment追加・置換と既存HTML import

`bento_authoring`では、`scratch/segments/`のHTML/registryペアを変換して追加、または明示したslide IDだけを置換できます。

```powershell
python -m scripts.bento_segment import --html scratch/segments/add.preview.html --registry scratch/segments/add.registry.json
python -m scripts.bento_segment replace --html scratch/segments/replacement.preview.html --registry scratch/segments/replacement.registry.json --slide-id target-slide
```

対象外slide hash、cross-slide reference、shared registry、resource、browser round-tripを検証し、generated/finalは変更しません。server起動中は一致するlocalhost APIだけをwriterとして使い、識別できなければ拒否します。

一般HTMLは`imports/`へ原本を隔離してから静的に正規化します。

```powershell
python -m scripts.import_html_deck --input imports/source.html --slide-selector ".slide"
```

scriptは実行・移入せず、networkを遮断し、event handlerと`javascript:` URLを除去し、remote resourceや危険な埋め込みをreportします。selectorを安全に決められない場合は明示指定が必要です。詳しくは[docs/html-import.md](docs/html-import.md)を参照してください。

## Crash safety and concurrency

複数artifactの更新は、OS排他writer lease、短時間transaction lock、fsync済みtemporary/backup、永続journalを使います。起動・status・操作前に未完了journalを復旧し、部分置換は全体rollback、全targetがnew revisionならcommit完了処理を行います。安全判定できない場合はartifactを変更せず停止します。report生成だけの失敗では正常artifactをrollbackせず、`report_failed`を次回復旧します。

## Legacy JSON-first

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
```

旧仕様は[docs/bento-conversion-spec.md](docs/bento-conversion-spec.md)にあります。

## Verification

```powershell
python -m unittest discover -v
$env:BENTO_BROWSER_TEST = "1"
python -m unittest discover -v
Remove-Item Env:BENTO_BROWSER_TEST
```

GitHub Actionsは単一のclass/test-ID manifestからLinuxのunit、HTML-first browser integration、determinismとWindows launcher groupを選択し、未分類classや存在しないoverrideをunitで拒否します。unit groupはChromium起動自体を禁止します。browser jobはPlaywright Chromium binaryをversion固定keyでcacheし、各jobの証跡は最後に`html-first-evidence`へ統合されます。
