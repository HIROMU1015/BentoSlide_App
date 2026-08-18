from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from bento_converter.artifact_transaction import ArtifactLeaseConflict, WriterLease
from bento_converter.errors import ValidationError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, serialize_bento_doc
from bento_converter.work_editor_storage import WorkEditorStorage
from scripts.deck_workflow import (
    WorkflowError,
    _normalize_handoff,
    atomic_write_state,
    command_approve_chapter,
    command_approve_content,
    command_approve_final,
    command_approve_plan,
    command_begin_authoring,
    command_begin_chapter,
    command_begin_content_review,
    command_begin_finalization,
    command_block,
    command_complete,
    command_complete_chapter,
    command_configure_chapters,
    command_initialize,
    command_mark_converted,
    command_migrate,
    command_prepare_conversion,
    command_reset_authoring_from_html,
    command_restart_finalization_from_authoring,
    command_reopen_finalization,
    command_resume,
    command_set_project,
    command_submit_plan,
    authoring_storage,
    discover_source_candidates,
    load_state,
    load_final_baseline,
    parser,
    run,
    validate_chapters,
    validate_output_bundle,
    validate_state,
)
from scripts.bento_segment import run as run_segment


ROOT = Path(__file__).resolve().parents[1]


class ProjectMetadataCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 workflow demo"
        for directory in ("workflow", "sources/private", "planning"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "REQUEST.md", "workflow/deck.schema.json",
            "workflow/deck.v1.schema.json", "planning/work-log.md",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        (self.root / "sources/private/spec.md").write_text("# Fixture source\n\nStable test evidence.\n", encoding="utf-8")
        (self.root / "sources/source-manifest.yaml").write_text(
            yaml.safe_dump({
                "schemaVersion": 1,
                "authorityMode": "single",
                "items": [{
                    "id": "fixture-source",
                    "path": "sources/private/spec.md",
                    "type": "text/markdown",
                    "role": "primary",
                }],
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self) -> dict:
        return load_state(self.root)

    def set_stage(self, stage: str, status: str, source: str) -> None:
        state = self.state()
        state["workflow"].update(stage=stage, status=status, owner="work", sourceOfTruth=source)
        _normalize_handoff(state)
        atomic_write_state(self.root, state)

    def test_set_project_updates_only_metadata_in_initialized(self) -> None:
        before = self.state()
        expected = copy.deepcopy(before)
        expected["project"].update(kind="workflow_explainer", title="BentoSlideができるまで")
        real_replace = os.replace
        with mock.patch("scripts.deck_workflow.os.replace", wraps=real_replace) as replace:
            command_set_project(
                self.root, before,
                kind="workflow_explainer", title="BentoSlideができるまで",
            )
        deck_replaces = [
            call for call in replace.call_args_list
            if Path(call.args[1]).resolve() == (self.root / "deck.yaml").resolve()
        ]
        self.assertEqual(len(deck_replaces), 1)
        self.assertEqual(self.state(), expected)
        self.assertFalse(list(self.root.glob(".deck.*.yaml.tmp")))
        self.assertIn("workflow_explainer", (self.root / "planning/work-log.md").read_text(encoding="utf-8"))

    def test_set_project_is_allowed_in_planning_without_changing_approvals(self) -> None:
        self.set_stage("planning", "in_progress", "planning")
        before = self.state()
        command_set_project(self.root, before, kind="project_demo", title="Project Demo")
        after = self.state()
        self.assertEqual(after["workflow"], before["workflow"])
        self.assertEqual(after["approvals"], before["approvals"])
        self.assertEqual(after["project"]["kind"], "project_demo")

    def test_set_project_rejects_invalid_metadata_without_writing(self) -> None:
        invalid = (
            ("", "Title"), ("Workflow", "Title"), ("1workflow", "Title"),
            ("workflow demo", "Title"), ("workflow/demo", "Title"),
            ("workflow_explainer", ""), ("workflow_explainer", "   "),
            ("workflow_explainer", " Outer"), ("workflow_explainer", "Two\nLines"),
        )
        for kind, title in invalid:
            with self.subTest(kind=kind, title=title):
                before = (self.root / "deck.yaml").read_bytes()
                with self.assertRaises(WorkflowError):
                    command_set_project(self.root, self.state(), kind=kind, title=title)
                self.assertEqual((self.root / "deck.yaml").read_bytes(), before)

    def test_set_project_rejects_late_blocked_and_schema_v1_states(self) -> None:
        self.set_stage("awaiting_plan_approval", "awaiting_approval", "planning")
        with self.assertRaisesRegex(WorkflowError, "does not allow"):
            command_set_project(self.root, self.state(), kind="workflow_explainer", title="Title")

        self.set_stage("bento_authoring", "in_progress", "authoring")
        with self.assertRaisesRegex(WorkflowError, "does not allow"):
            command_set_project(self.root, self.state(), kind="workflow_explainer", title="Title")

        self.set_stage("initialized", "pending", "sources")
        command_block(self.root, self.state(), "work", "Resolve metadata source")
        with self.assertRaisesRegex(WorkflowError, "does not allow"):
            command_set_project(self.root, self.state(), kind="workflow_explainer", title="Title")

        shutil.copy2(ROOT / "tests/fixtures/deck_v1.yaml", self.root / "deck.yaml")
        before = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "schema v2"):
            command_set_project(self.root, self.state(), kind="workflow_explainer", title="Title")
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)

    def test_set_project_cli_dispatch(self) -> None:
        args = parser().parse_args([
            "--root", str(self.root), "set-project",
            "--kind", "workflow_explainer", "--title", "BentoSlideができるまで",
        ])
        self.assertEqual(run(args), 0)
        project = self.state()["project"]
        self.assertEqual(project["kind"], "workflow_explainer")
        self.assertEqual(project["title"], "BentoSlideができるまで")


class DeckWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "planning", "chapters", "output/diagnostics"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "tests/fixtures/deck_v1.yaml", self.root / "deck.yaml")
        for relative in (
            "REQUEST.md", "workflow/deck.schema.json", "workflow/deck.v1.schema.json",
            "planning/decisions.md", "planning/work-log.md",
        ):
            shutil.copy2(ROOT / relative, self.root / relative)
        for relative, heading in (
            ("planning/explanation-policy.md", "Explanation policy"),
            ("planning/story-outline.md", "Story outline"),
            ("planning/slide-plan.md", "Slide plan"),
        ):
            (self.root / relative).write_text(
                f"# {heading}\n\n<!-- Fixture intentionally starts without substantive content. -->\n",
                encoding="utf-8",
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self) -> dict:
        return load_state(self.root)

    def write_state(self, state: dict) -> None:
        atomic_write_state(self.root, state)

    def add_source(self, name: str = "論文.pdf") -> Path:
        path = self.root / "sources/private" / name
        path.write_bytes(b"%PDF-1.4\nfixture\n")
        return path

    def fill_plan(self) -> None:
        (self.root / "planning/explanation-policy.md").write_text("# Policy\n\nExplain the verified contribution.\n", encoding="utf-8")
        (self.root / "planning/story-outline.md").write_text("# Story\n\nProblem to evidence.\n", encoding="utf-8")
        (self.root / "planning/slide-plan.md").write_text("# Plan\n\nTwo approved chapters.\n", encoding="utf-8")

    def add_chapter(self, chapter_id: str, *, slide_id: str | None = None, duplicate_element: bool = False, latex: str = "E=mc^2") -> None:
        slide = slide_id or f"{chapter_id}-slide"
        duplicate = '<p data-bento-id="title">duplicate</p>' if duplicate_element else ""
        html = f"""<!doctype html><html><body><section class="slide" data-slide-id="{slide}">
<h1 data-bento-id="title">Title</h1>{duplicate}
<div data-bento-id="equation" data-equation-id="energy" data-latex="{latex}">E = mc2</div>
</section></body></html>"""
        registry = {
            "format": "bento/html-registry/v1", "chapterId": chapter_id,
            "equations": {"energy": {"latex": "E=mc^2", "usedOnSlides": [slide]}},
            "protected": {"slideIds": [slide], "elementIds": ["title"], "requiredText": []},
        }
        (self.root / f"chapters/{chapter_id}.preview.html").write_text(html, encoding="utf-8")
        (self.root / f"chapters/{chapter_id}.registry.json").write_text(json.dumps(registry), encoding="utf-8")

    def plan_to_authoring(self, chapters: tuple[str, ...] = ("chapter-01",)) -> dict:
        self.add_source()
        state = self.state()
        command_initialize(self.root, state)
        self.fill_plan()
        state = self.state()
        command_configure_chapters(self.root, state, chapters)
        state = self.state()
        command_submit_plan(self.root, state)
        state = self.state()
        command_approve_plan(self.root, state)
        return self.state()

    def author_and_approve(self, state: dict, chapter_id: str) -> dict:
        command_begin_chapter(self.root, state, chapter_id)
        self.add_chapter(chapter_id)
        state = self.state()
        command_complete_chapter(self.root, state, chapter_id)
        state = self.state()
        command_approve_chapter(self.root, state, chapter_id)
        return self.state()

    def prepare_output_bundle(self, state: dict, *, existing_final: bool) -> tuple[str, str | None]:
        output = self.root / "output"
        generated_html = output / "presentation.generated.bento.html"
        generated_json = output / "presentation.generated.bento.json"
        source_html = load_html(ROOT / "demo.bento.html")
        source_doc = extract_bento_doc(source_html)
        generated_html.write_text(source_html, encoding="utf-8")
        generated_json.write_text(serialize_bento_doc(source_doc) + "\n", encoding="utf-8")
        (output / "conversion-report.json").write_text(json.dumps({"summary": {"criticalElementFail": 0, "unresolvedLocalResourceReferences": 0}}), encoding="utf-8")
        diagnostics = output / "diagnostics"
        (diagnostics / "computed-layout.json").write_text("{}\n", encoding="utf-8")
        (diagnostics / "merged-registry.json").write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
            "document": {}, "assets": {}, "fonts": {},
            "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        (diagnostics / "resource-scan.json").write_text(json.dumps({"passed": True, "unresolved": []}), encoding="utf-8")
        (diagnostics / "browser-check.json").write_text(json.dumps({"serialize_roundtrip": True}), encoding="utf-8")
        (diagnostics / "browser-environment.json").write_text(json.dumps({
            "format": "bento/browser-environment/v1",
            "environmentDigest": "sha256:" + "0" * 64,
            "browserEnvironment": {"profiles": {"sourceLayout": {}, "bentoCheck": {}}},
        }), encoding="utf-8")
        generated_hash = hashlib.sha256(generated_html.read_bytes()).hexdigest()
        final_hash = None
        if existing_final:
            final_doc = copy.deepcopy(source_doc)
            next(element for slide in final_doc["slides"] for element in slide["elements"] if element["type"] == "shape")["x"] += 7
            final_html = embed_bento_doc(source_html, final_doc)
            (output / "presentation.final.bento.html").write_text(final_html, encoding="utf-8")
            (output / "presentation.final.bento.json").write_text(serialize_bento_doc(final_doc) + "\n", encoding="utf-8")
            final_hash = hashlib.sha256((output / "presentation.final.bento.html").read_bytes()).hexdigest()
        return generated_hash, final_hash

    def ready_for_conversion(self) -> dict:
        state = self.plan_to_authoring()
        state = self.author_and_approve(state, "chapter-01")
        self.assertEqual(state["workflow"]["stage"], "ready_for_conversion")
        command_prepare_conversion(self.root, state)
        return self.state()

    def test_schema_rejects_wrong_stage_owner_and_path_escape(self) -> None:
        previous_template = self.state()
        previous_template["workflow"].pop("blockedFrom")
        previous_template["validation"].pop("finalBaseline")
        validate_state(self.root, previous_template)
        state = self.state()
        state["workflow"]["owner"] = "codex"
        with self.assertRaisesRegex(WorkflowError, "owner"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["generatedHtml"] = "../outside.bento.html"
        with self.assertRaisesRegex(WorkflowError, "escapes"):
            validate_state(self.root, state)
        state = self.state()
        state["preview"]["currentUrl"] = "http://127.0.0.1:70000/"
        with self.assertRaisesRegex(WorkflowError, "invalid port"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["finalHtml"] = state["outputs"]["generatedHtml"]
        with self.assertRaisesRegex(WorkflowError, "must be distinct"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["generatedJson"] = "output/not-the-generated-sidecar.json"
        with self.assertRaisesRegex(WorkflowError, "sidecar path"):
            validate_state(self.root, state)
        state = yaml.safe_load(
            (ROOT / "tests/fixtures/deck_v2.initialized.yaml").read_text(encoding="utf-8")
        )
        state["handoff"]["readyForBentoAuthoring"] = True
        with self.assertRaisesRegex(WorkflowError, "handoff flags do not match"):
            validate_state(self.root, state)

    def test_blocked_state_records_machine_readable_reason(self) -> None:
        command_block(self.root, self.state(), "work", "Primary source decision is required")
        blocked = self.state()
        self.assertEqual(blocked["workflow"]["stage"], "blocked")
        self.assertEqual(blocked["workflow"]["owner"], "work")
        self.assertEqual(blocked["workflow"]["blockingReason"], "Primary source decision is required")
        self.assertEqual(blocked["workflow"]["blockedFrom"]["stage"], "initialized")
        command_resume(self.root, blocked)
        resumed = self.state()
        self.assertEqual(resumed["workflow"]["stage"], "initialized")
        self.assertIsNone(resumed["workflow"]["blockedFrom"])

    def test_resume_revalidates_files_before_restoring_html_review(self) -> None:
        state = self.plan_to_authoring()
        command_begin_chapter(self.root, state, "chapter-01")
        self.add_chapter("chapter-01")
        command_complete_chapter(self.root, self.state(), "chapter-01")
        command_block(self.root, self.state(), "work", "Review source needs repair")
        registry = self.root / "chapters/chapter-01.registry.json"
        registry.unlink()
        with self.assertRaisesRegex(WorkflowError, "registry does not exist"):
            command_resume(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "blocked")
        self.add_chapter("chapter-01")
        command_resume(self.root, self.state())
        resumed = self.state()
        self.assertEqual(resumed["workflow"]["stage"], "html_review")
        self.assertEqual(resumed["workflow"]["currentChapter"], "chapter-01")

    def test_atomic_write_uses_replace_and_roundtrips_unicode(self) -> None:
        state = self.state()
        state["project"]["title"] = "量子スライド"
        real_replace = os.replace
        with mock.patch("scripts.deck_workflow.os.replace", wraps=real_replace) as replace:
            atomic_write_state(self.root, state)
        replace.assert_called_once()
        self.assertEqual(self.state()["project"]["title"], "量子スライド")
        self.assertFalse(list(self.root.glob(".deck.*.yaml.tmp")))

    def test_primary_source_discovery_single_japanese_pdf(self) -> None:
        source = self.add_source("日本語 論文.pdf")
        selected, candidates = discover_source_candidates(self.root, self.state())
        self.assertEqual(selected, source.resolve())
        self.assertEqual(candidates, [source.resolve()])
        state = self.state()
        command_initialize(self.root, state)
        updated = self.state()
        self.assertEqual(updated["project"]["primarySource"], "sources/private/日本語 論文.pdf")
        self.assertEqual(updated["workflow"]["stage"], "planning")

    def test_primary_source_absent_and_multiple_require_decision(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "No primary PDF"):
            discover_source_candidates(self.root, self.state())
        self.add_source("a.pdf")
        self.add_source("b.pdf")
        with self.assertRaisesRegex(WorkflowError, "Multiple PDF"):
            discover_source_candidates(self.root, self.state())
        state = self.state()
        state["project"]["primarySource"] = "sources/private/b.pdf"
        self.write_state(state)
        selected, _ = discover_source_candidates(self.root, self.state())
        self.assertEqual(selected.name, "b.pdf")

    def test_plan_requires_substantive_files_chapters_and_approval(self) -> None:
        self.add_source()
        state = self.state()
        command_initialize(self.root, state)
        with self.assertRaisesRegex(WorkflowError, "substantive"):
            command_submit_plan(self.root, self.state())
        self.fill_plan()
        with self.assertRaisesRegex(WorkflowError, "Register"):
            command_submit_plan(self.root, self.state())
        command_configure_chapters(self.root, self.state(), ("chapter-01", "chapter-02"))
        command_submit_plan(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "awaiting_plan_approval")
        command_approve_plan(self.root, self.state())
        updated = self.state()
        self.assertEqual(updated["workflow"]["stage"], "html_authoring")
        self.assertTrue(all(updated["approvals"][key] == "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")))

    def test_complete_chapter_requires_registry_and_rejects_duplicate_ids(self) -> None:
        state = self.plan_to_authoring()
        command_begin_chapter(self.root, state, "chapter-01")
        html = self.root / "chapters/chapter-01.preview.html"
        html.write_text('<section data-slide-id="slide"><h1 data-bento-id="title">T</h1></section>', encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "registry does not exist"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01", duplicate_element=True)
        with self.assertRaisesRegex(WorkflowError, "Duplicate element IDs"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01")
        registry_path = self.root / "chapters/chapter-01.registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["protected"]["requiredText"] = ["text that is absent"]
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "required text"):
            command_complete_chapter(self.root, self.state(), "chapter-01")

    def test_equation_registry_mismatch_and_cross_chapter_slide_duplicates(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        command_begin_chapter(self.root, state, "chapter-01")
        self.add_chapter("chapter-01", latex="wrong")
        with self.assertRaisesRegex(WorkflowError, "data-latex"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01", slide_id="shared")
        self.add_chapter("chapter-02", slide_id="shared")
        state = self.state()
        state["chapters"]["chapter-01"].update(status="complete", visualApproval="approved")
        state["chapters"]["chapter-02"].update(status="complete", visualApproval="approved")
        self.write_state(state)
        with self.assertRaisesRegex(WorkflowError, "across chapters"):
            validate_chapters(self.root, self.state(), require_complete=True)

    def test_visual_approval_gate_selects_next_then_ready(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        state = self.author_and_approve(state, "chapter-01")
        self.assertEqual(state["workflow"]["stage"], "html_authoring")
        self.assertEqual(state["workflow"]["currentChapter"], "chapter-02")
        self.assertEqual(state["chapters"]["chapter-02"]["status"], "authoring")
        self.add_chapter("chapter-02")
        command_complete_chapter(self.root, state, "chapter-02")
        command_approve_chapter(self.root, self.state(), "chapter-02")
        ready = self.state()
        self.assertEqual(ready["workflow"]["stage"], "ready_for_conversion")
        self.assertTrue(ready["handoff"]["readyForCodex"])

    def test_prepare_conversion_refuses_incomplete_chapter(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        state["workflow"].update(stage="ready_for_conversion", status="ready", owner="codex", sourceOfTruth="chapters", currentChapter=None)
        state["handoff"]["readyForCodex"] = True
        self.write_state(state)
        with self.assertRaisesRegex(WorkflowError, "not complete"):
            command_prepare_conversion(self.root, self.state())

    def test_mark_converted_initializes_missing_final_without_reset(self) -> None:
        state = self.ready_for_conversion()
        generated_hash, _ = self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        updated = self.state()
        self.assertEqual(updated["workflow"]["stage"], "bento_validation")
        self.assertTrue((self.root / "output/presentation.final.bento.html").is_file())
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.generated.bento.html").read_bytes()).hexdigest(), generated_hash)
        baseline = updated["validation"]["finalBaseline"]
        self.assertIsNotNone(baseline)
        self.assertTrue((self.root / baseline["path"]).is_file())
        self.assertRegex(baseline["protectedContentFingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_mark_converted_requires_versioned_browser_environment_evidence(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        environment = self.root / "output/diagnostics/browser-environment.json"
        environment.unlink()
        with self.assertRaisesRegex(WorkflowError, "browser-environment"):
            command_mark_converted(self.root, self.state())
        environment.write_text(json.dumps({
            "format": "bento/browser-environment/v1",
            "environmentDigest": "not-a-digest",
            "browserEnvironment": {"profiles": {"sourceLayout": {}, "bentoCheck": {}}},
        }), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "environment evidence"):
            command_mark_converted(self.root, self.state())

    def test_mark_converted_preserves_existing_final(self) -> None:
        state = self.ready_for_conversion()
        generated_hash, final_hash = self.prepare_output_bundle(state, existing_final=True)
        command_mark_converted(self.root, self.state())
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.generated.bento.html").read_bytes()).hexdigest(), generated_hash)
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.final.bento.html").read_bytes()).hexdigest(), final_hash)

    def test_v2_mark_converted_initializes_authoring_then_explicitly_hands_off(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        validated = self.state()
        self.assertEqual(validated["workflow"]["stage"], "bento_validation")
        self.assertTrue(validated["handoff"]["readyForBentoAuthoring"])
        self.assertFalse((self.root / validated["outputs"]["finalHtml"]).exists())
        for field in ("authoringHtml", "authoringJson", "authoringRegistry"):
            self.assertTrue((self.root / validated["outputs"][field]).is_file())
        command_begin_authoring(self.root, validated)
        authoring = self.state()
        self.assertEqual(authoring["workflow"]["stage"], "bento_authoring")
        self.assertEqual(authoring["workflow"]["sourceOfTruth"], "authoring")
        self.assertTrue(authoring["handoff"]["readyForBentoAuthoring"])

    def test_v2_content_approval_invalidation_and_final_transaction(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        command_begin_content_review(self.root, self.state())
        command_approve_content(self.root, self.state())
        approved = self.state()["approvals"]["bentoContent"]
        self.assertEqual(approved["status"], "approved")
        self.assertRegex(approved["approvalDigest"], r"^sha256:[0-9a-f]{64}$")

        storage = authoring_storage(self.root, self.state())
        status = storage.status()
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        next(element for slide in document["slides"] for element in slide["elements"] if element["id"] == "slide-1-title")["html"] = "Approved content changed"
        storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        invalidated = self.state()
        self.assertEqual(invalidated["approvals"]["bentoContent"]["status"], "pending")
        with self.assertRaisesRegex(WorkflowError, "fresh content approval"):
            command_begin_finalization(self.root, invalidated)

        command_approve_content(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        finalized = self.state()
        self.assertEqual(finalized["workflow"]["stage"], "bento_finalization")
        self.assertTrue(finalized["handoff"]["readyForFinalEditing"])
        baseline = finalized["validation"]["finalBaseline"]
        for field in ("finalHtml", "finalJson", "finalRegistry"):
            self.assertTrue((self.root / finalized["outputs"][field]).is_file())
        self.assertTrue((self.root / baseline["documentPath"]).is_file())
        self.assertTrue((self.root / baseline["registryPath"]).is_file())
        self.assertEqual(
            json.loads((self.root / finalized["outputs"]["finalRegistry"]).read_text(encoding="utf-8")),
            json.loads((self.root / baseline["registryPath"]).read_text(encoding="utf-8")),
        )
        journals = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "output/.bento-transactions/archive").rglob("*.json")
        ]
        final_journal = next(item for item in journals if item["operation"] == "authoring-to-final-initialize")
        self.assertEqual(len(final_journal["artifacts"]), 6)

    def test_content_revision_archives_complete_old_final_and_restarts_transactionally(self) -> None:
        parsed = parser().parse_args([
            "--root", str(self.root), "restart-finalization-from-authoring",
            "--confirm", "ARCHIVE-AND-RESTART-FINALIZATION",
        ])
        self.assertEqual(parsed.command, "restart-finalization-from-authoring")
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        command_begin_content_review(self.root, self.state())
        command_approve_content(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        finalized = self.state()
        final_paths = {
            field: self.root / finalized["outputs"][field]
            for field in ("finalHtml", "finalJson", "finalRegistry")
        }
        baseline = finalized["validation"]["finalBaseline"]
        old_payloads = {
            **{field: path.read_bytes() for field, path in final_paths.items()},
            "baselineDocument": (self.root / baseline["documentPath"]).read_bytes(),
            "baselineRegistry": (self.root / baseline["registryPath"]).read_bytes(),
        }
        generated_before = {
            field: (self.root / finalized["outputs"][field]).read_bytes()
            for field in ("generatedHtml", "generatedJson", "generatedRegistry")
        }

        revised = copy.deepcopy(finalized)
        revised["approvals"]["bentoContent"] = {
            "status": "pending", "documentRevision": None, "registryRevision": None,
            "approvalDigest": None, "approvedAt": None,
        }
        revised["approvals"]["finalBento"] = {
            "status": "pending", "documentRevision": None, "htmlRevision": None,
            "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
        }
        revised["validation"].update(finalStatus="pending", checkedAt=None)
        revised["workflow"].update(
            stage="bento_authoring", status="in_progress", owner="work",
            sourceOfTruth="authoring", currentChapter=None, currentSection=None,
        )
        _normalize_handoff(revised)
        self.write_state(revised)
        storage = authoring_storage(self.root, self.state())
        status = storage.status()
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        next(
            element for slide in document["slides"] for element in slide["elements"]
            if element["id"] == "slide-1-title"
        )["html"] = "Revised approved content"
        storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        command_begin_content_review(self.root, self.state())
        command_approve_content(self.root, self.state())

        with self.assertRaisesRegex(WorkflowError, "ARCHIVE-AND-RESTART"):
            command_restart_finalization_from_authoring(
                self.root, self.state(), confirmation="wrong",
            )
        with self.assertRaisesRegex(WorkflowError, "restart-finalization-from-authoring"):
            command_begin_finalization(self.root, self.state())
        for field, path in final_paths.items():
            self.assertEqual(path.read_bytes(), old_payloads[field])

        editor_lease = WriterLease(self.root, final_paths.values())
        editor_lease.acquire()
        try:
            with self.assertRaises(ArtifactLeaseConflict):
                command_restart_finalization_from_authoring(
                    self.root, self.state(), confirmation="ARCHIVE-AND-RESTART-FINALIZATION",
                )
        finally:
            editor_lease.release()
        for field, path in final_paths.items():
            self.assertEqual(path.read_bytes(), old_payloads[field])

        command_restart_finalization_from_authoring(
            self.root, self.state(), confirmation="ARCHIVE-AND-RESTART-FINALIZATION",
        )
        restarted = self.state()
        self.assertEqual(restarted["workflow"]["stage"], "bento_finalization")
        self.assertEqual(restarted["handoff"], {
            "readyForCodex": False, "readyForBentoAuthoring": False,
            "readyForContentReview": False, "readyForFinalEditing": True,
        })
        self.assertEqual(
            extract_bento_doc(load_html(final_paths["finalHtml"])), document,
        )
        for field, payload in generated_before.items():
            self.assertEqual((self.root / restarted["outputs"][field]).read_bytes(), payload)
        manifests = list((self.root / "output/revisions/final-restarts").glob("restart-*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        archive = manifests[0].parent
        self.assertEqual((archive / "final.bento.html").read_bytes(), old_payloads["finalHtml"])
        self.assertEqual((archive / "final.bento.json").read_bytes(), old_payloads["finalJson"])
        self.assertEqual((archive / "final.registry.json").read_bytes(), old_payloads["finalRegistry"])
        self.assertEqual((archive / "baseline.bento.json").read_bytes(), old_payloads["baselineDocument"])
        self.assertEqual((archive / "baseline.registry.json").read_bytes(), old_payloads["baselineRegistry"])
        self.assertEqual(manifest["format"], "bento/final-restart-archive/v1")
        journals = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "output/.bento-transactions/archive").rglob("*.json")
        ]
        self.assertIn("authoring-to-final-restart", {item["operation"] for item in journals})

    def test_v2_content_review_rejects_chart_without_registry_id(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        storage = authoring_storage(self.root, self.state())
        status = storage.status()
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        document["slides"][0]["elements"].append({
            "id": "unregistered-chart", "type": "chart",
            "x": 20, "y": 20, "w": 200, "h": 100, "preset": "bar", "option": {},
        })
        storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        with self.assertRaisesRegex(ValidationError, "chartId"):
            command_begin_content_review(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "bento_authoring")

        status = storage.status()
        html = storage.html_response()
        document = extract_bento_doc(html)
        chart = next(
            element for slide in document["slides"] for element in slide["elements"]
            if element["id"] == "unregistered-chart"
        )
        chart["chartId"] = "registered-chart"
        registry = json.loads(storage.registry_path.read_text(encoding="utf-8"))
        registry["charts"]["registered-chart"] = {"title": "Registered chart"}
        storage.save_serialized(
            embed_bento_doc(html, document), registry=registry,
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        command_begin_content_review(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "content_review")

    def test_explicit_full_reset_rebuilds_authoring_but_never_final(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        state = self.state()
        chapter_html = self.root / "chapters/chapter-01.preview.html"
        chapter_html.write_text(
            chapter_html.read_text(encoding="utf-8").replace(
                '<section class="slide" data-slide-id="chapter-01-slide">',
                '<section class="slide" data-slide-id="chapter-01-slide" style="width:1280px;height:720px">',
            ),
            encoding="utf-8",
        )
        final_html = self.root / state["outputs"]["finalHtml"]
        final_html.write_bytes(b"do-not-touch-final")
        with self.assertRaisesRegex(WorkflowError, "requires --confirm"):
            command_reset_authoring_from_html(
                self.root, state, confirmation="no",
                browser_executable=None, browser_check=False,
            )
        command_reset_authoring_from_html(
            self.root, self.state(), confirmation="RESET-AUTHORING-FROM-HTML",
            browser_executable=None, browser_check=False,
        )
        reset = self.state()
        authoring_document = extract_bento_doc(load_html(self.root / reset["outputs"]["authoringHtml"]))
        self.assertEqual([slide["id"] for slide in authoring_document["slides"]], ["chapter-01-slide"])
        self.assertEqual(final_html.read_bytes(), b"do-not-touch-final")
        self.assertEqual(reset["workflow"]["stage"], "bento_authoring")
        self.assertEqual(reset["approvals"]["bentoContent"]["status"], "pending")
        self.assertTrue(list((self.root / "output/revisions").glob("*.bento.html")))
        report = json.loads((self.root / "output/reset-authoring-report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["finalArtifactsChanged"])

    def test_segment_cli_imports_new_slide_without_changing_generated_or_final(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        state = self.state()
        generated = self.root / state["outputs"]["generatedHtml"]
        generated_before = generated.read_bytes()
        segment_dir = self.root / "scratch/segments"
        segment_dir.mkdir(parents=True)
        segment_html = segment_dir / "追加.preview.html"
        segment_html.write_text(
            '<!doctype html><html><body style="margin:0"><section class="slide" data-slide-id="added-slide" '
            'style="width:1280px;height:720px"><h1 data-bento-id="added-title">Added</h1>'
            '</section></body></html>', encoding="utf-8",
        )
        segment_registry = segment_dir / "追加.registry.json"
        segment_registry.write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "added", "sources": {},
            "document": {}, "assets": {}, "fonts": {}, "equations": {}, "figures": {},
            "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        result = run_segment(SimpleNamespace(
            root=self.root, command="import", html=segment_html, registry=segment_registry,
            browser_executable=None, skip_browser_check=True,
        ))
        self.assertEqual(result, 0)
        updated = self.state()
        authoring = extract_bento_doc(load_html(self.root / updated["outputs"]["authoringHtml"]))
        self.assertIn("added-slide", [slide["id"] for slide in authoring["slides"]])
        self.assertEqual(generated.read_bytes(), generated_before)
        self.assertFalse((self.root / updated["outputs"]["finalHtml"]).exists())
        reports = list((self.root / "output/segment-reports").glob("*/operation-report.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text(encoding="utf-8"))["operation"], "segment-import")

    def test_finalization_and_complete_require_explicit_final_approval(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        with self.assertRaisesRegex(WorkflowError, "approval"):
            command_complete(self.root, self.state())
        command_approve_final(self.root, self.state())
        command_complete(self.root, self.state())
        completed = self.state()
        self.assertEqual(completed["workflow"]["stage"], "complete")
        self.assertEqual(completed["validation"]["finalStatus"], "pass")
        self.assertFalse(completed["handoff"]["readyForFinalEditing"])

    def test_final_approval_is_revision_bound_and_reopen_clears_it(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        command_begin_content_review(self.root, self.state())
        command_approve_content(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        command_approve_final(self.root, self.state())
        approved = self.state()["approvals"]["finalBento"]
        self.assertEqual(approved["status"], "approved")
        for field in ("documentRevision", "htmlRevision", "registryRevision", "runtimeFingerprint"):
            self.assertRegex(approved[field], r"^sha256:[0-9a-f]{64}$")

        final_html_path = self.root / "output/presentation.final.bento.html"
        final_json_path = self.root / "output/presentation.final.bento.json"
        html = load_html(final_html_path)
        document = extract_bento_doc(html)
        shape = next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "shape")
        shape["x"] += 4
        final_html_path.write_bytes(embed_bento_doc(html, document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "stale"):
            command_complete(self.root, self.state())

        command_reopen_finalization(self.root, self.state())
        reopened = self.state()
        self.assertEqual(reopened["workflow"]["stage"], "bento_finalization")
        self.assertEqual(reopened["approvals"]["finalBento"], {
            "status": "pending", "documentRevision": None, "htmlRevision": None,
            "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
        })
        command_approve_final(self.root, reopened)
        command_complete(self.root, self.state())

    def test_final_approval_waits_for_the_editor_writer_lease(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_authoring(self.root, self.state())
        command_begin_content_review(self.root, self.state())
        command_approve_content(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        finalizing = self.state()
        bundle = validate_output_bundle(self.root, finalizing, require_final=True)
        baseline, _ = load_final_baseline(self.root, finalizing, bundle["generatedDocument"])
        outputs = finalizing["outputs"]
        editor = WorkEditorStorage(
            source=self.root / outputs["authoringHtml"],
            target=self.root / outputs["finalHtml"],
            registry=self.root / outputs["finalRegistry"],
            repository=self.root,
            hold_writer_lease=True,
            baseline_document=baseline,
            state_path=self.root / "deck.yaml",
        )
        try:
            with self.assertRaises(ArtifactLeaseConflict):
                command_approve_final(self.root, self.state())
        finally:
            editor.release_writer_lease()
        command_approve_final(self.root, self.state())

    def test_final_validation_rejects_content_replacement_but_allows_layout(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        final_html_path = self.root / "output/presentation.final.bento.html"
        final_json_path = self.root / "output/presentation.final.bento.json"
        final_html = load_html(final_html_path)
        document = extract_bento_doc(final_html)
        shape = next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "shape")
        shape["x"] += 3
        shape["fill"] = "#abcdef"
        final_html_path.write_bytes(embed_bento_doc(final_html, document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        command_approve_final(self.root, self.state())

        document = extract_bento_doc(load_html(final_html_path))
        text = next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "text")
        text["html"] = "externally replaced content"
        final_html_path.write_bytes(embed_bento_doc(load_html(final_html_path), document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "protected"):
            command_complete(self.root, self.state())

    def test_final_fingerprint_rejects_slide_structure_reordering(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        final_html_path = self.root / "output/presentation.final.bento.html"
        final_json_path = self.root / "output/presentation.final.bento.json"
        final_html = load_html(final_html_path)
        document = extract_bento_doc(final_html)
        document["slides"].reverse()
        final_html_path.write_bytes(embed_bento_doc(final_html, document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "content/structure"):
            command_approve_final(self.root, self.state())


if __name__ == "__main__":
    unittest.main()
