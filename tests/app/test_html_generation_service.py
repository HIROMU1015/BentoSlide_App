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
from app.backend.services.html_generation_service import (
    CANDIDATE_FORMAT,
    JOB_FORMAT,
    RESULT_FORMAT,
    HtmlGenerationService,
)
from bento_converter.artifact_transaction import WriterLease
from bento_converter.html_change_review import HtmlChangeBrowserEvidence
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_apply_initial_html_candidate,
    command_approve_plan,
    command_capture_request,
    command_configure_sections,
    command_initialize,
    command_submit_plan,
    load_state,
    planning_action_artifact_paths,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeHtmlGenerationAdapter:
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
            (workspace / "inputs/planning/story-outline.md").write_text("tampered", encoding="utf-8")
        candidate = workspace / "candidate"
        candidate.mkdir()
        slide_two_section = "introduction" if self.mode == "section-mismatch" else "method"
        slide_two_id = "unknown-slide" if self.mode == "slide-mismatch" else "method-1"
        script = "<script>window.bad = true</script>" if self.mode == "script" else ""
        unsupported = "<div data-bento-id='unsupported' data-bento-type='text'>架空結論</div>" if self.mode == "unsupported-fact" else ""
        (candidate / "deck.preview.html").write_text(
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            ".slide{width:1280px;height:720px;position:relative;overflow:hidden}"
            ".item{position:absolute}</style></head><body><main data-bento-deck>"
            "<section class='slide' data-slide-id='intro-1' data-section-id='introduction'>"
            "<h1 data-bento-id='intro-title' data-bento-type='text'>背景</h1>"
            "<div data-bento-id='intro-point' data-bento-type='text'>課題</div></section>"
            f"<section class='slide' data-slide-id='{slide_two_id}' data-section-id='{slide_two_section}'>"
            "<h1 data-bento-id='method-title' data-bento-type='text'>方法</h1>"
            f"<div data-bento-id='method-point' data-bento-type='text'>手順</div>{unsupported}</section>"
            f"</main>{script}</body></html>",
            encoding="utf-8",
        )
        source_id = "unknown" if self.mode == "unknown-source" else "source"
        registry = {
            "format": "bento/html-registry/v2",
            "unitId": "deck",
            "sources": {
                "source": {
                    "path": "sources/private/source.md",
                    "type": "text/markdown",
                    "role": "primary",
                },
            },
            "document": {"title": "Fixture"},
            "assets": {},
            "fonts": {},
            "equations": {},
            "figures": {},
            "tables": {},
            "charts": {},
            "protected": {
                "slideIds": ["intro-1", slide_two_id],
                "elementIds": [],
                "requiredText": [],
            },
        }
        (candidate / "deck.registry.json").write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8",
        )
        slides = [
            {"id": "intro-1", "sectionId": "introduction", "title": "背景"},
            {"id": slide_two_id, "sectionId": slide_two_section, "title": "方法"},
        ]
        result = {
            "format": RESULT_FORMAT,
            "summary": "承認済み構成から2枚のHTML案を生成しました。",
            "slides": slides,
            "visualsSummary": "図を追加せず、文章中心で構成しました。",
            "provenanceSummary": "primary sourceに基づく記述だけを使用しました。",
            "warnings": [],
            "factualChanges": [],
            "sourceReferences": [source_id],
        }
        (workspace / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8",
        )


class BlockingHtmlGenerationAdapter(FakeHtmlGenerationAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, workspace: Path, prompt: str) -> None:
        self.entered.set()
        self.release.wait(timeout=5)
        super().generate(workspace, prompt)


def fake_browser_validator(**kwargs) -> HtmlChangeBrowserEvidence:
    slide_ids = list(kwargs["affected_slide_ids"])
    screenshots_dir = Path(kwargs["screenshots_dir"])
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    screenshots: dict[str, Path] = {}
    for index, slide_id in enumerate(slide_ids, start=1):
        path = screenshots_dir / f"{index:02d}.png"
        path.write_bytes(b"png:" + slide_id.encode("utf-8"))
        screenshots[slide_id] = path
    return HtmlChangeBrowserEvidence(
        report={
            "status": "pass",
            "affectedSlideIds": slide_ids,
            "checks": [
                {"slideId": slide_id, "sourceSize": {"w": 1280, "h": 720}, "status": "pass"}
                for slide_id in slide_ids
            ],
        },
        environment={"environmentDigest": "sha256:" + "a" * 64},
        screenshots=screenshots,
    )


