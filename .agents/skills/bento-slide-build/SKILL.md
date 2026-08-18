---
name: bento-slide-build
description: Manage the repository-centered BentoSlide workflow and build, inspect, validate, author, browser-check, fast-edit, and finalize deterministic Bento Slides from a single fixed-size HTML/registry deck, migrated modular HTML chapters, imported static HTML, or legacy coordinate JSON. Use for short deck commands, deck.yaml stages, section approval, HTML preview/conversion, Bento authoring and content approval, segment import/replace, localhost Work editor persistence, transaction recovery and writer leases, final presentation edits, runtime integrity, screenshots, or final validation in this repository.
---

# Bento slide build

Work from the repository root. Read `START_HERE.md`, `deck.yaml`, and `python -m scripts.deck_workflow status --json` first. Route through `workflow/WORKFLOW.md`; do not ask the user for routine paths, IDs, logs, ports, or CLI steps. Use the manifest-listed sources as factual authority. Do not redesign approved composition, rewrite copy, or modify the Bento runtime.

## Route the workflow

Use natural conversation as the primary route. Translate the user's intent to one high-level operation and stop at the next human decision. Do not expose internal stage names, IDs, revisions, registry mechanics, paths, or CLI syntax unless the user asks for diagnostics. Capture a new substantive brief with `capture-request --text`.

For new schema v2 single/imported decks, default to `whole_deck`: author the complete HTML/registry pair, preview the whole story, and use one HTML approval before conversion. Treat sections as internal planning, provenance, digest, and impact scopes rather than separate user checkpoints. Existing states without a strategy and explicit `rolling_sections` projects keep the section-by-section lifecycle.

When the user requests an HTML correction after the whole deck is in review, require the current dependency-bound HTML review baseline and never overwrite canonical HTML or shared CSS/assets first. Treat even a single-slide correction as an editorial request against the complete story. Before candidate authoring, inspect prerequisites, adjacent transitions, repetition, terminology, numbering/cross-references, conclusions, slide order/count, and section boundaries, and classify the recommendation as local, related, or structural/global. Explicitly say when no wider change is recommended. Include and justify related slides when needed. If a materially broader reorder, addition/removal, section split/merge, or story rewrite would be better, propose that direction and affected slides before building it; neither silently narrow the request nor silently broaden it. Slide-by-slide messages accumulate: reassess the whole-deck effect each time, and cancel/recreate an active candidate when new intent makes its bound scope or explanation stale.

For the agreed scope, create a complete temporary candidate and run the reviewed change route. Tell the user, by slide title, what will change and whether other slides are implicated by narrative continuity, ordering, membership, registry/provenance, assets, or shared CSS/theme. Include machine-detected changed/affected slides and any additional related slides from semantic reasoning. Ask whether that exact candidate is acceptable. Only an explicit chat confirmation or the revision-bound preview action may approve and apply it. The preview's per-slide `未確認` / `確認済み` / `要修正` states are a review checklist, not separate approval records or partial-apply permission. Confirmation is revision-bound to canonical HTML/registry, candidate HTML/registry, both complete local-dependency manifests, the human explanation, and recomputed impact; stale or tampered evidence requires a fresh proposal. Apply must revalidate those inputs while one union writer lease covers both canonical and candidate artifacts plus their dependencies. After apply, close the comparison UI and run `check-html-change`: browser-check all affected slides that remain in the deck for transformed visible bounds, text-bearing element overflow, visual consistency, and story continuity, and persist revision-bound report, environment, and screenshot evidence. A separate whole-deck HTML approval is still required after current evidence exists. See `docs/html-change-review.md`.

In the optional rolling route, record exactly one canonical source (`planning`, `html`, or `bento`). HTML approval authorizes promotion but is not Bento acceptance. Promotion uses a section-only registry projection, planning order, browser conversion, revision-checked authoring storage, and a state/artifact transaction; it never rebuilds unrelated authoring slides or changes generated/final. A promoted HTML section is historical, not automatically synchronized back. After every section is accepted, require whole-deck content review.

The former fixed phrases are compatibility aliases only; use `docs/legacy-command-aliases.md` when their exact checkpoint mapping is needed. Do not present them as the primary UX.

Use `scripts.deck_workflow` for every state change. Recompute revision/digest validity rather than trusting chat history. Run `resume` after resolving a blocker. For schema v1, run `migrate --dry-run` before `migrate`; migration is stage-preserving and late-stage evidence must validate before any change.

