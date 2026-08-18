from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.ai_proposal_service import (
    AdapterAvailability,
    AiProposalService,
    CodexSdkAdapter,
    JOB_FORMAT,
    RESULT_FORMAT,
)
from scripts.deck_workflow import WorkflowError


def _registry() -> dict:
    return {
        "format": "bento/html-registry/v2",
        "unitId": "deck",
        "sources": {"primary": {"path": "sources/primary.md", "type": "text/markdown", "role": "primary"}},
        "document": {"title": "Fixture", "theme": "light"},
        "assets": {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }


def _html() -> str:
    return """<!doctype html><html><head><style>.slide{width:1280px;height:720px}</style></head><body>
<main data-bento-deck>
<section class="slide" data-slide-id="s1" data-section-id="main"><h1 data-bento-id="s1-title">Alpha details</h1><p data-bento-id="s1-body">市場規模は拡大している</p></section>
<section class="slide" data-slide-id="s2" data-section-id="main"><h1 data-bento-id="s2-title">Beta details</h1></section>
</main></body></html>"""


class FakeAdapter:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.workspaces: list[Path] = []

    def availability(self) -> AdapterAvailability:
        return AdapterAvailability(True)

    def generate(self, workspace: Path, _prompt: str) -> None:
        self.workspaces.append(workspace)
        current = (workspace / "inputs/current.html").read_text(encoding="utf-8")
        registry = json.loads((workspace / "inputs/current.registry.json").read_text(encoding="utf-8"))
        request = json.loads((workspace / "inputs/request.json").read_text(encoding="utf-8"))
        result = {
            "format": RESULT_FORMAT,
            "action": request["action"],
            "requestedSlideIds": [request["slideId"]],
            "relatedSlideIds": [],
            "summary": "対象スライドを短くする",
            "impactSummary": "対象以外への変更はありません",
            "changedReason": "選択した説明を短くする",
            "factualChanges": [],
            "sourceReferences": ["primary"],
        }
        candidate = current.replace("Alpha details", "Alpha")
        if self.mode == "requested-mismatch":
            result["requestedSlideIds"] = ["s2"]
        elif self.mode == "incomplete":
            candidate = candidate.replace(
                '<section class="slide" data-slide-id="s2" data-section-id="main"><h1 data-bento-id="s2-title">Beta details</h1></section>',
                "",
            )
        elif self.mode == "registry-mismatch":
            registry["assets"] = {"new": {"path": "new.png"}}
        elif self.mode == "invented-number":
            candidate = current.replace("Alpha details", "Alpha 999")
        elif self.mode == "invented-fact":
            candidate = current.replace("Alpha details", "Gamma claim")
        elif self.mode == "japanese-shorten":
            candidate = candidate.replace("市場規模は拡大している", "市場は拡大")
        elif self.mode == "invented-japanese-fact":
            candidate = candidate.replace("市場規模は拡大している", "市場は縮小")
        elif self.mode == "invented-hiragana-fact":
            candidate = candidate.replace("市場規模は拡大している", "市場はすくない")
        elif self.mode == "unrelated-change":
            candidate = current.replace("Beta details", "Changed Beta")
        elif self.mode == "mutate-input":
            (workspace / "inputs/current.html").write_text("tampered", encoding="utf-8")
        elif self.mode in {"add-diagram", "bitmap-diagram"}:
            figure = '<div data-bento-id="s1-diagram" data-figure-id="fig-ai-flow"><svg></svg></div>'
            if self.mode == "bitmap-diagram":
                figure = '<div data-bento-id="s1-diagram" data-figure-id="fig-ai-flow"><img src="data:image/png;base64,abc"></div>'
            candidate = current.replace("</section>", figure + "</section>", 1)
            registry["figures"]["fig-ai-flow"] = {
                "role": "derived-diagram",
                "description": "Editable flow",
                "origin": {"kind": "source-derived", "sources": [{"sourceId": "primary", "locator": "Fixture"}]},
            }
        (workspace / "candidate.html").write_text(candidate, encoding="utf-8")
        (workspace / "candidate.registry.json").write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8",
        )
        (workspace / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8",
        )


class BlockingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, workspace: Path, prompt: str) -> None:
        self.entered.set()
        self.release.wait(5)
        super().generate(workspace, prompt)


class UnavailableAdapter(FakeAdapter):
    def availability(self) -> AdapterAvailability:
        return AdapterAvailability(False, "SDKを利用できません")


class AiProposalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "deck").mkdir()
        (self.root / "sources").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "deck/current.html").write_text(_html(), encoding="utf-8")
        (self.root / "deck/current.registry.json").write_text(
            json.dumps(_registry(), ensure_ascii=False), encoding="utf-8",
        )
        (self.root / "sources/primary.md").write_text(
            "Alpha Beta source\n市場規模は拡大している\n", encoding="utf-8",
        )
        (self.root / "sources/source-manifest.yaml").write_text(
            "schemaVersion: 1\nitems:\n  - id: primary\n    path: sources/primary.md\n    role: primary\n",
            encoding="utf-8",
        )
        for name in ("html-change-review.md", "html-first-authoring-contract.md", "visual-workflow.md"):
            (self.root / "docs" / name).write_text("fixture specification", encoding="utf-8")
        (self.root / "deck.yaml").write_text("sentinel: unchanged\n", encoding="utf-8")
        self.state = {
            "workflow": {"stage": "html_review"},
            "authoring": {
                "mode": "single",
                "strategy": "whole_deck",
                "entryHtml": "deck/current.html",
                "registry": "deck/current.registry.json",
                "htmlChange": None,
                "htmlReview": {"evidenceDigest": "sha256:review-current"},
            },
            "sources": {"manifest": "sources/source-manifest.yaml"},
        }
        self.proposals: list[dict] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, mode: str = "success") -> AiProposalService:
        def propose(_root: Path, _state: dict, **kwargs):
            self.proposals.append(kwargs)
            self.state["authoring"]["htmlChange"] = {"status": "proposed"}
            return {"status": "proposed"}

        return AiProposalService(
            self.root,
            adapter=FakeAdapter(mode),
            state_loader=lambda _root: copy.deepcopy(self.state),
            propose=propose,
        )

    def wait_for_terminal(self, service: AiProposalService):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status()
            if status.status != "running":
                return status
            time.sleep(0.01)
        self.fail("AI proposal service did not reach a terminal state")

    def test_success_registers_candidate_and_leaves_canonical_inputs_unchanged(self) -> None:
        html_before = (self.root / "deck/current.html").read_bytes()
        registry_before = (self.root / "deck/current.registry.json").read_bytes()
        state_before = (self.root / "deck.yaml").read_bytes()
        service = self.service()

        started = service.start(slide_id="s1", action="shorten", instruction="")
        result = self.wait_for_terminal(service)

        self.assertEqual(started.status, "running")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, "succeeded")
        self.assertEqual(len(self.proposals), 1)
        adapter = service.adapter
        self.assertIsInstance(adapter, FakeAdapter)
        request = json.loads(
            (adapter.workspaces[0] / "inputs/request.json").read_text(encoding="utf-8"),
        )
        self.assertEqual(request["slideTitle"], "Alpha details")
        self.assertTrue((adapter.workspaces[0] / "inputs/output-schema.json").is_file())
        self.assertEqual((self.root / "deck/current.html").read_bytes(), html_before)
        self.assertEqual((self.root / "deck/current.registry.json").read_bytes(), registry_before)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), state_before)

    def test_invalid_or_overbroad_agent_outputs_never_register_a_proposal(self) -> None:
        for mode in (
            "requested-mismatch", "incomplete", "registry-mismatch", "invented-number", "invented-fact",
            "unrelated-change", "mutate-input", "invented-japanese-fact", "invented-hiragana-fact",
        ):
            with self.subTest(mode=mode):
                self.state["authoring"]["htmlChange"] = None
                service = self.service(mode)
                service.start(slide_id="s1", action="shorten", instruction="")
                result = self.wait_for_terminal(service)
                self.assertEqual(result.status, "failed")
                self.assertTrue(result.retryable)
        self.assertEqual(self.proposals, [])

    def test_japanese_shortening_is_not_mistaken_for_a_new_fact(self) -> None:
        service = self.service("japanese-shorten")

        service.start(slide_id="s1", action="shorten", instruction="")

        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")
        self.assertEqual(len(self.proposals), 1)

    def test_canonical_inputs_changed_during_generation_reject_stale_candidate(self) -> None:
        for changed_input in ("html", "registry", "review", "deck"):
            with self.subTest(changed_input=changed_input):
                self.state["authoring"]["htmlChange"] = None
                self.state["authoring"]["htmlReview"]["evidenceDigest"] = "sha256:review-current"
                (self.root / "deck/current.html").write_text(_html(), encoding="utf-8")
                (self.root / "deck/current.registry.json").write_text(
                    json.dumps(_registry(), ensure_ascii=False), encoding="utf-8",
                )
                (self.root / "deck.yaml").write_text("sentinel: unchanged\n", encoding="utf-8")
                adapter = BlockingAdapter()
                service = AiProposalService(
                    self.root,
                    adapter=adapter,
                    state_loader=lambda _root: copy.deepcopy(self.state),
                    propose=lambda *_args, **kwargs: self.proposals.append(kwargs),
                )
                service.start(slide_id="s1", action="shorten", instruction="")
                self.assertTrue(adapter.entered.wait(2))
                if changed_input == "html":
                    path = self.root / "deck/current.html"
                    path.write_text(
                        path.read_text(encoding="utf-8").replace("Alpha details", "Externally changed"),
                        encoding="utf-8",
                    )
                elif changed_input == "registry":
                    path = self.root / "deck/current.registry.json"
                    registry = json.loads(path.read_text(encoding="utf-8"))
                    registry["document"]["title"] = "Externally changed"
                    path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
                elif changed_input == "review":
                    self.state["authoring"]["htmlReview"]["evidenceDigest"] = "sha256:review-new"
                else:
                    (self.root / "deck.yaml").write_text("sentinel: changed\n", encoding="utf-8")
                adapter.release.set()

                result = self.wait_for_terminal(service)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error, "AI実行中に現在案が変更されました。最新の内容から再試行してください。")
        self.assertEqual(self.proposals, [])

    def test_candidate_validation_is_followed_by_a_second_revision_check(self) -> None:
        def analyze_then_change(**kwargs):
            from bento_converter.html_change import analyze_html_change

            impact = analyze_html_change(**kwargs)
            canonical = self.root / "deck/current.html"
            canonical.write_text(
                canonical.read_text(encoding="utf-8").replace("Alpha details", "Changed during validation"),
                encoding="utf-8",
            )
            return impact

        service = AiProposalService(
            self.root,
            adapter=FakeAdapter(),
            state_loader=lambda _root: copy.deepcopy(self.state),
            propose=lambda *_args, **kwargs: self.proposals.append(kwargs),
            analyze=analyze_then_change,
        )

        service.start(slide_id="s1", action="shorten", instruction="")
        result = self.wait_for_terminal(service)

        self.assertEqual(result.status, "failed")
        self.assertEqual(self.proposals, [])

    def test_sdk_adapter_uses_version_compatible_config_and_deny_all(self) -> None:
        captured: dict[str, object] = {}

        class Config:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class Sandbox:
            workspace_write = object()

        class ApprovalMode:
            deny_all = object()

        class Thread:
            async def run(self, _prompt, **kwargs):
                captured["run"] = kwargs
                return type("Result", (), {"error": None})()

        class AsyncCodex:
            def __init__(self, _config):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def account(self, **_kwargs):
                return type("Account", (), {"account": object()})()

            async def thread_start(self, **kwargs):
                captured["thread_start"] = kwargs
                return Thread()

        adapter = CodexSdkAdapter()
        adapter._imports = lambda: (AsyncCodex, Config, Sandbox, ApprovalMode)  # type: ignore[method-assign]

        asyncio.run(adapter._generate(self.root, "fixture prompt"))

        config = captured["config"]
        self.assertIsInstance(config, dict)
        overrides = config["config_overrides"]
        self.assertNotIn("agents.enabled=false", overrides)
        self.assertNotIn('approval_policy="never"', overrides)
        self.assertIs(captured["thread_start"]["approval_mode"], ApprovalMode.deny_all)
        self.assertIs(captured["run"]["approval_mode"], ApprovalMode.deny_all)

    @unittest.skipUnless(importlib.util.find_spec("openai_codex"), "optional Codex SDK is not installed")
    def test_installed_codex_sdk_contract_exposes_deny_all(self) -> None:
        from openai_codex import Thread

        adapter = CodexSdkAdapter()

        async_codex, config_type, _sandbox, approval_mode = adapter._imports()
        config = adapter._config(config_type, self.root)

        self.assertTrue(hasattr(approval_mode, "deny_all"))
        self.assertNotIn("agents.enabled=false", config.config_overrides)
        self.assertIn("approval_mode", inspect.signature(async_codex.thread_start).parameters)
        self.assertIn("approval_mode", inspect.signature(Thread.run).parameters)

    def test_add_diagram_accepts_native_source_derived_figure_and_rejects_bitmap(self) -> None:
        service = self.service("add-diagram")
        service.start(slide_id="s1", action="add-diagram", instruction="")
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")
        self.assertEqual(len(self.proposals), 1)

        self.state["authoring"]["htmlChange"] = None
        service = self.service("bitmap-diagram")
        service.start(slide_id="s1", action="add-diagram", instruction="")
        self.assertEqual(self.wait_for_terminal(service).status, "failed")
        self.assertEqual(len(self.proposals), 1)

    def test_wrong_stage_active_proposal_and_duplicate_job_are_rejected(self) -> None:
        service = self.service()
        self.state["workflow"]["stage"] = "bento_authoring"
        with self.assertRaisesRegex(WorkflowError, "HTML全体"):
            service.start(slide_id="s1", action="shorten", instruction="")
        self.state["workflow"]["stage"] = "html_review"
        self.state["authoring"]["htmlChange"] = {"status": "proposed"}
        with self.assertRaisesRegex(WorkflowError, "変更案"):
            service.start(slide_id="s1", action="shorten", instruction="")

    def test_duplicate_job_is_rejected_for_the_same_repository(self) -> None:
        adapter = BlockingAdapter()
        service = AiProposalService(
            self.root,
            adapter=adapter,
            state_loader=lambda _root: copy.deepcopy(self.state),
            propose=lambda *_args, **_kwargs: {},
        )
        service.start(slide_id="s1", action="shorten", instruction="")
        self.assertTrue(adapter.entered.wait(2))
        try:
            with self.assertRaisesRegex(WorkflowError, "すでに実行中"):
                service.start(slide_id="s1", action="shorten", instruction="")
        finally:
            adapter.release.set()
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

    def test_custom_requires_instruction_and_unknown_slide_is_rejected(self) -> None:
        service = self.service()
        with self.assertRaisesRegex(WorkflowError, "指示"):
            service.start(slide_id="s1", action="custom", instruction="   ")
        with self.assertRaisesRegex(WorkflowError, "存在"):
            service.start(slide_id="unknown", action="shorten", instruction="")

    def test_interrupted_marker_becomes_retryable_without_touching_canonical(self) -> None:
        marker = self.root / ".bento-ai/runs/old/job.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({
            "format": JOB_FORMAT, "status": "running", "phase": "running-agent",
        }), encoding="utf-8")
        service = self.service()

        status = service.status()

        self.assertEqual(status.status, "failed")
        self.assertTrue(status.retryable)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["status"], "failed")

    def test_missing_sdk_is_reported_without_preventing_service_startup(self) -> None:
        service = AiProposalService(
            self.root,
            adapter=UnavailableAdapter(),
            state_loader=lambda _root: copy.deepcopy(self.state),
        )
        status = service.status()
        self.assertFalse(status.available)
        self.assertEqual(status.reason, "SDKを利用できません")
        with self.assertRaisesRegex(WorkflowError, "SDK"):
            service.start(slide_id="s1", action="shorten", instruction="")


class AiProposalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AiProposalServiceTests(methodName="run")
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.client = TestClient(create_app(
            Path.cwd(), frontend_dist=Path.cwd() / "missing-frontend", ai_service=self.service,
        ))

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_status_and_validation_do_not_expose_internal_details(self) -> None:
        status = self.client.get("/api/ai/status")
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertEqual(set(payload), {
            "available", "reason", "supportedActions", "allowedStage", "status", "phase",
            "message", "error", "retryable",
        })
        serialized = json.dumps(payload)
        for forbidden in ("thread", "digest", "revision", str(self.fixture.root), "auth"):
            self.assertNotIn(forbidden, serialized.casefold())

        self.assertEqual(self.client.post("/api/ai/proposals", json={
            "confirmed": False, "slideId": "s1", "action": "shorten", "instruction": "",
        }).status_code, 422)
        self.assertEqual(self.client.post("/api/ai/proposals", json={
            "confirmed": True, "slideId": "../deck", "action": "shorten", "instruction": "",
        }).status_code, 422)
        self.assertEqual(self.client.post("/api/ai/proposals", json={
            "confirmed": True, "slideId": "s1", "action": "unknown", "instruction": "",
        }).status_code, 422)

    def test_post_starts_background_candidate_generation(self) -> None:
        response = self.client.post("/api/ai/proposals", json={
            "confirmed": True, "slideId": "s1", "action": "shorten", "instruction": "",
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "running")
        result = self.fixture.wait_for_terminal(self.service)
        self.assertEqual(result.status, "succeeded")

    def test_post_rejects_wrong_stage_and_active_proposal(self) -> None:
        self.fixture.state["workflow"]["stage"] = "bento_authoring"
        response = self.client.post("/api/ai/proposals", json={
            "confirmed": True, "slideId": "s1", "action": "shorten", "instruction": "",
        })
        self.assertEqual(response.status_code, 409)

        self.fixture.state["workflow"]["stage"] = "html_review"
        self.fixture.state["authoring"]["htmlChange"] = {"status": "proposed"}
        response = self.client.post("/api/ai/proposals", json={
            "confirmed": True, "slideId": "s1", "action": "shorten", "instruction": "",
        })
        self.assertEqual(response.status_code, 409)
