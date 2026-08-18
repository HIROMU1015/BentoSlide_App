from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.storyboard_service import StoryboardService
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_configure_sections,
    load_state,
)


ROOT = Path(__file__).resolve().parents[2]


class StoryboardServiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 Storyboard fixture"
        for directory in ("workflow", "sources/private", "planning", "deck"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        deck = yaml.safe_load((self.root / "deck.yaml").read_text(encoding="utf-8"))
        deck["project"].update(title="日本語 Storyboard", kind="storyboard_fixture")
        deck["authoring"]["strategy"] = "whole_deck"
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(deck, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        (self.root / "REQUEST.md").write_text(
            "# 依頼内容\n\n研究の背景と方法を、初見でも追える構成にしてください。\n", encoding="utf-8",
        )
        (self.root / "sources/private/source.md").write_text(
            "# 一次資料\n\n背景と方法の根拠。\n", encoding="utf-8",
        )
        (self.root / "sources/source-manifest.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "authorityMode": "single",
            "items": [{
                "id": "source", "path": "sources/private/source.md",
                "type": "text/markdown", "role": "primary",
            }],
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        (self.root / "planning/explanation-policy.md").write_text(
            "# 説明方針\n\n専門語を定義してから結果を説明します。\n", encoding="utf-8",
        )
        (self.root / "planning/story-outline.md").write_text(
            "# 全体ストーリー\n\n背景から方法へ順に進みます。\n", encoding="utf-8",
        )
        (self.root / "planning/slide-plan.md").write_text(
            "# スライド構成\n\n"
            "## Section 1: introduction\n\n"
            "### Slide 1 — 背景\n\n- 課題を示す\n- 目的につなぐ\n\n"
            "## Section 2: method\n\n"
            "### Slide 2 — 方法\n\n- 手順を示す\n",
            encoding="utf-8",
        )
        (self.root / "planning/visual-plan.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "slides": [
                {"id": "intro-1", "purpose": "課題を整理する", "visual": {
                    "recommended": True, "type": "native-diagram", "intent": "課題と目的を結ぶ",
                    "originKind": "source-derived",
                }},
                {"id": "method-1", "purpose": "方法を説明する", "visual": {
                    "recommended": False, "type": "none",
                }},
            ],
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.service = StoryboardService(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize_and_configure(self) -> None:
        self.service.initialize(action_token=self.service.view().actionToken)
        command_configure_sections(self.root, load_state(self.root), ("introduction", "method"))
        state = load_state(self.root)
        state["sections"]["introduction"].update(title="背景", slideIds=["intro-1"])
        state["sections"]["method"].update(title="方法", slideIds=["method-1"])
        atomic_write_state(self.root, state)

    def client(self) -> TestClient:
        return TestClient(create_app(
            self.root,
            frontend_dist=self.root / "missing-frontend",
            storyboard_service=self.service,
        ))

    def test_read_model_preserves_section_order_and_visual_guidance_without_internal_state(self) -> None:
        self.initialize_and_configure()

        view = self.service.view()

        self.assertEqual([section.id for section in view.sections], ["introduction", "method"])
        self.assertEqual([slide.title for section in view.sections for slide in section.slides], ["背景", "方法"])
        self.assertTrue(view.sections[0].slides[0].visual.recommended)  # type: ignore[union-attr]
        self.assertEqual(view.sections[0].slides[0].visual.type, "native-diagram")  # type: ignore[union-attr]
        payload = json.dumps(view.model_dump(), ensure_ascii=False)
        for internal in ("proposalDigest", "htmlRevision", "registryRevision", "documentRevision", "processId", "pid"):
            self.assertNotIn(internal, payload)
        self.assertNotIn(str(self.root), payload)
        self.assertGreaterEqual(len(view.actionToken), 20)

    def test_optional_or_unknown_planning_formats_fall_back_safely(self) -> None:
        self.initialize_and_configure()
        (self.root / "planning/visual-plan.yaml").unlink()
        (self.root / "planning/slide-plan.md").write_text(
            "# 独自形式\n\nこの構成案は独自の見出しですが、本文として安全に表示します。\n",
            encoding="utf-8",
        )

        view = self.service.view()

        self.assertEqual(view.slidePlan.title, "独自形式")
        self.assertIn("本文として安全に表示", view.slidePlan.sections[0].paragraphs[0])
        self.assertEqual([section.slides for section in view.sections], [[], []])

    def test_stale_artifact_or_reordered_sections_rejects_action_without_writing_deck(self) -> None:
        self.initialize_and_configure()
        stale_document_token = self.service.view().actionToken
        (self.root / "planning/story-outline.md").write_text(
            "# 全体ストーリー\n\n更新されたストーリーです。\n", encoding="utf-8",
        )
        before = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "更新"):
            self.service.submit(action_token=stale_document_token)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)

        current_token = self.service.view().actionToken
        command_configure_sections(self.root, load_state(self.root), ("method", "introduction"))
        reordered = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "更新"):
            self.service.submit(action_token=current_token)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), reordered)

    def test_api_delegates_full_transition_and_html_authoring_is_safe_without_html(self) -> None:
        client = self.client()
        initial = client.get("/api/storyboard")
        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.json()["canInitialize"])

        initialized = client.post("/api/storyboard/initialize", json={
            "confirmed": True, "actionToken": initial.json()["actionToken"],
        })
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(initialized.json()["stage"], "planning")
        command_configure_sections(self.root, load_state(self.root), ("introduction", "method"))

        planning = client.get("/api/storyboard").json()
        submitted = client.post("/api/storyboard/submit", json={
            "confirmed": True, "actionToken": planning["actionToken"],
        })
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()["stage"], "awaiting_plan_approval")

        approved = client.post("/api/storyboard/approve", json={
            "confirmed": True, "actionToken": submitted.json()["actionToken"],
        })
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["stage"], "html_authoring")
        self.assertEqual(load_state(self.root)["workflow"]["stage"], "html_authoring")

        state = client.get("/api/state")
        slides = client.get("/api/slides")
        html_review = client.get("/api/html/review")
        self.assertEqual(state.status_code, 200)
        self.assertFalse(state.json()["htmlAvailable"])
        self.assertEqual(slides.status_code, 200)
        self.assertEqual(slides.json()["slides"], [])
        self.assertEqual(html_review.status_code, 404)
        self.assertNotIn(str(self.root), html_review.text)

    def test_api_requires_confirmation_and_rejects_stage_mismatch(self) -> None:
        client = self.client()
        storyboard = client.get("/api/storyboard").json()
        self.assertEqual(client.post("/api/storyboard/initialize", json={
            "actionToken": storyboard["actionToken"],
        }).status_code, 422)
        self.assertEqual(client.post("/api/storyboard/initialize", json={
            "confirmed": False, "actionToken": storyboard["actionToken"],
        }).status_code, 422)
        before = (self.root / "deck.yaml").read_bytes()
        response = client.post("/api/storyboard/submit", json={
            "confirmed": True, "actionToken": storyboard["actionToken"],
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)

    def test_failed_initialize_submit_and_approve_leave_deck_state_unchanged(self) -> None:
        source = self.root / "sources/private/source.md"
        source_payload = source.read_text(encoding="utf-8")
        initialize_token = self.service.view().actionToken
        source.unlink()
        before_initialize = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "一次資料"):
            self.service.initialize(action_token=initialize_token)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before_initialize)

        source.write_text(source_payload, encoding="utf-8")
        self.initialize_and_configure()
        explanation = self.root / "planning/explanation-policy.md"
        explanation_payload = explanation.read_text(encoding="utf-8")
        explanation.write_text("# 説明方針\n", encoding="utf-8")
        before_submit = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "提出"):
            self.service.submit(action_token=self.service.view().actionToken)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before_submit)

        explanation.write_text(explanation_payload, encoding="utf-8")
        self.service.submit(action_token=self.service.view().actionToken)
        story = self.root / "planning/story-outline.md"
        story_payload = story.read_text(encoding="utf-8")
        story.write_text("# 全体ストーリー\n", encoding="utf-8")
        before_approve = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "承認"):
            self.service.approve(action_token=self.service.view().actionToken)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before_approve)
        story.write_text(story_payload, encoding="utf-8")
