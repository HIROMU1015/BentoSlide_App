# Local BentoSlide workflow

`deck.yaml` is the sole machine-readable state. Agents use `python -m scripts.deck_workflow`; users do not run state commands, choose IDs, or name files.

## Stages

| Stage | Owner | Source of truth | Exit condition |
| --- | --- | --- | --- |
| `initialized` | Work | sources | primary source resolved |
| `planning` | Work | sources + planning | policy, story, slide plan, section list exist |
| `awaiting_plan_approval` | Work | planning | user approves material plan |
| `html_authoring` | Work | single HTML/registry or migrated chapters | complete deck or current compatibility unit is ready |
| `html_review` | Work | same HTML/registry | exact whole-deck candidate or compatibility unit is approved |
| `ready_for_conversion` | Codex | all approved HTML units | every approval digest remains current |
| `converting` | Codex | approved HTML units | deterministic build/evidence exists |
| `bento_validation` | Codex | generated Bento + generated registry | generated bundle passes and authoring artifacts initialize/retain safely |
| `bento_authoring` | Work | authoring Bento HTML/JSON/registry | content/structure edits validate |
| `content_review` | Work | authoring Bento HTML/JSON/registry | exact document and registry revisions approved |
| `bento_finalization` | Work | final `#bento-doc`, frozen final registry, baselines | presentation-only edits and final approval pass |
| `complete` | Codex | final artifacts and baselines | final technical verification recorded |
| `blocked` | Work or Codex | saved pre-block source | reason resolved and `resume` revalidates it |

Expected owner/source and real artifacts are invariant checked. Approval stages are never crossed automatically.

## Whole-deck HTML lifecycle (primary UX)

New schema v2 single/imported decks normally use `authoring.strategy: whole_deck`:

```text
planning -> complete HTML/registry -> whole-deck preview
         -> reviewed change proposals -> one HTML approval -> deterministic conversion
```

The first HTML review registers every slide under its planned section and stores a baseline for the exact HTML, registry, complete deck evidence, and all local dependencies, but the user reviews the deck as one story. Later natural-language edits never overwrite reviewed canonical HTML immediately. The agent creates a temporary candidate and a machine-readable `bento/html-change-proposal/v3` report containing the requested, changed, related, added, removed, reordered, and affected slides; section, registry, structural, and global-style impact; dependency manifests; and human-facing summaries. Global CSS/theme or structural changes conservatively require whole-deck review. Registry changes expand review to every affected section.

Before candidate authoring, every targeted correction receives a whole-deck narrative assessment. The agent checks prerequisites, adjacent transitions, duplication, terminology, numbering and cross-references, conclusions, ordering, slide count, and section boundaries, then classifies the recommendation as local, related, or structural/global. A sequence of slide-specific requests is not a command to freeze the rest of the deck; each new request is evaluated together with the accumulated pending intent. Local proposals explicitly state that no wider change is recommended. Related proposals identify and justify the additional slides. A materially broader reorder, slide addition/removal, section split/merge, or story rewrite is proposed to the user before candidate authoring rather than being silently omitted or silently introduced. If a proposal is already active, a new correction amends it and requires cancellation/recreation when its bound candidate or impact is no longer exact.

The user is told what the candidate changes and whether it can affect other slides before confirmation. The preview sidebar switches between current and candidate versions, lists every affected slide, and stores browser-local `未確認` / `確認済み` / `要修正` checklist state. These marks are not partial approvals. Once all affected slides are confirmed, its revision-bound action may perform `approve-html-change`, `apply-html-change`, and `check-html-change` in sequence; explicit chat confirmation remains equivalent. `approve-html-change` binds confirmation to the current canonical HTML/registry revisions, exact candidate revisions, both dependency manifests, human explanation, and recomputed impact. `apply-html-change` is the only route that replaces canonical HTML/registry; it revalidates every bound input and dependency under one union writer lease and commits transactionally. The preview then closes comparison and shows the updated canonical deck. `check-html-change` stores revision-bound browser/environment/screenshot evidence for every affected slide still present, using transformed visible bounds and text-bearing element overflow checks. A separate whole-deck HTML approval is refused until that evidence is current. A stale or tampered proposal is rejected; cancellation remains safe because it never installs candidate bytes. See `docs/html-change-review.md`.

