from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.editor_session_service import WorkEditorSession
from app.backend.services.html_review_service import HtmlReviewService
from app.backend.services.workflow_service import WorkflowService, ui_mode_for_stage, verified_local_session_url
from scripts.deck_workflow import WorkflowError, load_state


class ApplicationApiTests(unittest.TestCase):
    def test_current_repository_exposes_ui_view_models_not_raw_deck_yaml(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        client = TestClient(create_app(repository, frontend_dist=repository / "missing-frontend"))

        project = client.get("/api/project")
        state = client.get("/api/state")
        slides = client.get("/api/slides")

        self.assertEqual(project.status_code, 200)
        self.assertEqual(project.json()["project"]["title"], load_state(repository)["project"]["title"])
        self.assertEqual(state.status_code, 200)
        self.assertEqual(slides.status_code, 200)
        self.assertIn(state.json()["mode"], {"storyboard", "html-design", "converting", "bento-edit", "final-edit", "complete", "blocked"})
        self.assertNotIn("approvals", state.json())
        self.assertNotIn("outputs", state.json())
        self.assertGreater(len(slides.json()["slides"]), 0)

    def test_stage_mapping_covers_html_and_bento_modes(self) -> None:
        self.assertEqual(ui_mode_for_stage("html_review"), "html-design")
        self.assertEqual(ui_mode_for_stage("bento_authoring"), "bento-edit")
        self.assertEqual(ui_mode_for_stage("bento_finalization"), "final-edit")

    def test_non_loopback_backend_bind_is_rejected(self) -> None:
        from app.backend.main import main

        with self.assertRaises(SystemExit) as raised:
            main(["--host", "0.0.0.0"])
        self.assertIn("127.0.0.1", str(raised.exception))


class HtmlReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        self.current = self.repository / "deck.preview.html"
        self.candidate = self.repository / "candidate.html"
        self.current.write_text(
            '<section data-slide-id="s1" data-section-id="main"><h1>背景</h1></section>'
            '<section data-slide-id="s2" data-section-id="main"><h1>提案</h1></section>',
            encoding="utf-8",
        )
        self.candidate.write_text(
            '<section data-slide-id="s1" data-section-id="main"><h1>背景</h1></section>'
            '<section data-slide-id="s2" data-section-id="main"><h1>新しい提案</h1></section>',
            encoding="utf-8",
        )
        self.state = {
            "workflow": {"stage": "html_review"},
            "preview": {"currentUrl": None},
            "authoring": {
                "entryHtml": "deck.preview.html",
                "htmlReview": {"htmlRevision": "html-r1", "registryRevision": "registry-r1"},
                "htmlChange": {
                    "proposalId": "proposal-1",
                    "proposalDigest": "sha256:" + "1" * 64,
                    "status": "proposed",
                    "scope": "related",
                    "summary": "提案の説明を短くする",
                    "impactSummary": "前後の用語も確認する",
                    "candidateHtml": "candidate.html",
                    "requestedSlideIds": ["s2"],
                    "relatedSlideIds": ["s1"],
                    "changedSlideIds": ["s2"],
                    "affectedSlideIds": ["s1", "s2"],
                    "addedSlideIds": [],
                    "removedSlideIds": [],
                    "slideTitles": {"s1": "背景", "s2": "新しい提案"},
                    "postApplyReview": None,
                },
            },
            "sections": {"main": {"title": "Main"}},
        }

        class FakeWorkflow:
            def __init__(inner_self, outer: "HtmlReviewServiceTests"):
                inner_self.outer = outer

            def state(inner_self):
                return copy.deepcopy(inner_self.outer.state)

            def html_source(inner_self, _state=None):
                return inner_self.outer.current

        self.workflow = FakeWorkflow(self)
        self.service = HtmlReviewService(self.repository, self.workflow)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_candidate_review_returns_readable_data_and_only_an_opaque_action_token(self) -> None:
        review = self.service.review()

        self.assertEqual(review.proposal.scope, "related")  # type: ignore[union-attr]
        self.assertEqual([slide.title for slide in review.proposal.affectedSlides], ["背景", "新しい提案"])  # type: ignore[union-attr]
        payload = review.model_dump()
        self.assertNotIn("proposalDigest", payload)
        self.assertNotIn("baseHtmlRevision", payload)
        self.assertGreaterEqual(len(review.actionToken), 20)

    def test_unreviewed_or_stale_actions_cannot_reach_engine_commands(self) -> None:
        review = self.service.review()
        with mock.patch("app.backend.services.html_review_service.load_state", return_value=copy.deepcopy(self.state)), \
             mock.patch("app.backend.services.html_review_service.command_approve_html_change") as approve:
            with self.assertRaises(WorkflowError):
                self.service.apply_and_check(action_token=review.actionToken, reviewed_slide_ids=["s1"])
            with self.assertRaises(WorkflowError):
                self.service.apply_and_check(action_token="stale-token-that-is-long-enough", reviewed_slide_ids=["s1", "s2"])
            approve.assert_not_called()

    def test_all_reviewed_slides_dispatch_existing_approve_apply_check_sequence(self) -> None:
        review = self.service.review()

        def approve(_root, _state):
            self.state["authoring"]["htmlChange"]["status"] = "approved"

        def apply(_root, _state):
            proposal = self.state["authoring"]["htmlChange"]
            proposal["status"] = "applied"
            proposal["postApplyReview"] = {"status": "pending"}

        def check(_root, _state, *, browser_executable):
            self.assertIsNone(browser_executable)
            self.state["authoring"]["htmlChange"]["postApplyReview"]["status"] = "checked"

        with mock.patch("app.backend.services.html_review_service.load_state", side_effect=lambda _root: copy.deepcopy(self.state)), \
             mock.patch("app.backend.services.html_review_service.command_approve_html_change", side_effect=approve) as approve_command, \
             mock.patch("app.backend.services.html_review_service.command_apply_html_change", side_effect=apply) as apply_command, \
             mock.patch("app.backend.services.html_review_service.command_check_html_change", side_effect=check) as check_command:
            result = self.service.apply_and_check(
                action_token=review.actionToken,
                reviewed_slide_ids=["s1", "s2"],
            )

        self.assertEqual(result.status, "checked")
        approve_command.assert_called_once()
        apply_command.assert_called_once()
        check_command.assert_called_once()
        self.assertTrue(result.review.canApproveDeck)  # type: ignore[union-attr]


class WorkflowServiceViewTests(unittest.TestCase):
    def test_can_convert_only_after_html_deck_approval(self) -> None:
        service = WorkflowService(Path.cwd())
        for stage, expected in (("html_review", False), ("ready_for_conversion", True)):
            with self.subTest(stage=stage):
                state = {
                    "workflow": {"stage": stage, "currentSection": None, "currentChapter": None, "blockingReason": None},
                    "preview": {"bentoPort": 8765},
                    "project": {"title": "Fixture", "kind": "fixture"},
                    "authoring": {"strategy": "whole_deck", "htmlChange": None},
                    "approvals": {"bentoContent": {"status": "pending"}, "finalBento": "pending"},
                    "sections": {},
                }
                with mock.patch("app.backend.services.workflow_service.load_state", return_value=state), \
                     mock.patch(
                         "app.backend.services.workflow_service.user_status_summary",
                         return_value={"current": "Fixture", "next": "Fixture", "route": "html-preview", "validActions": [], "blockingReason": None},
                     ):
                    view = service.state_view()
                self.assertEqual(view.canConvert, expected)

    def test_foreign_repository_session_url_is_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "output").mkdir()
            (repository / "output/html-preview-session.json").write_text(json.dumps({
                "format": "bento/html-preview-session/v1",
                "repository": str(repository / "another-project"),
                "port": 4173,
                "url": "http://127.0.0.1:4173/",
            }), encoding="utf-8")
            self.assertIsNone(verified_local_session_url(
                repository,
                filename="html-preview-session.json",
                expected_format="bento/html-preview-session/v1",
                expected_port=4173,
            ))

    def test_bento_authoring_enables_existing_editor_url(self) -> None:
        service = WorkflowService(Path.cwd())
        state = {
            "workflow": {"stage": "bento_authoring", "currentSection": None, "currentChapter": None, "blockingReason": None},
            "preview": {"bentoPort": 9876},
            "project": {"title": "Fixture", "kind": "fixture"},
            "authoring": {"strategy": "whole_deck", "htmlChange": None},
            "approvals": {"bentoContent": {"status": "pending"}, "finalBento": "pending"},
            "sections": {},
        }
        with mock.patch("app.backend.services.workflow_service.load_state", return_value=state), \
             mock.patch("app.backend.services.workflow_service.user_status_summary", return_value={"current": "Bento編集中", "next": "確認", "route": "authoring-editor", "validActions": [], "blockingReason": None}), \
             mock.patch("app.backend.services.workflow_service.inspect_work_editor_session", return_value=WorkEditorSession(mode="authoring", url="http://127.0.0.1:9876/")):
            view = service.state_view()
        self.assertEqual(view.mode, "bento-edit")
        self.assertTrue(view.canEditBento)
        self.assertEqual(view.bentoEditorUrl, "http://127.0.0.1:9876/")