def failing_browser_validator(**_kwargs) -> HtmlChangeBrowserEvidence:
    raise WorkflowError("fatal runtime error")


class HtmlGenerationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 HTML Generation fixture"
        for directory in ("workflow", "sources/private", "planning", "deck", "docs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        deck = yaml.safe_load((self.root / "deck.yaml").read_text(encoding="utf-8"))
        deck["project"].update(title="Fixture", kind="html_generation_fixture")
        deck["authoring"]["strategy"] = "whole_deck"
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(deck, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        (self.root / "REQUEST.md").write_text(
            "# 依頼\n\n背景、課題、方法、手順を説明してください。\n", encoding="utf-8",
        )
        (self.root / "sources/private/source.md").write_text(
            "# 一次資料\n\n背景、課題、方法、手順を説明する。\n", encoding="utf-8",
        )
        (self.root / "sources/source-manifest.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "authorityMode": "single",
            "items": [{
                "id": "source",
                "path": "sources/private/source.md",
                "type": "text/markdown",
                "role": "primary",
            }],
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        (self.root / "planning/explanation-policy.md").write_text(
            "# 説明方針\n\n背景と方法を簡潔に説明する。\n", encoding="utf-8",
        )
        (self.root / "planning/story-outline.md").write_text(
            "# 全体ストーリー\n\n背景、課題、方法、手順の順に説明する。\n", encoding="utf-8",
        )
        (self.root / "planning/slide-plan.md").write_text(
            "# スライド構成\n\n"
            "## Section 1: introduction\n\n### Slide 1 — 背景\n\n- 課題\n\n"
            "## Section 2: method\n\n### Slide 2 — 方法\n\n- 手順\n",
            encoding="utf-8",
        )
        (self.root / "planning/visual-plan.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "slides": [
                {"id": "intro-1", "purpose": "背景を示す", "visual": {"recommended": False, "type": "none"}},
                {"id": "method-1", "purpose": "方法を示す", "visual": {"recommended": False, "type": "none"}},
            ],
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        for relative in (
            "docs/html-first-authoring-contract.md",
            "docs/source-of-truth-policy.md",
            "docs/visual-workflow.md",
            "workflow/WORKFLOW.md",
        ):
            (self.root / relative).write_text("fixture specification\n", encoding="utf-8")
        state = load_state(self.root)
        command_initialize(self.root, state)
        command_configure_sections(self.root, load_state(self.root), ("introduction", "method"))
        state = load_state(self.root)
        state["sections"]["introduction"].update(title="背景", slideIds=["intro-1"])
        state["sections"]["method"].update(title="方法", slideIds=["method-1"])
        atomic_write_state(self.root, state)
        command_submit_plan(self.root, load_state(self.root))
        command_approve_plan(self.root, load_state(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, mode: str = "success", **kwargs) -> HtmlGenerationService:
        return HtmlGenerationService(
            self.root,
            adapter=FakeHtmlGenerationAdapter(mode),
            browser_validator=fake_browser_validator,
            **kwargs,
        )

    def wait_for_terminal(self, service: HtmlGenerationService):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status()
            if status.status != "running":
                return status
            time.sleep(0.01)
        self.fail("HTML generation service did not reach a terminal state")

    def canonical_snapshot(self) -> dict[str, bytes | None]:
        return {
            relative: (self.root / relative).read_bytes() if (self.root / relative).is_file() else None
            for relative in ("deck.yaml", "deck/deck.preview.html", "deck/deck.registry.json")
        }

    def create_candidate(self, service: HtmlGenerationService | None = None) -> HtmlGenerationService:
        selected = service or self.service()
        self.assertEqual(selected.start().status, "running")
        terminal = self.wait_for_terminal(selected)
        self.assertEqual(terminal.status, "succeeded", terminal.error)
        self.assertTrue(terminal.hasCandidate)
        return selected

    def test_availability_is_limited_to_approved_whole_deck_without_html(self) -> None:
        status = self.service().status()
        self.assertTrue(status.allowedStage)
        state = load_state(self.root)
        state["workflow"]["stage"] = "planning"
        planning_service = HtmlGenerationService(
            self.root,
            adapter=FakeHtmlGenerationAdapter(),
            state_loader=lambda _root: state,
            browser_validator=fake_browser_validator,
        )
        self.assertFalse(planning_service.status().allowedStage)

        state["workflow"]["stage"] = "html_review"
        review_service = HtmlGenerationService(
            self.root,
            adapter=FakeHtmlGenerationAdapter(),
            state_loader=lambda _root: state,
            browser_validator=fake_browser_validator,
        )
        self.assertFalse(review_service.status().allowedStage)

        (self.root / "deck/deck.preview.html").write_text("existing", encoding="utf-8")
        self.assertFalse(self.service().status().allowedStage)

    def test_generation_registers_html_and_registry_candidate_without_canonical_write(self) -> None:
        before = self.canonical_snapshot()
        service = self.create_candidate()
        candidate = service.candidate()

        self.assertEqual(self.canonical_snapshot(), before)
        self.assertEqual(candidate.generatedSlideCount, 2)
        self.assertEqual(candidate.sectionCount, 2)
        self.assertEqual([slide.id for slide in candidate.slides], ["intro-1", "method-1"])
        serialized = candidate.model_dump_json()
        for hidden in (str(self.root), "basePlanningSignature", "candidateDigest"):
            self.assertNotIn(hidden, serialized)
        marker = self.root / ".bento-ai/runs" / candidate.id / "html-generation.json"
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["format"], CANDIDATE_FORMAT)

    def test_duplicate_generation_is_rejected_and_cancel_allows_regeneration(self) -> None:
        before = self.canonical_snapshot()
        blocking = BlockingHtmlGenerationAdapter()
        service = HtmlGenerationService(
            self.root,
            adapter=blocking,
            browser_validator=fake_browser_validator,
        )
        self.assertEqual(service.start().status, "running")
        self.assertTrue(blocking.entered.wait(timeout=2))
        with self.assertRaisesRegex(WorkflowError, "すでに実行中"):
            service.start()
        blocking.release.set()
        terminal = self.wait_for_terminal(service)
        self.assertEqual(terminal.status, "succeeded", terminal.error)

        candidate = service.candidate()
        cancelled = service.cancel(
            generation_id=candidate.id,
            action_token=candidate.actionToken,
        )
        self.assertEqual(cancelled.status, "idle")
        self.assertFalse(cancelled.hasCandidate)
        self.assertEqual(self.canonical_snapshot(), before)

        regenerated = self.create_candidate(service)
        self.assertTrue(regenerated.status().hasCandidate)

    def test_invalid_ai_or_browser_output_never_registers_candidate(self) -> None:
        for mode in (
            "unknown-source", "slide-mismatch", "section-mismatch", "script",
            "unsupported-fact", "mutate-input", "failure",
        ):
            with self.subTest(mode=mode):
                before = self.canonical_snapshot()
                service = self.service(mode)
                service.start()
                terminal = self.wait_for_terminal(service)
                self.assertEqual(terminal.status, "failed")
                self.assertFalse(terminal.hasCandidate)
                self.assertEqual(self.canonical_snapshot(), before)

        browser_failure = HtmlGenerationService(
            self.root,
            adapter=FakeHtmlGenerationAdapter(),
            browser_validator=failing_browser_validator,
        )
        browser_failure.start()
        terminal = self.wait_for_terminal(browser_failure)
        self.assertEqual(terminal.status, "failed")
        self.assertIn("runtime", terminal.error or "")

    def test_changed_request_planning_project_source_or_spec_makes_generation_stale(self) -> None:
        mutations = (
            lambda: (self.root / "REQUEST.md").write_text("# 依頼\n\n変更\n", encoding="utf-8"),
            lambda: (self.root / "planning/story-outline.md").write_text("# 流れ\n\n変更\n", encoding="utf-8"),
            self._change_project_title,
            lambda: (self.root / "sources/private/source.md").write_text("# Source\n\nchanged\n", encoding="utf-8"),
            lambda: (self.root / "docs/source-of-truth-policy.md").write_text("changed specification\n", encoding="utf-8"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                backup = {
                    path: (self.root / path).read_bytes()
                    for path in (
                        "deck.yaml", "REQUEST.md", "planning/story-outline.md",
                        "sources/private/source.md", "docs/source-of-truth-policy.md",
                    )
                }
                blocking = BlockingHtmlGenerationAdapter()
                service = HtmlGenerationService(
                    self.root, adapter=blocking, browser_validator=fake_browser_validator,
                )
                service.start()
                self.assertTrue(blocking.entered.wait(timeout=2))
                mutate()
                blocking.release.set()
                terminal = self.wait_for_terminal(service)
                self.assertEqual(terminal.status, "failed")
                self.assertIn("変更", terminal.error or "")
                for path, payload in backup.items():
                    (self.root / path).write_bytes(payload)

    def test_snapshot_lease_rejects_supported_request_writer_during_capture(self) -> None:
        request = self.root / "REQUEST.md"
        original = request.read_bytes()
        entered = threading.Event()
        release = threading.Event()

        def snapshot_hook(phase: str) -> None:
            self.assertEqual(phase, "request-snapshotted")
            entered.set()
            release.wait(timeout=5)

        adapter = FakeHtmlGenerationAdapter()
        service = HtmlGenerationService(
            self.root,
            adapter=adapter,
            browser_validator=fake_browser_validator,
            snapshot_hook=snapshot_hook,
        )
        self.assertEqual(service.start().status, "running")
        self.assertTrue(entered.wait(timeout=2))
        try:
            with self.assertRaises(WorkflowError):
                command_capture_request(
                    self.root,
                    load_state(self.root),
                    text="# Request\n\nconcurrent update\n",
                )
            self.assertEqual(request.read_bytes(), original)
        finally:
            release.set()

        terminal = self.wait_for_terminal(service)
        self.assertEqual(terminal.status, "succeeded", terminal.error)
        self.assertEqual(len(adapter.workspaces), 1)
        self.assertEqual((adapter.workspaces[0] / "inputs/REQUEST.md").read_bytes(), original)
        self.assertTrue(service.candidate().id)

    def _change_project_title(self) -> None:
        state = load_state(self.root)
        state["project"]["title"] = "Changed"
        atomic_write_state(self.root, state)

    def test_apply_rejects_stale_and_tampered_candidate(self) -> None:
        service = self.create_candidate()
        candidate = service.candidate()
        before = self.canonical_snapshot()
        request = self.root / "REQUEST.md"
        request.write_text(request.read_text(encoding="utf-8") + "\n変更\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "更新|変更"):
            service.apply(generation_id=candidate.id, action_token=candidate.actionToken)
        self.assertEqual(self.canonical_snapshot(), before)

        request.write_text("# 依頼\n\n背景、課題、方法、手順を説明してください。\n", encoding="utf-8")
        html = self.root / ".bento-ai/runs" / candidate.id / "candidate/deck.preview.html"
        original_html = html.read_bytes()
        html.write_bytes(original_html + b"tampered")
        with self.assertRaisesRegex(WorkflowError, "変更"):
            service.candidate(candidate.id)
        html.write_bytes(original_html)

        registry = self.root / ".bento-ai/runs" / candidate.id / "candidate/deck.registry.json"
        registry.write_bytes(registry.read_bytes() + b" ")
        with self.assertRaisesRegex(WorkflowError, "変更"):
            service.candidate(candidate.id)

    def test_apply_is_atomic_respects_writer_lease_and_opens_html_review(self) -> None:
        service = self.create_candidate()
        candidate = service.candidate()
        candidate_html = self.root / ".bento-ai/runs" / candidate.id / "candidate/deck.preview.html"
        candidate_registry = self.root / ".bento-ai/runs" / candidate.id / "candidate/deck.registry.json"
        expected_html = candidate_html.read_bytes()
        expected_registry = candidate_registry.read_bytes()

        lease = WriterLease(self.root, planning_action_artifact_paths(self.root, load_state(self.root)))
        lease.acquire()
        try:
            with self.assertRaisesRegex(WorkflowError, "別の処理"):
                service.apply(generation_id=candidate.id, action_token=candidate.actionToken)
        finally:
            lease.release()
        self.assertFalse((self.root / "deck/deck.preview.html").exists())

        latest = service.candidate()
        service.apply(generation_id=latest.id, action_token=latest.actionToken)
        state = load_state(self.root)
        self.assertEqual(state["workflow"]["stage"], "html_review")
        self.assertEqual((self.root / "deck/deck.preview.html").read_bytes(), expected_html)
        self.assertEqual((self.root / "deck/deck.registry.json").read_bytes(), expected_registry)
        self.assertEqual(
            json.loads((self.root / ".bento-ai/runs" / candidate.id / "html-generation.json").read_text(encoding="utf-8"))["status"],
            "applied",
        )

    def test_transaction_fault_rolls_back_html_registry_state_and_marker(self) -> None:
        service = self.create_candidate()
        candidate = service.candidate()
        marker = self.root / ".bento-ai/runs" / candidate.id / "html-generation.json"
        before = self.canonical_snapshot()
        marker_before = marker.read_bytes()

        def fail_apply(*args, **kwargs):
            def fault(event: str, _journal: dict) -> None:
                if event == "replaced:2":
                    raise RuntimeError("simulated transaction failure")
            return command_apply_initial_html_candidate(*args, **kwargs, fault_injector=fault)

        failing = HtmlGenerationService(
            self.root,
            adapter=FakeHtmlGenerationAdapter(),
            browser_validator=fake_browser_validator,
            apply_command=fail_apply,
        )
        latest = failing.candidate()
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            failing.apply(generation_id=latest.id, action_token=latest.actionToken)
        self.assertEqual(self.canonical_snapshot(), before)
        self.assertEqual(marker.read_bytes(), marker_before)

    def test_restart_recovers_ready_candidate_and_interrupted_job(self) -> None:
        service = self.create_candidate()
        candidate = service.candidate()
        restarted = self.service()
        self.assertTrue(restarted.status().hasCandidate)
        self.assertEqual(restarted.candidate().id, candidate.id)
        restarted.cancel(generation_id=candidate.id, action_token=restarted.candidate().actionToken)

        interrupted = self.root / ".bento-ai/runs" / ("f" * 32)
        interrupted.mkdir()
        (interrupted / "html-generation-job.json").write_text(json.dumps({
            "format": JOB_FORMAT, "status": "running", "phase": "generating",
        }), encoding="utf-8")
        recovered = self.service()
        self.assertEqual(recovered.status().status, "failed")
        self.assertTrue(recovered.status().retryable)

    def test_api_exposes_generation_preview_apply_cancel_without_internal_fields(self) -> None:
        service = self.service()
        client = TestClient(create_app(
            self.root,
            frontend_dist=self.root / "missing-frontend",
            html_generation_service=service,
        ))
        started = client.post("/api/ai/html-generation", json={"confirmed": True, "instruction": ""})
        self.assertEqual(started.status_code, 202)
        terminal = self.wait_for_terminal(service)
        generation_id = terminal.generationId
        assert generation_id is not None

        status = client.get("/api/ai/html-generation/status")
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertTrue(payload["hasCandidate"])
        self.assertEqual(payload["candidate"]["generatedSlideCount"], 2)
        serialized = json.dumps(payload, ensure_ascii=False)
        for hidden in (str(self.root), "basePlanningSignature", "candidateDigest", "threadId"):
            self.assertNotIn(hidden, serialized)
        self.assertEqual(client.get("/api/slides?view=candidate").json()["slides"][0]["id"], "intro-1")
        self.assertEqual(client.get("/api/html/view/candidate/").status_code, 200)

        candidate = service.candidate(generation_id)
        applied = client.post(f"/api/ai/html-generation/{generation_id}/apply", json={
            "confirmed": True, "actionToken": candidate.actionToken,
        })
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(load_state(self.root)["workflow"]["stage"], "html_review")
        rejected = client.post("/api/ai/html-generation", json={"confirmed": False, "instruction": ""})
        self.assertEqual(rejected.status_code, 422)