Sections are still required for planning order, provenance closure, approval digests, impact reporting, and optional targeted work, but they are not separate user approval gates in whole-deck mode. One final HTML approval stores every current section digest and opens conversion.

## Rolling section lifecycle (optional compatibility route)

`authoring.strategy: rolling_sections` retains section-by-section production for migrated work, very large decks, or an explicitly chosen incremental process:

```text
planned -> html_authoring -> html_review -> bento_integration -> bento_authoring -> accepted
```

`canonical` is exactly one of `planning`, `html`, or `bento`. `slideIds` records current canonical membership, while `bentoSlideIds` retains the installed authoring membership during an HTML redesign. HTML approval records the current section digest and authorizes `promote-current-section`; it does not accept the Bento result. Promotion converts only that section and atomically replaces its old contiguous Bento range with the new N-slide range, so slide count and every section-local ID may change without leaving stale slides. It rejects collisions and external dangling references, preserves planning order, and leaves unrelated hashes unchanged. The promoted HTML becomes a historical snapshot; later Work editor changes stay Bento-canonical and are not synchronized back. `finish-current-section` binds acceptance to the section slides plus their referenced registry/provenance closure and opens the next section. Accepted sections can be reopened through Bento or, for a deliberate redesign, through a fresh HTML candidate. After all sections are accepted, whole-deck `content_review` is mandatory on every low-level and high-level approval route.

A content/structure request made in finalization or after completion reopens the affected authoring section. It invalidates whole-deck/final approval and requires section acceptance plus whole-deck approval again. After that approval, finalization restarts by archiving the complete previous final/baseline set and transactionally installing the newly approved authoring set. It never enables content edits in final mode or silently discards an existing final.

Natural conversation is routed internally to the high-level operations below. `advance` performs safe mechanical work only and stops at human approval checkpoints. Whole-deck proposal commands are agent-facing mechanics; users see the proposed change and impact in ordinary language.

```text
advance / approve-current / promote-current-section / edit-current
finish-current-section / reopen-current-section / review-whole-deck
capture-request / route / status [--json]
propose-html-change / approve-html-change / apply-html-change / check-html-change / cancel-html-change
```

## State commands

```text
status [--json]                 consistent status; refresh stale content approval
route [--json]                  deterministic primary-workspace route
capture-request --text ...      persist the conversational brief in REQUEST.md
write-planning-artifact         transactionally update one known planning file
advance                         move to the next human checkpoint, never approve
approve-current                 approve only the displayed plan/HTML/content checkpoint
adopt-whole-deck                adopt an existing complete HTML deck without rewriting it
complete-html-deck              validate the complete deck and open one HTML review
approve-html-deck               bind one approval to all current section digests
propose-html-change             snapshot and analyze a candidate; canonical stays unchanged
approve-html-change             confirm the exact current proposal and impact
apply-html-change               transactionally install only the approved candidate
check-html-change               browser-check installed affected slides and bind the evidence
cancel-html-change              close a proposal without changing canonical HTML
promote-current-section         section-only conversion and authoring transaction
promote-section --section ...   explicit-ID compatibility form of the same transaction
edit-current                    resolve the current editable workspace
finish-current-section          accept the current Bento section revision
reopen-current-section          resume an accepted section via Bento or HTML
review-whole-deck               mandatory review after every section is accepted
validate                        schema, path, stage, source, and artifact invariants
migrate [--dry-run]             idempotent schema v1 -> v2 migration
set-project --kind ... --title  schema v2 early-stage project metadata only
discover-sources [--json]       resolve manifest/PDF candidates
initialize                      initialized -> planning
configure-sections ...          register single-file planned sections
configure-chapters ...          legacy migrated modular equivalent
submit-plan                     planning -> awaiting_plan_approval
approve-plan                    -> html_authoring
begin-section                   choose the first incomplete section
complete-section                validate source -> html_review
approve-section                 store digest; choose next or become ready
unlock-section                  invalidate one approved section deliberately
prepare-conversion              ready_for_conversion -> converting
mark-converted                  validate generated; initialize/retain authoring
begin-authoring                 bento_validation -> bento_authoring
begin-content-review            validate authoring -> content_review
approve-content                 bind approval to both current revisions
reset-authoring-from-html       explicit full reset; authoring stage only
begin-finalization              approved content -> final artifacts/baselines
restart-finalization-from-authoring
                                archive an older final, then install newly approved authoring
approve-final                   final technical check + human approval
complete                        bento_finalization -> complete
reopen-finalization             invalidate final approval and resume presentation edits
block / resume                  preserve and revalidate the complete prior tuple
```