Write canonical planning documents only through `python -m scripts.deck_workflow write-planning-artifact --artifact ... --from-file ...` or `command_write_planning_artifacts`. Do not edit `planning/explanation-policy.md`, `planning/story-outline.md`, `planning/slide-plan.md`, or `planning/visual-plan.yaml` directly. This route holds the same cross-process lease as section/chapter configuration and plan submission/approval, so a reviewed revision cannot be replaced or overwritten during a transition.

For schema v2 project metadata, use `python -m scripts.deck_workflow set-project --kind ... --title ...` only in `initialized` or `planning`. It is an agent-facing setup command rather than a ninth user short phrase; it changes neither workflow stage nor approvals.

## Propose and manage visuals

For every planned slide, decide whether prose is sufficient, a diagram improves understanding, an original source figure is necessary, a Bento-native diagram can express the idea, or a generated image is justified. Propose useful concept, structure, relationship, flow, architecture, hierarchy, comparison, timeline, and state-change visuals without waiting for the user to request a diagram. Keep the UX conversational; do not ask the user to edit visual YAML, crop images, place assets, or update registry entries.

Prefer native HTML text/shape/connector diagrams. Use `source-original` only for the actual source figure with a registered source ID and precise locator. Use `source-derived` for a reconstruction based on one or more located source passages. Use `generated` for explanatory art that is not evidence and give it no source provenance. Never generate numerical data, experimental or measurement results, benchmark results, quantitative plots, or equations; use registered data and LaTeX/native equations instead.

When present, author and validate `planning/visual-plan.yaml`. Use the visible local image library: keep user-supplied images in `images/user`, PDF/source crops in `images/extracted`, and generated explanatory art in `images/generated`. Register those images and extract PDF figures with `scripts.register_visual_asset`; it preserves extracted crops in the library, selects hidden `deck/assets/source`, `deck/assets/local`, or `deck/assets/generated`, and updates the registered file, SHA-256 `contentDigest`, and registry in one transaction. Images carry both `data-asset-id` and `data-figure-id`. For a source-derived native diagram, register one assetless figure and put its `data-figure-id` on every participating text/shape/connector. Read `docs/visual-workflow.md` before visual work. Visual creation/redesign stays inside whole-deck candidate review or the explicitly selected rolling lifecycle.

## HTML authoring and conversion

For schema v2 `single`/`imported`, use the paths in `authoring.entryHtml` and `authoring.registry`. Each slide is a 1280 x 720 `section.slide` with stable `data-slide-id` and `data-section-id`. Read `docs/html-first-authoring-contract.md`. Treat the pair as the pre-conversion source of truth; whole-deck approval records every section's DOM, registry projection, asset hashes, and global CSS/theme digest at one checkpoint.

Build the configured single pair:

```powershell
python -m scripts.build_bento_from_html --html deck/deck.preview.html --registry deck/deck.registry.json --base Bento_Slides.base.bento.html --output output/presentation.generated.bento.html
```

Migrated modular decks instead use `--html-dir chapters/ --registry-dir chapters/`. Preserve native semantic elements and localize fallback to the smallest block. Inspect conversion report, computed layout, resource scan, browser check, browser-environment fingerprint, screenshots, native/fallback classes, crop results, reference/protected checks, serialize round-trip, and runtime fingerprint. Browser conversion uses one shared Chromium with isolated deterministic contexts and blocks all HTTP(S), including loopback; the known Bento release-manifest probe remains blocked/recorded while any other blocked request fails. For interactive iteration only, `--incremental` may reuse slide evidence from `output/.bento-cache/` when its PNG revision also matches; never use it at conversion, content/final approval, or completion gates, which require full build/full validation. The cache is neither source of truth nor approval evidence. For reproducibility:

```powershell
python -m scripts.check_html_first_determinism --html deck/deck.preview.html --registry deck/deck.registry.json --base Bento_Slides.base.bento.html --report output/determinism-report.json
```

## Bento authoring

After `mark-converted` and `begin-authoring`, use `start_deck_workspace.cmd`. Authoring mode may change content/structure and its registry, but every save must use the Work editor API or common storage transaction with both base revisions. Never overwrite authoring HTML/JSON/registry directly. Existing ID/type changes require explicit slide replacement.

The server holds the artifact-set OS writer lease. An offline tool may write only after acquiring the same lease, or through a localhost API whose repository/mode/targets match exactly. Recover unfinished journals before reads/writes. Never roll back a valid commit because only its report failed.

