from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.ai_proposal_service import AdapterAvailability
from app.backend.services.planning_ai_proposal_service import (
    JOB_FORMAT,
    PlanningAiProposalService,
)
from app.backend.services.storyboard_service import StoryboardService
from bento_converter.artifact_transaction import WriterLease
from bento_converter.planning_proposal import PLANNING_AGENT_RESULT_FORMAT
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_apply_planning_proposal,
    command_configure_sections,
    load_state,
    planning_action_artifact_paths,
)


ROOT = Path(__file__).resolve().parents[2]


class FakePlanningAdapter:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.workspaces: list[Path] = []

    def availability(self) -> AdapterAvailability:
        return AdapterAvailability(True)

    def generate(self, workspace: Path, _prompt: str) -> None:
        self.workspaces.append(workspace)
        if self.mode == "failure":
            raise RuntimeError("agent failed")
        if self.mode == "mutate-input":
            (workspace / "inputs/current/story-outline.md").write_text("tampered", encoding="utf-8")

        candidate = workspace / "candidate"
        candidate.mkdir()
        (candidate / "explanation-policy.md").write_text(
            "# 説明方針\n\n専門語を定義してから結果を説明します。詳細も示します。\n",
            encoding="utf-8",
        )
        (candidate / "story-outline.md").write_text(
            "# 全体ストーリー\n\n背景から方法、詳細へ順に進みます。\n",
            encoding="utf-8",
        )
        (candidate / "slide-plan.md").write_text(
            "# スライド構成\n\n"
            "## Section 1: introduction\n\n"
            "### Slide 1 — 背景\n\n- 課題を示す\n- 目的につなぐ\n\n"
            "## Section 2: method\n\n"
            "### Slide 2 — 方法\n\n- 手順を示す\n\n"
            "### Slide 3 — 詳細\n\n- 詳細を示す\n",
            encoding="utf-8",
        )
        visual_slides = [
            {"id": "method-2", "purpose": "詳細を示す", "visual": {
                "recommended": False, "type": "none",
            }},
            {"id": "intro-1", "purpose": "課題を整理する", "visual": {
                "recommended": True, "type": "native-diagram", "intent": "課題と目的を結ぶ",
                "originKind": "source-derived",
            }},
            {"id": "method-1", "purpose": "方法を説明する", "visual": {
                "recommended": False, "type": "none",
            }},
        ]
        if self.mode == "invalid-visual":
            visual_slides = visual_slides[:-1]
        if self.mode == "unsafe-visual-field":
            visual_slides[0]["path"] = "../../outside.png"
        (candidate / "visual-plan.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "slides": visual_slides,
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        result = {
            "format": PLANNING_AGENT_RESULT_FORMAT,
            "sections": [
                {"id": "introduction", "title": "背景", "slideIds": ["intro-1"]},
                {"id": "method", "title": "方法", "slideIds": ["method-1", "method-2"]},
            ],
            "summary": "方法の詳細を独立したスライドに分ける",
            "impactSummary": "方法sectionに詳細スライドを1枚追加し、説明方針と全体ストーリーを更新します。",
            "factualChanges": [],
            "sourceReferences": ["source"],
        }
        if self.mode == "duplicate-slide-id":
            result["sections"][1]["slideIds"] = ["method-1", "intro-1"]
        if self.mode == "unsupported-source":
            result["sourceReferences"] = ["unknown"]
        (workspace / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8",
        )


class BlockingPlanningAdapter(FakePlanningAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, workspace: Path, prompt: str) -> None:
        self.entered.set()
        self.release.wait(timeout=5)
        super().generate(workspace, prompt)


class PlanningAiProposalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 Planning AI fixture"
        for directory in ("workflow", "sources/private", "planning", "deck", "docs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        deck = yaml.safe_load((self.root / "deck.yaml").read_text(encoding="utf-8"))
        deck["project"].update(title="日本語 Planning AI", kind="planning_ai_fixture")
        deck["authoring"]["strategy"] = "whole_deck"
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(deck, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        (self.root / "REQUEST.md").write_text(
            "# 依頼内容\n\n背景、方法、詳細を初見でも追える構成にしてください。\n", encoding="utf-8",
        )
        (self.root / "sources/private/source.md").write_text(
            "# 一次資料\n\n背景、課題、目的、方法、手順、詳細を説明する。"
            "専門語を定義してから結果を説明します。詳細も示します。"
            "背景から方法、詳細へ順に進みます。1 2 3\n",
            encoding="utf-8",
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
        for relative in (
            "workflow/WORKFLOW.md", "docs/source-of-truth-policy.md", "docs/visual-workflow.md",
        ):
            (self.root / relative).write_text("fixture specification\n", encoding="utf-8")

        storyboard = StoryboardService(self.root)
        storyboard.initialize(action_token=storyboard.view().actionToken)
        command_configure_sections(self.root, load_state(self.root), ("introduction", "method"))
        state = load_state(self.root)
        state["sections"]["introduction"].update(title="背景", slideIds=["intro-1"])
        state["sections"]["method"].update(title="方法", slideIds=["method-1"])
        atomic_write_state(self.root, state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, mode: str = "success", **kwargs) -> PlanningAiProposalService:
        return PlanningAiProposalService(
            self.root, adapter=FakePlanningAdapter(mode), **kwargs,
        )

    def wait_for_terminal(self, service: PlanningAiProposalService):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status()
            if status.status != "running":
                return status
            time.sleep(0.01)
        self.fail("Planning AI service did not reach a terminal state")

    def canonical_snapshot(self) -> dict[str, bytes]:
        return {
            relative: (self.root / relative).read_bytes()
            for relative in (
                "deck.yaml", "planning/explanation-policy.md", "planning/story-outline.md",
                "planning/slide-plan.md", "planning/visual-plan.yaml",
            )
        }

    def create_proposal(self, service: PlanningAiProposalService | None = None) -> PlanningAiProposalService:
        selected = service or self.service()
        self.assertEqual(selected.start(instruction="方法を2枚に分けてください").status, "running")
        terminal = self.wait_for_terminal(selected)
        self.assertEqual(terminal.status, "succeeded", terminal.error)
        self.assertTrue(terminal.hasProposal)
        return selected

    def test_generation_registers_complete_candidate_without_changing_canonical(self) -> None:
        before = self.canonical_snapshot()
        service = self.create_proposal()

        candidate, proposal = service.candidate()

        self.assertEqual(self.canonical_snapshot(), before)
        self.assertEqual([slide.id for slide in candidate.slides], ["intro-1", "method-1", "method-2"])
        self.assertEqual([entry["id"] for entry in candidate.visual_plan["slides"]], [
            "method-2", "intro-1", "method-1",
        ])
        self.assertEqual(proposal.impact.slides[0].change, "added")
        self.assertEqual(proposal.impact.visualChanges, 1)
        self.assertNotIn(str(self.root), proposal.model_dump_json())
        self.assertNotIn("basePlanningSignature", proposal.model_dump_json())

    def test_invalid_failure_and_retry_never_change_canonical(self) -> None:
        for mode in (
            "invalid-visual", "unsafe-visual-field", "duplicate-slide-id",
            "unsupported-source", "mutate-input", "failure",
        ):
            with self.subTest(mode=mode):
                before = self.canonical_snapshot()
                service = self.service(mode)
                service.start(instruction="構成を改善してください")
                failed = self.wait_for_terminal(service)
                self.assertEqual(failed.status, "failed")
                self.assertTrue(failed.retryable)
                self.assertFalse(failed.hasProposal)
                self.assertEqual(self.canonical_snapshot(), before)
                self.assertFalse(any(
                    json.loads(path.read_text(encoding="utf-8")).get("status") == "proposed"
                    for path in (self.root / ".bento-ai/runs").glob("*/proposal.json")
                ))

        adapter = FakePlanningAdapter()
        retryable = PlanningAiProposalService(self.root, adapter=adapter)
        retryable.start(instruction="再試行します")
        self.assertEqual(self.wait_for_terminal(retryable).status, "succeeded")

    def test_other_stage_and_duplicate_job_are_rejected(self) -> None:
        blocking = BlockingPlanningAdapter()
        first = PlanningAiProposalService(self.root, adapter=blocking)
        second = self.service()
        first.start(instruction="候補を作成")
        self.assertTrue(blocking.entered.wait(timeout=2))
        try:
            with self.assertRaisesRegex(WorkflowError, "すでに実行中"):
                second.start(instruction="重複候補")
        finally:
            blocking.release.set()
            self.wait_for_terminal(first)

        state = load_state(self.root)
        state["workflow"]["stage"] = "awaiting_plan_approval"
        atomic_write_state(self.root, state)
        with self.assertRaisesRegex(WorkflowError, "planning段階"):
            self.service().start(instruction="許可されない段階")

    def test_request_changed_during_generation_rejects_candidate(self) -> None:
        blocking = BlockingPlanningAdapter()
        service = PlanningAiProposalService(self.root, adapter=blocking)
        planning_before = self.canonical_snapshot()
        service.start(instruction="候補を作成")
        self.assertTrue(blocking.entered.wait(timeout=2))
        request = self.root / "REQUEST.md"
        request.write_text(request.read_text(encoding="utf-8") + "\n外部の追記\n", encoding="utf-8")
        blocking.release.set()

        terminal = self.wait_for_terminal(service)

        self.assertEqual(terminal.status, "failed")
        self.assertIn("変更", terminal.error or "")
        self.assertFalse(terminal.hasProposal)
        self.assertEqual(self.canonical_snapshot(), planning_before)

    def test_stale_or_tampered_proposal_is_never_applied(self) -> None:
        service = self.create_proposal()
        proposal = service.proposal()
        before = self.canonical_snapshot()
        request_path = self.root / "REQUEST.md"
        request_path.write_text(request_path.read_text(encoding="utf-8") + "\n外部変更\n", encoding="utf-8")

        client = TestClient(create_app(
            self.root,
            frontend_dist=self.root / "missing-frontend",
            planning_ai_service=service,
        ))
        stale = client.post(f"/api/ai/planning/proposals/{proposal.id}/apply", json={
            "confirmed": True, "actionToken": proposal.actionToken,
        })
        self.assertEqual(stale.status_code, 409)
        self.assertRegex(stale.json()["error"], "更新|変更")
        self.assertEqual(self.canonical_snapshot(), before)

        request_path.write_text("# 依頼内容\n\n背景、方法、詳細を初見でも追える構成にしてください。\n", encoding="utf-8")
        candidate_path = self.root / ".bento-ai/runs" / proposal.id / "candidate/story-outline.md"
        candidate_path.write_text(candidate_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "変更"):
            service.candidate(proposal.id)

    def test_apply_atomically_updates_snapshot_sections_and_rotates_submit_token(self) -> None:
        storyboard = StoryboardService(self.root)
        old_token = storyboard.view().actionToken
        service = self.create_proposal()
        proposal = service.proposal()

        service.apply(proposal_id=proposal.id, action_token=proposal.actionToken)

        state = load_state(self.root)
        self.assertEqual(state["workflow"]["stage"], "planning")
        self.assertEqual(state["sections"]["method"]["slideIds"], ["method-1", "method-2"])
        self.assertIn("### Slide 3 — 詳細", (self.root / "planning/slide-plan.md").read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads((self.root / ".bento-ai/runs" / proposal.id / "proposal.json").read_text(encoding="utf-8"))["status"],
            "applied",
        )
        current = storyboard.view()
        self.assertNotEqual(current.actionToken, old_token)
        self.assertTrue(current.canSubmit)

    def test_apply_rolls_back_every_target_and_respects_writer_lease(self) -> None:
        service = self.create_proposal()
        proposal = service.proposal()
        before = self.canonical_snapshot()
        proposal_path = self.root / ".bento-ai/runs" / proposal.id / "proposal.json"
        proposal_before = proposal_path.read_bytes()

        def fail_apply(*args, **kwargs):
            def fault(event: str, _journal: dict) -> None:
                if event == "replaced:2":
                    raise RuntimeError("simulated transaction failure")
            return command_apply_planning_proposal(*args, **kwargs, fault_injector=fault)

        failing = PlanningAiProposalService(self.root, adapter=FakePlanningAdapter(), apply_command=fail_apply)
        failing_proposal = failing.proposal()
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            failing.apply(proposal_id=failing_proposal.id, action_token=failing_proposal.actionToken)
        self.assertEqual(self.canonical_snapshot(), before)
        self.assertEqual(proposal_path.read_bytes(), proposal_before)

        lease = WriterLease(self.root, planning_action_artifact_paths(self.root, load_state(self.root)))
        lease.acquire()
        try:
            current = failing.proposal()
            with self.assertRaisesRegex(WorkflowError, "別の処理"):
                failing.apply(proposal_id=current.id, action_token=current.actionToken)
        finally:
            lease.release()
        self.assertEqual(self.canonical_snapshot(), before)

    def test_restart_recovers_proposal_and_interrupted_job(self) -> None:
        service = self.create_proposal()
        proposal = service.proposal()

        restarted = self.service()
        status = restarted.status()
        self.assertTrue(status.hasProposal)
        self.assertEqual(status.proposalId, proposal.id)
        self.assertEqual(restarted.candidate()[0].slides[-1].id, "method-2")

        restarted.cancel(proposal_id=proposal.id, action_token=restarted.proposal().actionToken)
        interrupted = self.root / ".bento-ai/runs" / ("f" * 32)
        interrupted.mkdir()
        (interrupted / "planning-job.json").write_text(json.dumps({
            "format": JOB_FORMAT, "status": "running", "phase": "running-agent",
        }), encoding="utf-8")
        recovered = self.service()
        recovered_status = recovered.status()
        self.assertEqual(recovered_status.status, "failed")
        self.assertTrue(recovered_status.retryable)

    def test_api_exposes_polling_candidate_apply_and_cancel_without_internal_revisions(self) -> None:
        service = self.service()
        client = TestClient(create_app(
            self.root,
            frontend_dist=self.root / "missing-frontend",
            planning_ai_service=service,
        ))
        response = client.post("/api/ai/planning/proposals", json={
            "confirmed": True, "instruction": "方法を2枚に分けてください",
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "running")
        terminal = self.wait_for_terminal(service)

        candidate = client.get("/api/storyboard?view=candidate")
        self.assertEqual(candidate.status_code, 200)
        payload = candidate.json()
        self.assertEqual(payload["view"], "candidate")
        self.assertEqual(payload["sections"][1]["slides"][1]["id"], "method-2")
        self.assertFalse(payload["canSubmit"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for internal in ("basePlanningSignature", "candidatePlanningSignature", "proposalDigest", str(self.root)):
            self.assertNotIn(internal, serialized)

        proposal = service.proposal(terminal.proposalId)
        applied = client.post(f"/api/ai/planning/proposals/{proposal.id}/apply", json={
            "confirmed": True, "actionToken": proposal.actionToken,
        })
        self.assertEqual(applied.status_code, 200)
        self.assertTrue(applied.json()["canSubmit"])

        replacement = self.create_proposal(self.service())
        replacement_proposal = replacement.proposal()
        cancelled = TestClient(create_app(
            self.root,
            frontend_dist=self.root / "missing-frontend",
            planning_ai_service=replacement,
        )).post(f"/api/ai/planning/proposals/{replacement_proposal.id}/cancel", json={
            "confirmed": True, "actionToken": replacement_proposal.actionToken,
        })
        self.assertEqual(cancelled.status_code, 200)
        self.assertIsNone(cancelled.json()["proposal"])

        rejected = client.post("/api/ai/planning/proposals", json={
            "confirmed": False, "instruction": "未確認",
        })
        self.assertEqual(rejected.status_code, 422)