State writes are atomic. Artifact-changing state transitions use the durable multi-artifact transaction layer where required. All repository-relative paths are traversal checked, generated/authoring/final paths are distinct, and sidecar paths must match their HTML names.

`set-project` is an agent-facing setup command, not an additional user short phrase. It is limited to schema v2 `initialized`/`planning`, changes only `project.kind` and `project.title`, and leaves stage and approvals unchanged. Blocked workflows must use `resume` first. The kind must match `^[a-z][a-z0-9_-]*$`; the title must be a non-empty single line.

Planning inputs and planning state share one cross-process writer contract. `capture-request`, `set-project`, `initialize`, `configure-sections`, `configure-chapters`, `write-planning-artifact`, `submit-plan`, and `approve-plan` acquire the same review-bound `WriterLease` before refreshing state or committing. Agents must update `explanation-policy`, `story-outline`, `slide-plan`, and `visual-plan` through `write-planning-artifact --artifact ... --from-file ...` (or the corresponding workflow function), never by writing the canonical targets directly. The command accepts only those fixed targets, validates UTF-8 and visual-plan structure, and commits through the common artifact transaction layer. A concurrent writer receives a conflict and cannot be overwritten by a submit or approval transition.

The App's optional AI Planning Proposal route is limited to schema v2 `single`/`imported` projects in `planning`. One isolated job produces a complete candidate of all four planning artifacts plus ordered sections and stable slide IDs. Generation stores only git-ignored `.bento-ai/runs/<job>/` candidate and proposal metadata; it never changes canonical planning, workflow state, submits, approves, or generates HTML. Current and Candidate remain display modes. Apply requires an explicit process-local action token and revalidates the bound planning/request/project/source context, candidate signature, proposal digest, and proposal revision. It then delegates to `command_apply_planning_proposal`, which acquires the shared planning `WriterLease` and atomically commits `deck.yaml`, all four planning artifacts, section membership, work log, and applied proposal marker. Stale/tampered candidates and concurrent writers are conflicts; any ordinary transaction failure rolls every target back. Apply deliberately leaves the stage at `planning`, so submit and approval remain separate human checkpoints.

## Standard single-HTML route

`authoring.mode: single` uses `deck/deck.preview.html` and `deck/deck.registry.json`. New projects pair it with `authoring.strategy: whole_deck`; states created before the strategy field remain `rolling_sections` for backward compatibility. Each planned section is a stable grouping of slide IDs. Its approval digest includes canonical section DOM, referenced registry projection, referenced asset bytes, and global CSS/theme. A changed section/registry/asset invalidates that section; changed global CSS/theme invalidates every section. Conversion rechecks all digests.

Planning may include the internal `planning/visual-plan.yaml` contract. For every slide, Work decides whether prose is sufficient, a diagram improves understanding, the original source figure is required, an editable native diagram can express it, or a generated image is justified. Native text/shape/connector diagrams are preferred; a source-derived native diagram uses one assetless figure and carries its `figureId` on every component. Source/generated image registration and PDF cropping use `scripts.register_visual_asset`, which commits the local asset, SHA-256 content digest, and registry together. Visual origin metadata and transitive figure-to-asset/source dependencies participate in section digests; unrelated visual definitions do not. Never generate data, experimental/measurement/benchmark results, quantitative plots, or equations. See `docs/visual-workflow.md`.

`authoring.mode: modular` is supported for migrated v1 chapter projects. It retains the chapter approval commands and files; migration alone never changes a later stage into Bento authoring.

## Bento authoring and content approval

`mark-converted` validates generated output and initializes or retains the three authoring artifacts. `begin-authoring` hands them to Work. Authoring mode may change content and structure, with registry changes in the same save. It treats existing ID/type changes as explicit replace operations.