Writer exclusion is per canonical artifact, not merely per complete set: any overlapping target conflicts, while disjoint sets may proceed. Treat `noOp: true` saves as successful validation without a backup or transaction.

For an added, ordered, or targeted replacement segment during `bento_authoring`:

```powershell
python -m scripts.bento_segment import --html scratch/segments/add.preview.html --registry scratch/segments/add.registry.json
python -m scripts.bento_segment replace --html scratch/segments/replacement.preview.html --registry scratch/segments/replacement.registry.json --slide-id target-slide
python -m scripts.bento_segment insert-before --html scratch/segments/add.preview.html --registry scratch/segments/add.registry.json --anchor-slide-id existing-slide
python -m scripts.bento_segment replace-section --html scratch/segments/section.preview.html --registry scratch/segments/section.registry.json --target-slide-id first --target-slide-id second
```

Require browser round-trip evidence, outside-slide hash invariance, cross-slide reference validity, and shared-registry safety. Do not change generated or final. A full HTML reset is exceptional and requires `reset-authoring-from-html --confirm RESET-AUTHORING-FROM-HTML` in authoring stage.

Content approval must match both current `sha256:` revisions and the canonical `bento/content-approval/v1` digest. Any authoring document/registry mutation invalidates it. Only approved current revisions may be copied to final HTML/JSON/registry plus document/registry baselines.

Allow provenance drafts while authoring, but before content review require `equationId` for equations, `chartId` for charts, `tableId` for tables, and `figureId` or `assetId` for source-backed image/SVG elements. Require origin-bearing embedded image bytes to match the registry `contentDigest`, and reject ordinary Work editor attempts to add, relabel, or change `source-original` identity. Reject `unprovenancedDraft` at that gate. Treat revision backups as complete only when their HTML/JSON/registry byte revisions match the transactionally written manifest.

## Finalization

In finalization, final `#bento-doc`, frozen final registry, and immutable baselines are authoritative. Use the browser UI for judgment/direct manipulation. For exact geometry, style, theme, background, or z-order, read `docs/fast-final-editing.md`, batch all requested changes, dry-run if uncertain, then save once:

```powershell
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json
```

Do not use fast-final editing for text/equations, data/media, IDs/types, slides, notes, behavior, or references. Do not reconvert into final, use `--reset-final`, or relax content protection for an ordinary layout request.

If the user requests content or structure during finalization or after completion, route to `reopen-current-section` and the authoring canonical artifact. Re-accept the affected section and repeat mandatory whole-deck content approval before attempting finalization again. Preserve the existing final and baselines until the newly approved handoff is explicitly initialized; never weaken final-mode validation. Stop the final editor, then use the dedicated archival restart (`restart-finalization-from-authoring --confirm ARCHIVE-AND-RESTART-FINALIZATION`) or the equivalent approved-content conversational route. It must archive the complete old final/baseline set with a revision manifest and atomically install final/baselines/state from approved authoring; generated stays unchanged.

The injected toolbar must preserve `window.bento.serialize()` as a synchronous HTML-string API. It is detached before serialization and restored in `finally`; toolbar/host/loader/style identifiers never persist. Describe `loadDoc()` mutation as API editing, not simulated typing/dragging.

Final validation checks HTML/JSON equality, runtime fingerprint, recursive resources, references, frozen registry, both baselines, protected fingerprint, revisions/backups, serialize round-trip, and browser evidence.

Before `approve-final`, stop the final Work editor so its lifetime lease is released. Approval is bound to the exact document, HTML, registry, and runtime revisions; `complete` must recompute them. If edits are needed after approval or completion, run `python -m scripts.deck_workflow reopen-finalization` first and never write while the old approval remains current.

## Static HTML import

Treat general HTML as untrusted. Read `docs/html-import.md`, keep the original under `imports/`, and use:

```powershell
python -m scripts.import_html_deck --input imports/source.html --slide-selector ".slide"
```

Never execute/import scripts or fetch remote resources. Require an explicit selector when ambiguous. Preview/convert only normalized static output.

## Legacy JSON-first

Keep the legacy path unchanged:

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
```

For all routes, run the relevant focused tests, full `python -m unittest discover -v`, and the browser-gated suite when browser evidence is required. Keep implementation logic in `bento_converter/` and `scripts/`; do not duplicate it in this skill.
