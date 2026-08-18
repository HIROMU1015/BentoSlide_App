from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from bento_converter.artifact_transaction import ArtifactTransactionStore, file_revision
from bento_converter.html_change import analyze_html_change
from bento_converter.html_change_review import (
    HtmlChangeBrowserEvidence,
    POST_APPLY_REPORT_FORMAT,
)
from bento_converter.section_approval import (
    compute_html_deck_structure_evidence,
    compute_section_approval_evidence,
)
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_adopt_whole_deck,
    command_apply_html_change,
    command_approve_current,
    command_approve_html_change,
    command_approve_html_deck,
    command_approve_plan,
    command_approve_section,
    command_begin_section,
    command_complete_section,
    command_complete_html_deck,
    command_configure_sections,
    command_cancel_html_change,
    command_check_html_change,
    command_propose_html_change,
    command_prepare_conversion,
    command_submit_plan,
    command_unlock_section,
    load_state,
    migrate_v1_state,
    parser as workflow_parser,
    validate_sections,
)


ROOT = Path(__file__).resolve().parents[1]


def registry() -> dict:
    return {
        "format": "bento/html-registry/v2",
        "unitId": "deck",
        "sources": {},
        "document": {"title": "日本語デッキ", "theme": "light"},
        "assets": {"plot": {"path": "assets/図 表.png"}},
        "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {"unused": {"data": [1]}},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }


def html(css: str = ".slide{width:1280px;height:720px}") -> str:
    return f'''<!doctype html><html data-theme="light"><head><style>{css}</style></head><body>
<main data-bento-deck>
  <section class="slide" data-slide-id="導入-1" data-section-id="introduction">
    <img data-bento-id="plot-image" data-asset-id="plot" src="assets/図 表.png">
  </section>
  <section class="slide" data-slide-id="method-1" data-section-id="method">
    <div data-bento-id="method-text">Method</div>
  </section>
</main></body></html>'''


class SectionDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 資料"
        (self.root / "deck/assets").mkdir(parents=True)
        self.html_path = self.root / "deck/deck.preview.html"
        self.html_path.write_text(html(), encoding="utf-8")
        (self.root / "deck/assets/図 表.png").write_bytes(b"png-one")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def evidence(self, value: dict | None = None):
        return compute_section_approval_evidence(self.html_path, value or registry(), repository=self.root)

    def test_digest_tracks_section_dom_registry_assets_and_global_css(self) -> None:
        initial = self.evidence()
        self.assertEqual(initial["introduction"].slide_ids, ("導入-1",))
        self.assertIn("deck/assets/図 表.png", initial["introduction"].asset_hashes)

        unrelated = registry()
        unrelated["charts"]["unused"]["data"] = [999]
        self.assertEqual(self.evidence(unrelated)["introduction"].digest, initial["introduction"].digest)

        (self.root / "deck/assets/図 表.png").write_bytes(b"png-two")
        changed_asset = self.evidence()
        self.assertNotEqual(changed_asset["introduction"].digest, initial["introduction"].digest)
        self.assertEqual(changed_asset["method"].digest, initial["method"].digest)

        self.html_path.write_text(html(".slide{width:1280px;height:720px;color:red}"), encoding="utf-8")
        changed_css = self.evidence()
        self.assertNotEqual(changed_css["introduction"].digest, changed_asset["introduction"].digest)
        self.assertNotEqual(changed_css["method"].digest, changed_asset["method"].digest)

    def test_duplicate_slides_and_missing_section_ids_are_rejected(self) -> None:
        self.html_path.write_text(
            "<section data-slide-id='same' data-section-id='a'></section>"
            "<section data-slide-id='same' data-section-id='b'></section>", encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "Duplicate slide id"):
            self.evidence()

    def test_review_evidence_tracks_recursive_stylesheet_dependencies(self) -> None:
        (self.root / "deck/styles").mkdir()
        (self.root / "deck/theme.css").write_text(
            '@import "styles/base.css"; .slide{background-image:url("assets/図 表.png")}',
            encoding="utf-8",
        )
        nested = self.root / "deck/styles/base.css"
        nested.write_text(".slide{color:#123456}", encoding="utf-8")
        self.html_path.write_text(
            html().replace(
                "<style>.slide{width:1280px;height:720px}</style>",
                '<link rel="stylesheet" href="theme.css">',
            ),
            encoding="utf-8",
        )
        first = compute_html_deck_structure_evidence(
            self.html_path, registry(), repository=self.root,
        )
        self.assertEqual(
            set(first.dependency_hashes),
            {"deck/theme.css", "deck/styles/base.css", "deck/assets/図 表.png"},
        )
        nested.write_text(".slide{color:#654321}", encoding="utf-8")
        second = compute_html_deck_structure_evidence(
            self.html_path, registry(), repository=self.root,
        )
        self.assertNotEqual(second.review_digest, first.review_digest)


class SingleHtmlWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "sources", "planning", "deck/assets"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in ("workflow/deck.schema.json", "workflow/deck.v1.schema.json"):
            shutil.copy2(ROOT / relative, self.root / relative)
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        (self.root / "REQUEST.md").write_text("# Request\nCreate a research deck.\n", encoding="utf-8")
        (self.root / "sources/private/spec.md").write_text("evidence", encoding="utf-8")
        for filename in ("explanation-policy.md", "story-outline.md", "slide-plan.md"):
            (self.root / "planning" / filename).write_text("# Plan\nSubstantive content.\n", encoding="utf-8")
        (self.root / "deck/deck.preview.html").write_text(html(), encoding="utf-8")
        (self.root / "deck/deck.registry.json").write_text(json.dumps(registry(), ensure_ascii=False), encoding="utf-8")
        (self.root / "deck/assets/図 表.png").write_bytes(b"asset")

        # Build a clean v2 state first; the test manifest is a new-project
        # authority, not a file for the v1 migrator to overwrite.
        v1 = load_state(self.root)
        state, _, _ = migrate_v1_state(self.root, v1, dry_run=True)
        (self.root / "sources/source-manifest.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "authorityMode": "single",
            "items": [{"id": "spec", "path": "sources/private/spec.md", "type": "document", "role": "primary"}],
        }, sort_keys=False), encoding="utf-8")
        state["project"].update(kind="research_project", primarySource=None)
        state["sources"].update(manifest="sources/source-manifest.yaml", authorityMode="single")
        state["authoring"].update(mode="single", entryHtml="deck/deck.preview.html", registry="deck/deck.registry.json", currentSection=None)
        state["workflow"].update(
            stage="planning", status="in_progress", owner="work", sourceOfTruth="planning",
            currentChapter=None, currentSection=None, blockingReason=None, blockedFrom=None,
        )
        state["chapters"] = {}
        state["sections"] = {}
        atomic_write_state(self.root, state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def whole_deck_review(self) -> dict:
        state = load_state(self.root)
        state["authoring"]["strategy"] = "whole_deck"
        state["authoring"]["htmlChange"] = None
        atomic_write_state(self.root, state)
        command_configure_sections(self.root, state, ["introduction", "method"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        authored = load_state(self.root)
        self.assertIsNone(authored["workflow"]["currentSection"])
        self.assertTrue(all(
            entry["status"] == "html_authoring" for entry in authored["sections"].values()
        ))
        command_complete_html_deck(self.root, authored)
        return load_state(self.root)

    def candidate(self, value: str) -> Path:
        path = self.root / "scratch/candidate.preview.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path.relative_to(self.root)

    def browser_evidence(self, affected_slide_ids: list[str]) -> HtmlChangeBrowserEvidence:
        screenshots: dict[str, Path] = {}
        for index, slide_id in enumerate(affected_slide_ids, start=1):
            path = self.root / "scratch/browser-evidence" / f"{index:02d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"png:{slide_id}".encode("utf-8"))
            screenshots[slide_id] = path
        environment_digest = "sha256:" + "e" * 64
        return HtmlChangeBrowserEvidence(
            report={
                "format": POST_APPLY_REPORT_FORMAT,
                "status": "pass",
                "affectedSlideIds": list(affected_slide_ids),
                "checks": [
                    {"slideId": slide_id, "status": "pass", "issues": []}
                    for slide_id in affected_slide_ids
                ],
                "networkPolicy": "local-only",
                "renderPolicy": {"animations": "disabled"},
            },
            environment={
                "format": "bento/browser-environment/v1",
                "environmentDigest": environment_digest,
                "browserEnvironment": {
                    "renderPolicy": {"animations": "disabled"},
                },
            },
            screenshots=screenshots,
        )

    def test_section_approval_detects_global_change_and_conversion_revalidates(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["introduction", "method"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        command_begin_section(self.root, state, "introduction")
        command_complete_section(self.root, state, "introduction")
        command_approve_section(self.root, state, "introduction")
        self.assertEqual(state["workflow"]["currentSection"], "method")

        command_complete_section(self.root, state, "method")
        original = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        (self.root / "deck/deck.preview.html").write_text(original.replace("</style>", "body{color:red}</style>"), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Approved section changed"):
            command_approve_section(self.root, state, "method")

        (self.root / "deck/deck.preview.html").write_text(original, encoding="utf-8")
        command_approve_section(self.root, state, "method")
        self.assertEqual(state["workflow"]["stage"], "ready_for_conversion")
        self.assertTrue(state["handoff"]["readyForCodex"])

        (self.root / "deck/assets/図 表.png").write_bytes(b"changed")
        with self.assertRaisesRegex(WorkflowError, "Approved section changed"):
            command_prepare_conversion(self.root, state)
        command_unlock_section(self.root, state, "introduction")
        self.assertEqual(state["sections"]["introduction"]["status"], "authoring")
        self.assertIsNone(state["sections"]["introduction"]["approvalDigest"])
        self.assertEqual(state["workflow"]["stage"], "html_authoring")

    def test_html_review_ignores_planning_canonical_membership(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["introduction", "method"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        command_complete_section(self.root, state, "introduction")

        current = load_state(self.root)
        self.assertEqual(current["sections"]["introduction"]["canonical"], "html")
        self.assertEqual(current["sections"]["method"]["canonical"], "planning")
        self.assertEqual(current["sections"]["method"]["slideIds"], [])
        validate_sections(self.root, current)

    def test_whole_deck_review_approves_all_section_digests_once(self) -> None:
        state = self.whole_deck_review()
        self.assertEqual(state["workflow"]["stage"], "html_review")
        self.assertIsNone(state["workflow"]["currentSection"])
        self.assertTrue(all(entry["status"] == "html_review" for entry in state["sections"].values()))

        command_approve_current(self.root, state)
        approved = load_state(self.root)
        self.assertEqual(approved["workflow"]["stage"], "ready_for_conversion")
        self.assertTrue(approved["handoff"]["readyForCodex"])
        self.assertTrue(all(entry["status"] == "approved" for entry in approved["sections"].values()))
        self.assertTrue(all(entry["approvalDigest"] for entry in approved["sections"].values()))

    def test_adopt_existing_full_html_opens_one_review_without_changing_html(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["introduction", "method"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        before = (self.root / "deck/deck.preview.html").read_bytes()
        command_adopt_whole_deck(self.root, state)
        adopted = load_state(self.root)
        self.assertEqual(adopted["authoring"]["strategy"], "whole_deck")
        self.assertEqual(adopted["workflow"]["stage"], "html_review")
        self.assertIsNone(adopted["workflow"]["currentSection"])
        self.assertEqual((self.root / "deck/deck.preview.html").read_bytes(), before)
        self.assertEqual(adopted["sections"]["method"]["slideIds"], ["method-1"])
        self.assertEqual(
            adopted["authoring"]["htmlReview"]["format"],
            "bento/html-deck-review-baseline/v1",
        )

    def test_direct_canonical_edit_cannot_be_approved_as_the_reviewed_deck(self) -> None:
        state = self.whole_deck_review()
        canonical_path = self.root / "deck/deck.preview.html"
        canonical_path.write_text(
            canonical_path.read_text(encoding="utf-8").replace("Method", "Unreviewed edit"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(WorkflowError, "review baseline is stale"):
            command_approve_html_deck(self.root, state)

    def test_external_css_change_invalidates_an_approved_proposal(self) -> None:
        canonical_path = self.root / "deck/deck.preview.html"
        canonical = canonical_path.read_text(encoding="utf-8")
        canonical_path.write_text(
            re.sub(
                r"<style>.*?</style>",
                '<link rel="stylesheet" href="theme.css">',
                canonical,
                flags=re.DOTALL,
            ),
            encoding="utf-8",
        )
        stylesheet = self.root / "deck/theme.css"
        stylesheet.write_text(
            '.slide{width:1280px;height:720px;background-image:url("assets/図 表.png")}',
            encoding="utf-8",
        )
        state = self.whole_deck_review()
        candidate = self.candidate(
            canonical_path.read_text(encoding="utf-8").replace("Method", "Updated method")
        )
        command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        command_approve_html_change(self.root, load_state(self.root))
        stylesheet.write_text(
            ".slide{width:1280px;height:720px;color:red}", encoding="utf-8",
        )

        with self.assertRaisesRegex(WorkflowError, "dependency changed"):
            command_apply_html_change(self.root, load_state(self.root))
        self.assertNotIn("Updated method", canonical_path.read_text(encoding="utf-8"))

    def test_reviewed_local_change_does_not_mutate_canonical_until_apply(self) -> None:
        state = self.whole_deck_review()
        canonical = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        candidate = self.candidate(canonical.replace("Method", "Updated method"))
        proposal = command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法の説明を更新", summary="方法スライドの説明を更新します",
            impact_summary="方法スライドだけに影響し、共通スタイルは変えません",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        self.assertEqual(proposal["scope"], "local")
        self.assertEqual(proposal["changedSlideIds"], ["method-1"])
        self.assertEqual(proposal["affectedSlideIds"], ["method-1"])
        self.assertEqual((self.root / "deck/deck.preview.html").read_text(encoding="utf-8"), canonical)
        with self.assertRaisesRegex(WorkflowError, "must be 'approved'"):
            command_apply_html_change(self.root, load_state(self.root))

        command_approve_html_change(self.root, load_state(self.root))
        command_apply_html_change(self.root, load_state(self.root))
        applied = load_state(self.root)
        self.assertIn("Updated method", (self.root / "deck/deck.preview.html").read_text(encoding="utf-8"))
        self.assertEqual(applied["authoring"]["htmlChange"]["status"], "applied")
        self.assertEqual(
            applied["authoring"]["htmlChange"]["postApplyReview"]["status"],
            "pending",
        )
        self.assertTrue(all(entry["status"] == "html_review" for entry in applied["sections"].values()))
        with self.assertRaisesRegex(WorkflowError, "post-apply browser review"):
            command_approve_html_deck(self.root, applied)

        expected = self.browser_evidence(["method-1"])
        with patch(
            "scripts.deck_workflow.collect_html_change_browser_evidence",
            return_value=expected,
        ):
            command_check_html_change(
                self.root, load_state(self.root), browser_executable=None,
            )
        checked = load_state(self.root)
        review = checked["authoring"]["htmlChange"]["postApplyReview"]
        self.assertEqual(review["status"], "checked")
        self.assertEqual(set(review["screenshots"]), {"method-1"})
        screenshot_path = self.root / review["screenshots"]["method-1"]["path"]
        screenshot_payload = screenshot_path.read_bytes()
        screenshot_path.write_bytes(b"tampered screenshot")
        with self.assertRaisesRegex(WorkflowError, "screenshot revision is stale"):
            command_approve_html_deck(self.root, checked)
        screenshot_path.write_bytes(screenshot_payload)
        command_approve_html_deck(self.root, checked)
        self.assertEqual(load_state(self.root)["workflow"]["stage"], "ready_for_conversion")

    def test_ai_proposal_registration_rejects_a_stale_expected_base(self) -> None:
        state = self.whole_deck_review()
        canonical_path = self.root / "deck/deck.preview.html"
        registry_path = self.root / "deck/deck.registry.json"
        candidate = self.candidate(
            canonical_path.read_text(encoding="utf-8").replace("Method", "Updated method")
        )
        review_digest = state["authoring"]["htmlReview"]["evidenceDigest"]

        with self.assertRaisesRegex(WorkflowError, "base canonical HTML changed"):
            command_propose_html_change(
                self.root, state, candidate_html=candidate, candidate_registry=None,
                request="Update method", summary="Update the method slide",
                impact_summary="Only the method slide changes",
                requested_slide_ids=["method-1"], related_slide_ids=[],
                expected_base_html_revision="sha256:" + "0" * 64,
                expected_base_registry_revision=file_revision(registry_path),
                expected_base_review_digest=review_digest,
                expected_state_revision=file_revision(self.root / "deck.yaml"),
            )

        self.assertIsNone(load_state(self.root)["authoring"]["htmlChange"])

    def test_ai_proposal_registration_rechecks_expected_base_inside_transaction(self) -> None:
        state = self.whole_deck_review()
        canonical_path = self.root / "deck/deck.preview.html"
        registry_path = self.root / "deck/deck.registry.json"
        candidate = self.candidate(
            canonical_path.read_text(encoding="utf-8").replace("Method", "Updated method")
        )
        original_commit = ArtifactTransactionStore.commit

        def racing_commit(store, payloads, **kwargs):
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["document"]["title"] = "Concurrent registry edit"
            registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
            return original_commit(store, payloads, **kwargs)

        with patch.object(ArtifactTransactionStore, "commit", new=racing_commit):
            with self.assertRaisesRegex(WorkflowError, "base canonical registry changed"):
                command_propose_html_change(
                    self.root, state, candidate_html=candidate, candidate_registry=None,
                    request="Update method", summary="Update the method slide",
                    impact_summary="Only the method slide changes",
                    requested_slide_ids=["method-1"], related_slide_ids=[],
                    expected_base_html_revision=file_revision(canonical_path),
                    expected_base_registry_revision=file_revision(registry_path),
                    expected_base_review_digest=state["authoring"]["htmlReview"]["evidenceDigest"],
                    expected_state_revision=file_revision(self.root / "deck.yaml"),
                )

        self.assertIsNone(load_state(self.root)["authoring"]["htmlChange"])

    def test_change_impact_expands_for_related_slides_and_global_css(self) -> None:
        state = self.whole_deck_review()
        canonical = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        related_candidate = self.candidate(
            canonical.replace('data-bento-id="plot-image"', 'data-bento-id="plot-image" alt="updated"')
            .replace("Method", "Updated method")
        )
        related = command_propose_html_change(
            self.root, state, candidate_html=related_candidate, candidate_registry=None,
            request="導入を分かりやすくする", summary="導入と関連する方法説明を調整します",
            impact_summary="導入の変更が方法スライドの接続表現にも影響します",
            requested_slide_ids=["導入-1"], related_slide_ids=["method-1"],
        )
        self.assertEqual(related["scope"], "related")
        self.assertEqual(set(related["affectedSlideIds"]), {"導入-1", "method-1"})

        command_cancel_html_change(self.root, load_state(self.root))
        global_candidate = self.candidate(canonical.replace("</style>", "body{color:red}</style>"))
        global_change = command_propose_html_change(
            self.root, load_state(self.root), candidate_html=global_candidate, candidate_registry=None,
            request="全体の文字色を調整", summary="共通スタイルの文字色を調整します",
            impact_summary="共通CSSのため全スライドを再確認します",
            requested_slide_ids=["導入-1"], related_slide_ids=[],
        )
        self.assertEqual(global_change["scope"], "global")
        self.assertTrue(global_change["globalStyleChanged"])
        self.assertEqual(set(global_change["affectedSlideIds"]), {"導入-1", "method-1"})

    def test_stale_base_rejects_change_approval(self) -> None:
        state = self.whole_deck_review()
        canonical_path = self.root / "deck/deck.preview.html"
        canonical = canonical_path.read_text(encoding="utf-8")
        candidate = self.candidate(canonical.replace("Method", "Updated method"))
        command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        canonical_path.write_text(canonical.replace("Method", "Concurrent edit"), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "canonical HTML changed"):
            command_approve_html_change(self.root, load_state(self.root))

        # Cancellation is always safe because it never installs candidate bytes.
        command_cancel_html_change(self.root, load_state(self.root))
        self.assertEqual(load_state(self.root)["authoring"]["htmlChange"]["status"], "cancelled")

    def test_tampered_candidate_or_impact_rejects_confirmation(self) -> None:
        state = self.whole_deck_review()
        canonical = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        candidate = self.candidate(canonical.replace("Method", "Updated method"))
        proposal = command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        snapshot = self.root / proposal["candidateHtml"]
        snapshot.write_text(snapshot.read_text(encoding="utf-8") + "<!-- tampered -->", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "candidate HTML changed"):
            command_approve_html_change(self.root, load_state(self.root))
        command_cancel_html_change(self.root, load_state(self.root))

        fresh = command_propose_html_change(
            self.root, load_state(self.root), candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        altered = load_state(self.root)
        altered["authoring"]["htmlChange"]["affectedSlideIds"] = ["導入-1"]
        altered["authoring"]["htmlChange"]["slideTitles"] = {"導入-1": "Introduction"}
        with self.assertRaisesRegex(WorkflowError, "proposalDigest"):
            atomic_write_state(self.root, altered)
        with self.assertRaisesRegex(WorkflowError, "explanation or impact changed"):
            command_approve_html_change(self.root, altered)
        unchanged = load_state(self.root)
        self.assertEqual(
            unchanged["authoring"]["htmlChange"]["proposalDigest"],
            fresh["proposalDigest"],
        )
        self.assertEqual(fresh["status"], "proposed")

    def test_human_explanation_is_bound_to_the_approved_proposal(self) -> None:
        state = self.whole_deck_review()
        canonical = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        candidate = self.candidate(canonical.replace("Method", "Updated method"))
        command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        altered = load_state(self.root)
        altered["authoring"]["htmlChange"]["summary"] = "承認画面とは異なる説明"
        with self.assertRaisesRegex(WorkflowError, "explanation or impact changed"):
            command_approve_html_change(self.root, altered)
        self.assertEqual(
            load_state(self.root)["authoring"]["htmlChange"]["status"],
            "proposed",
        )

    def test_apply_revalidates_base_and_candidate_under_one_union_lease(self) -> None:
        state = self.whole_deck_review()
        canonical_path = self.root / "deck/deck.preview.html"
        canonical = canonical_path.read_text(encoding="utf-8")
        candidate = self.candidate(canonical.replace("Method", "Updated method"))
        command_propose_html_change(
            self.root, state, candidate_html=candidate, candidate_registry=None,
            request="方法を変更", summary="方法説明を変更します",
            impact_summary="方法スライドだけに影響します",
            requested_slide_ids=["method-1"], related_slide_ids=[],
        )
        command_approve_html_change(self.root, load_state(self.root))
        approved = load_state(self.root)
        proposal = approved["authoring"]["htmlChange"]
        snapshot_html = (self.root / proposal["candidateHtml"]).resolve()
        snapshot_registry = (self.root / proposal["candidateRegistry"]).resolve()
        original_commit = ArtifactTransactionStore.commit
        observed_artifacts: set[Path] = set()

        def racing_commit(store, payloads, **kwargs):
            observed_artifacts.update(store.artifacts)
            canonical_path.write_text(
                canonical.replace("Method", "Concurrent canonical edit"),
                encoding="utf-8",
            )
            return original_commit(store, payloads, **kwargs)

        with patch.object(ArtifactTransactionStore, "commit", new=racing_commit):
            with self.assertRaisesRegex(WorkflowError, "canonical HTML changed"):
                command_apply_html_change(self.root, approved)

        self.assertIn(snapshot_html, observed_artifacts)
        self.assertIn(snapshot_registry, observed_artifacts)
        self.assertIn((self.root / "deck/assets/図 表.png").resolve(), observed_artifacts)
        self.assertIn("Concurrent canonical edit", canonical_path.read_text(encoding="utf-8"))
        self.assertEqual(
            load_state(self.root)["authoring"]["htmlChange"]["status"],
            "approved",
        )

    def test_structural_changes_are_always_global(self) -> None:
        canonical_path = self.root / "deck/deck.preview.html"
        canonical = canonical_path.read_text(encoding="utf-8")
        registry_value = registry()
        slides = re.findall(r'<section class="slide".*?</section>', canonical, flags=re.DOTALL)
        self.assertEqual(len(slides), 2)
        variants = {
            "add": canonical.replace(
                "</main>",
                '<section class="slide" data-slide-id="extra-1" '
                'data-section-id="method"><div data-bento-id="extra-text">Extra</div></section>\n</main>',
            ),
            "remove": canonical.replace(slides[1], ""),
            "reorder": canonical.replace(slides[0], "__FIRST_SLIDE__")
            .replace(slides[1], slides[0])
            .replace("__FIRST_SLIDE__", slides[1]),
            "membership": canonical.replace(
                'data-slide-id="method-1" data-section-id="method"',
                'data-slide-id="method-1" data-section-id="introduction"',
            ),
        }
        expected = {"導入-1", "method-1"}
        for name, candidate_html in variants.items():
            with self.subTest(name=name):
                path = self.root / "deck" / f".{name}.preview.html"
                path.write_text(candidate_html, encoding="utf-8")
                impact = analyze_html_change(
                    base_html=canonical_path,
                    base_registry=registry_value,
                    candidate_html=path,
                    candidate_registry=registry_value,
                    repository=self.root,
                    requested_slide_ids=["導入-1"],
                )
                self.assertEqual(impact.scope, "global")
                self.assertTrue(impact.structural_impact)
                self.assertTrue(expected.issubset(set(impact.affected_slide_ids)))

    def test_registry_only_change_expands_to_the_affected_section(self) -> None:
        canonical_path = self.root / "deck/deck.preview.html"
        candidate_registry = registry()
        candidate_registry["assets"]["plot"]["path"] = "assets/図 表 2.png"
        (self.root / "deck/assets/図 表 2.png").write_bytes(b"asset-two")
        impact = analyze_html_change(
            base_html=canonical_path,
            base_registry=registry(),
            candidate_html=canonical_path,
            candidate_registry=candidate_registry,
            repository=self.root,
            requested_slide_ids=["method-1"],
        )
        self.assertTrue(impact.registry_changed)
        self.assertFalse(impact.structural_impact)
        self.assertEqual(impact.scope, "related")
        self.assertEqual(set(impact.affected_slide_ids), {"導入-1", "method-1"})
        self.assertEqual(impact.changed_section_ids, ("introduction",))

    def test_whole_deck_change_cli_keeps_target_and_related_slides_distinct(self) -> None:
        args = workflow_parser().parse_args([
            "--root", str(self.root), "propose-html-change",
            "--candidate-html", "scratch/candidate.preview.html",
            "--request", "導入を短くする",
            "--summary", "導入を整理します",
            "--impact-summary", "方法の接続表現も確認します",
            "--target-slide", "導入-1",
            "--related-slide", "method-1",
        ])
        self.assertEqual(args.command, "propose-html-change")
        self.assertEqual(args.target_slides, ["導入-1"])
        self.assertEqual(args.related_slides, ["method-1"])


if __name__ == "__main__":
    unittest.main()
