from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.conversion_service import ConversionService
from scripts.deck_workflow import WorkflowError


def conversion_state(stage: str = "ready_for_conversion") -> dict:
    return {
        "workflow": {"stage": stage},
        "authoring": {
            "mode": "single",
            "entryHtml": "deck/deck.preview.html",
            "registry": "deck/deck.registry.json",
        },
        "chapters": {},
        "outputs": {
            "generatedHtml": "output/presentation.generated.bento.html",
            "generatedJson": "output/presentation.generated.bento.json",
            "generatedRegistry": "output/diagnostics/merged-registry.json",
            "authoringHtml": "output/presentation.authoring.bento.html",
            "authoringJson": "output/presentation.authoring.bento.json",
            "authoringRegistry": "output/presentation.authoring.registry.json",
            "finalHtml": "output/presentation.final.bento.html",
            "finalJson": "output/presentation.final.bento.json",
            "finalRegistry": "output/presentation.final.registry.json",
        },
    }


class ConversionServiceTests(unittest.TestCase):
    def wait_for_terminal(self, service: ConversionService) -> object:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status()
            if status.status != "running":
                return status
            time.sleep(0.01)
        self.fail("conversion service did not reach a terminal status")

    def service_for(self, repository: Path, state: dict, *, build=None, launch_editor=None):
        calls: list[str] = []
        build_calls: list[dict] = []

        def prepare(_root: Path, current: dict) -> None:
            calls.append("prepare")
            self.assertEqual(current["workflow"]["stage"], "ready_for_conversion")
            current["workflow"]["stage"] = "converting"

        def default_build(**kwargs) -> None:
            calls.append("build")
            build_calls.append(kwargs)

        def mark(_root: Path, current: dict) -> None:
            calls.append("mark")
            self.assertEqual(current["workflow"]["stage"], "converting")
            current["workflow"]["stage"] = "bento_validation"

        def begin(_root: Path, current: dict) -> None:
            calls.append("begin")
            self.assertEqual(current["workflow"]["stage"], "bento_validation")
            current["workflow"]["stage"] = "bento_authoring"

        def default_launch(_root: Path) -> None:
            calls.append("editor")

        build_impl = build or default_build

        def recorded_build(**kwargs):
            if build is not None:
                calls.append("build")
                build_calls.append(kwargs)
            return build_impl(**kwargs)

        service = ConversionService(
            repository,
            state_loader=lambda _root: state,
            prepare_conversion=prepare,
            build=recorded_build,
            mark_converted=mark,
            begin_authoring=begin,
            launch_editor=launch_editor or default_launch,
        )
        return service, calls, build_calls

    def test_ready_deck_runs_existing_transition_sequence_and_full_browser_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = conversion_state()
            service, calls, build_calls = self.service_for(root, state)

            started = service.start()
            self.assertEqual(started.status, "running")
            result = self.wait_for_terminal(service)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, "complete")
        self.assertEqual(result.completedSteps, 4)
        self.assertEqual(calls, ["prepare", "build", "mark", "begin", "editor"])
        self.assertEqual(state["workflow"]["stage"], "bento_authoring")
        self.assertFalse(build_calls[0]["incremental"])
        self.assertTrue(build_calls[0]["browser_check"])
        self.assertEqual(build_calls[0]["html_path"], root / "deck/deck.preview.html")
        self.assertEqual(build_calls[0]["registry_path"], root / "deck/deck.registry.json")
        self.assertEqual(build_calls[0]["output_path"], root / "output/presentation.generated.bento.html")

    def test_conversion_cannot_start_before_ready_for_conversion(self) -> None:
        state = conversion_state("html_review")
        service, _, _ = self.service_for(Path.cwd(), state)

        with self.assertRaisesRegex(WorkflowError, "HTML全体の承認後"):
            service.start()

    def test_modular_conversion_uses_registered_chapter_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = conversion_state()
            state["authoring"].update(mode="modular", entryHtml=None, registry=None)
            state["chapters"] = {
                "chapter-01": {
                    "html": "units/html/chapter-01.preview.html",
                    "registry": "units/registry/chapter-01.registry.json",
                },
                "chapter-02": {
                    "html": "units/html/chapter-02.preview.html",
                    "registry": "units/registry/chapter-02.registry.json",
                },
            }
            service, _, build_calls = self.service_for(root, state)

            service.start()
            self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

        self.assertEqual(build_calls[0]["html_dir"], root / "units/html")
        self.assertEqual(build_calls[0]["registry_dir"], root / "units/registry")
        self.assertNotIn("html_path", build_calls[0])

    def test_duplicate_conversion_is_rejected_for_the_same_repository(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_build(**_kwargs) -> None:
            entered.set()
            release.wait(5)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = conversion_state()
            service, _, _ = self.service_for(root, state, build=blocking_build)
            service.start()
            self.assertTrue(entered.wait(2))
            running = service.status()
            self.assertEqual(running.status, "running")
            self.assertEqual(running.phase, "building")
            self.assertEqual(running.completedSteps, 1)
            try:
                with self.assertRaisesRegex(WorkflowError, "すでに実行中"):
                    service.start()
            finally:
                release.set()
            self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

    def test_failed_conversion_is_visible_and_retry_resumes_from_converting(self) -> None:
        attempts = 0

        def flaky_build(**_kwargs) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("C:/private/deck.preview.html sha256:" + "1" * 64)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = conversion_state()
            service, calls, _ = self.service_for(root, state, build=flaky_build)
            service.start()
            failed = self.wait_for_terminal(service)
            self.assertEqual(failed.status, "failed")
            self.assertTrue(failed.retryable)
            self.assertNotIn("C:/private", failed.error or "")
            self.assertNotIn("sha256", failed.error or "")
            self.assertEqual(state["workflow"]["stage"], "converting")

            service.start()
            succeeded = self.wait_for_terminal(service)

        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(attempts, 2)
        self.assertEqual(calls.count("prepare"), 1)
        self.assertEqual(state["workflow"]["stage"], "bento_authoring")

    def test_backend_restart_marks_converting_as_retryable(self) -> None:
        state = conversion_state("converting")
        service, _, _ = self.service_for(Path.cwd(), state)

        status = service.status()

        self.assertEqual(status.status, "failed")
        self.assertEqual(status.phase, "building")
        self.assertTrue(status.retryable)

    def test_editor_start_failure_retries_without_rebuilding(self) -> None:
        attempts = 0

        def flaky_editor(_root: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("editor port is busy")

        with tempfile.TemporaryDirectory() as temporary:
            state = conversion_state()
            service, calls, _ = self.service_for(Path(temporary), state, launch_editor=flaky_editor)
            service.start()
            failed = self.wait_for_terminal(service)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.phase, "starting-authoring")
            self.assertTrue(failed.retryable)

            service.start()
            self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

        self.assertEqual(attempts, 2)
        self.assertEqual(calls.count("build"), 1)
        self.assertEqual(calls.count("begin"), 1)

    def test_failure_never_changes_final_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final = root / "output/presentation.final.bento.html"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"protected-final")
            state = conversion_state()

            def fail_build(**_kwargs) -> None:
                raise RuntimeError("conversion failed")

            service, _, _ = self.service_for(root, state, build=fail_build)
            service.start()
            self.assertEqual(self.wait_for_terminal(service).status, "failed")

            self.assertEqual(final.read_bytes(), b"protected-final")


class ConversionApiTests(unittest.TestCase):
    def test_api_requires_explicit_confirmation_and_ready_stage(self) -> None:
        state = conversion_state("html_review")
        conversion = ConversionService(Path.cwd(), state_loader=lambda _root: copy.deepcopy(state))
        client = TestClient(create_app(Path.cwd(), frontend_dist=Path.cwd() / "missing", conversion_service=conversion))

        self.assertEqual(client.post("/api/convert", json={"confirmed": False}).status_code, 422)
        response = client.post("/api/convert", json={"confirmed": True})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("outputs", response.text)
        self.assertEqual(client.get("/api/convert/status").json()["status"], "idle")