Every save checks both base revisions and validates HTML/JSON/registry, cross references, protected metadata, resources, and runtime before a three-artifact commit. A registry body can be omitted only when the supplied current registry revision is still current and the proposed document validates against that registry. Registry-requiring document changes without the corresponding definitions are rejected.

Authoring may temporarily save provenance drafts. Content review rejects equations without `equationId`, charts without `chartId`, tables without `tableId`, source-backed image/SVG elements without `figureId` or `assetId`, origin-bearing images whose embedded bytes do not match the registry `contentDigest`, and any element marked `unprovenancedDraft`. Referenced IDs must resolve in the current registry. Ordinary Work editor saves cannot rewrite or relabel `source-original` identity; registered source/segment tooling is required.

Content approval stores current document revision, registry revision, time, and:

```text
sha256(UTF-8("bento/content-approval/v1\0" + documentRevision + "\0" + registryRevision))
```

Current revisions are recomputed on save, status, review, approval, final handoff, segment operations, offline transactions, and migrated-state validation. A mismatch makes the approval pending. Finalization refuses a stale approval.

`begin-finalization` creates final HTML, JSON, final registry, baseline document, baseline registry, and updated state in one transaction. Existing mismatching final artifacts are not overwritten by this ordinary route. After a final/complete deck is reopened for content work and the revised authoring set receives fresh whole-deck approval, `restart-finalization-from-authoring --confirm ARCHIVE-AND-RESTART-FINALIZATION` archives the complete old final HTML/JSON/registry, both baselines, workflow snapshot, and a revision manifest under `revisions/final-restarts/restart-NNNNNN/`, then installs the new final/baselines and pending final approval in one transaction. The final editor must be stopped so the union writer lease can be acquired. The conversational approved-content route performs this archival restart when it detects an older complete final; generated remains unchanged. Final mode freezes content, structure, IDs/types, data, references, and registry; only geometry, presentation style, theme/background, and z-order may change.

Stop the final Work editor before `approve-final`; its lifetime writer lease deliberately prevents approval from racing a save. Final approval binds the document revision, final HTML byte revision, final registry revision, and runtime fingerprint. `complete` recomputes all four and refuses stale approval. Editing a completed or already-approved deck requires `reopen-finalization`, which validates the current bundle, returns to `bento_finalization`, and clears the old approval before any write.

## Legacy compatibility aliases

The former fixed Japanese phrases remain accepted aliases, but they are not the primary workflow description or required user syntax. Their exact checkpoint mapping lives in `docs/legacy-command-aliases.md`. Natural requests must be routed by intent through the high-level operations above.

## Segment and import routes

During `bento_authoring`, segment operations support append/import, insert before/after an anchor, single-slide replacement, contiguous range replacement, and section replacement. Targets remain explicit internally but are inferred from planning/current state for conversational use. Every operation protects outside slide hashes and cross-slide/registry references; generated/final remain unchanged. A running matching editor becomes the sole writer via localhost API. Otherwise the CLI must acquire the same OS lease.

`scripts.import_html_deck` accepts only an original under `imports/`, never executes its scripts, blocks network, sanitizes active content, produces normalized static single HTML/registry, and updates source manifest/state transactionally. Ambiguous slide selection requires `--slide-selector`.

## Recovery, blocking, and migration

Before normal read/write service, unfinished journals are recovered. Full new revisions finish commit; partial replacement rolls the entire set back; all-old targets finish rollback; missing recovery evidence stops without modifying artifacts. Report-only failures keep the committed artifacts and retry the report later.

`block` stores the full prior workflow tuple. `resume` validates that tuple's required files before restoration. Users never edit YAML for recovery.

Schema v1 migration is idempotent and stage-preserving. For `bento_finalization`/`complete`, it verifies the existing final pair, baseline, and merged registry, transactionally snapshots final/baseline registries, and leaves final/revision data untouched. Such late migrations may have null authoring paths only under `migration.lateStageCompatibility`; new v2 decks may not.

Never trigger conversion from a launcher, rebuild into final, or reset protected artifacts without explicit authorization and the dedicated confirmed command.
