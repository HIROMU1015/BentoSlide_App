from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.services.bento_lifecycle_service import BentoLifecycleService
from app.backend.services.editor_session_service import WorkEditorSession, inspect_work_editor_session
from scripts.deck_workflow import WorkflowError


def lifecycle_state(stage: str = "bento_authoring") -> dict:
    return {
        "workflow": {"stage": stage},
        "preview": {"bentoPort": 8765},
        "outputs": {
            "generatedHtml": "output/presentation.generated.bento.html",
            "generatedRegistry": "output/diagnostics/merged-registry.json",
            "authoringHtml": "output/presentation.authoring.bento.html",
            "authoringJson": "output/presentation.authoring.bento.json",
            "authoringRegistry": "output/presentation.authoring.registry.json",
            "finalHtml": "output/presentation.final.bento.html",
            "finalJson": "output/presentation.final.bento.json",
            "finalRegistry": "output/presentation.final.registry.json",
        },
        "approvals": {
            "bentoContent": {
                "status": "pending", "documentRevision": None, "registryRevision": None,
                "approvalDigest": None, "approvedAt": None,
            },
            "finalBento": {
                "status": "pending", "documentRevision": None, "htmlRevision": None,
                "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
            },
        },
    }


class BentoLifecycleServiceTests(unittest.TestCase):
    def wait_for_terminal(self, service: BentoLifecycleService) -> object:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status()
            if status.status != "running":
                return status
            time.sleep(0.01)
        self.fail("lifecycle service did not reach a terminal status")

    def service_for(
        self,
        repository: Path,
        state: dict,
        *,
        editor_mode: str | None = None,
        begin_finalization_failure: list[BaseException] | None = None,
        complete_failure: list[BaseException] | None = None,
        blocking_stop: tuple[threading.Event, threading.Event] | None = None,
    ):
        calls: list[str] = []
        editor = {"mode": editor_mode}
        finalization_failures = begin_finalization_failure or []
        completion_failures = complete_failure or []

        def inspect(_root: Path, _state: dict):
            mode = editor["mode"]
            return WorkEditorSession(mode=mode, url="http://127.0.0.1:8765/") if mode else None

        def stop(_root: Path, _state: dict, expected_mode: str) -> None:
            calls.append(f"stop:{expected_mode}")
            if editor["mode"] is not None:
                self.assertEqual(editor["mode"], expected_mode)
            if blocking_stop is not None:
                blocking_stop[0].set()
                blocking_stop[1].wait(5)
            editor["mode"] = None

        def launch(_root: Path) -> None:
            calls.append("launch")
            editor["mode"] = (
                "finalization" if state["workflow"]["stage"] == "bento_finalization" else "authoring"
            )

        def open_final(_root: Path) -> None:
            calls.append("open")

        def begin_review(_root: Path, current: dict) -> None:
            calls.append("begin-review")
            self.assertIsNone(editor["mode"])
            current["workflow"]["stage"] = "content_review"

        def approve_content(_root: Path, current: dict) -> None:
            calls.append("approve-content")
            self.assertIsNone(editor["mode"])
            current["approvals"]["bentoContent"] = {
                "status": "approved",
                "documentRevision": "sha256:document-current",
                "registryRevision": "sha256:registry-current",
                "approvalDigest": "sha256:approval-current",
                "approvedAt": "2026-08-18T00:00:00Z",
            }

        def begin_final(_root: Path, current: dict) -> None:
            calls.append("begin-final")
            self.assertIsNone(editor["mode"])
            if finalization_failures:
                raise finalization_failures.pop(0)
            current["workflow"]["stage"] = "bento_finalization"
            current["approvals"]["finalBento"] = {
                "status": "pending", "documentRevision": None, "htmlRevision": None,
                "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
            }

        def restart_final(_root: Path, current: dict, *, confirmation: str) -> None:
            calls.append("restart-final")
            self.assertEqual(confirmation, "ARCHIVE-AND-RESTART-FINALIZATION")
            current["workflow"]["stage"] = "bento_finalization"

        def approve_final(_root: Path, current: dict) -> None:
            calls.append("approve-final")
            self.assertIsNone(editor["mode"])
            current["approvals"]["finalBento"] = {
                "status": "approved",
                "documentRevision": "sha256:final-document",
                "htmlRevision": "sha256:final-html",
                "registryRevision": "sha256:final-registry",
                "runtimeFingerprint": "sha256:runtime",
                "approvedAt": "2026-08-18T00:00:00Z",
            }

        def complete(_root: Path, current: dict) -> None:
            calls.append("complete")
            if completion_failures:
                raise completion_failures.pop(0)
            if current["approvals"]["finalBento"].get("status") != "approved":
                raise WorkflowError("Final Bento approval is stale")
            current["workflow"]["stage"] = "complete"

        def reopen(_root: Path, current: dict) -> None:
            calls.append("reopen")
            current["approvals"]["finalBento"] = {
                "status": "pending", "documentRevision": None, "htmlRevision": None,
                "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
            }
            current["workflow"]["stage"] = "bento_finalization"

        service = BentoLifecycleService(
            repository,
            state_loader=lambda _root: state,
            begin_content_review=begin_review,
            approve_content=approve_content,
            begin_finalization=begin_final,
            restart_finalization=restart_final,
            approve_final=approve_final,
            complete=complete,
            reopen_finalization=reopen,
            inspect_session=inspect,
            stop_editor=stop,
            launch_editor=launch,
            open_final=open_final,
        )
        return service, calls, editor

    def test_content_review_only_starts_from_bento_authoring(self) -> None:
        state = lifecycle_state("html_review")
        service, _, _ = self.service_for(Path.cwd(), state)
        with self.assertRaises(WorkflowError):
            service.start("content-review")

    def test_authoring_editor_stops_before_content_review_and_restarts(self) -> None:
        state = lifecycle_state()
        service, calls, editor = self.service_for(Path.cwd(), state, editor_mode="authoring")

        service.start("content-review")
        result = self.wait_for_terminal(service)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(state["workflow"]["stage"], "content_review")
        self.assertEqual(calls, ["stop:authoring", "begin-review", "launch"])
        self.assertEqual(editor["mode"], "authoring")

    def test_content_approval_binds_current_revisions_and_starts_finalization(self) -> None:
        state = lifecycle_state("content_review")
        service, calls, editor = self.service_for(Path.cwd(), state, editor_mode="authoring")

        service.start("content-approve")
        result = self.wait_for_terminal(service)

        approval = state["approvals"]["bentoContent"]
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(approval["documentRevision"], "sha256:document-current")
        self.assertEqual(approval["registryRevision"], "sha256:registry-current")
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")
        self.assertEqual(calls, ["stop:authoring", "approve-content", "begin-final", "launch"])
        self.assertEqual(editor["mode"], "finalization")

    def test_existing_final_uses_archive_restart_route(self) -> None:
        state = lifecycle_state("content_review")
        failure = WorkflowError(
            "Existing final artifacts differ from approved authoring content; use restart"
        )
        service, calls, _ = self.service_for(
            Path.cwd(), state, editor_mode="authoring", begin_finalization_failure=[failure],
        )

        service.start("content-approve")
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

        self.assertEqual(calls.count("approve-content"), 1)
        self.assertIn("restart-final", calls)
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")

    def test_final_editor_stops_before_approval_and_completion(self) -> None:
        state = lifecycle_state("bento_finalization")
        service, calls, editor = self.service_for(Path.cwd(), state, editor_mode="finalization")

        service.start("final-approve")
        result = self.wait_for_terminal(service)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(calls, ["stop:finalization", "approve-final", "complete"])
        self.assertIsNone(editor["mode"])
        self.assertEqual(state["workflow"]["stage"], "complete")

    def test_stale_final_approval_cannot_complete(self) -> None:
        state = lifecycle_state("bento_finalization")
        state["approvals"]["finalBento"] = {
            "status": "approved", "documentRevision": "sha256:stale-document",
            "htmlRevision": "sha256:stale-html", "registryRevision": "sha256:stale-registry",
            "runtimeFingerprint": "sha256:stale-runtime", "approvedAt": "2026-08-18T00:00:00Z",
        }
        service, calls, _ = self.service_for(
            Path.cwd(), state, editor_mode="finalization",
            complete_failure=[WorkflowError("Final Bento approval is stale")],
        )

        service.start("final-approve")
        result = self.wait_for_terminal(service)

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.retryable)
        self.assertNotIn("approve-final", calls)
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")

    def test_complete_reopen_invalidates_approval_and_restarts_final_editor(self) -> None:
        state = lifecycle_state("complete")
        state["approvals"]["finalBento"] = {
            "status": "approved", "documentRevision": "sha256:document",
            "htmlRevision": "sha256:html", "registryRevision": "sha256:registry",
            "runtimeFingerprint": "sha256:runtime", "approvedAt": "2026-08-18T00:00:00Z",
        }
        service, calls, editor = self.service_for(Path.cwd(), state)

        service.start("final-reopen")
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

        self.assertEqual(calls, ["reopen", "launch"])
        self.assertEqual(state["approvals"]["finalBento"]["status"], "pending")
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")
        self.assertEqual(editor["mode"], "finalization")

    def test_concurrent_action_is_rejected(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        state = lifecycle_state()
        service, _, _ = self.service_for(
            Path.cwd(), state, editor_mode="authoring", blocking_stop=(entered, release),
        )
        service.start("content-review")
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(WorkflowError, "すでに実行中"):
                service.start("content-review")
        finally:
            release.set()
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

    def test_retry_does_not_repeat_completed_content_approval(self) -> None:
        state = lifecycle_state("content_review")
        service, calls, _ = self.service_for(
            Path.cwd(), state, editor_mode="authoring",
            begin_finalization_failure=[WorkflowError("temporary finalization failure")],
        )
        service.start("content-approve")
        failed = self.wait_for_terminal(service)
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.retryable)

        service.start("content-approve")
        self.assertEqual(self.wait_for_terminal(service).status, "succeeded")

        self.assertEqual(calls.count("approve-content"), 1)
        self.assertEqual(calls.count("begin-final"), 2)

    def test_failure_status_never_exposes_internal_error_details(self) -> None:
        state = lifecycle_state()
        service, _, _ = self.service_for(Path.cwd(), state, editor_mode="authoring")

        def unsafe_stop(_root: Path, _state: dict, _mode: str) -> None:
            raise RuntimeError("C:/private/final.html pid=123 sha256:secret")

        service._stop_editor = unsafe_stop  # type: ignore[method-assign]
        service.start("content-review")
        result = self.wait_for_terminal(service)
        payload = result.model_dump_json()

        self.assertEqual(result.status, "failed")
        for secret in ("C:/private", "pid=", "sha256:", "revision", "digest"):
            self.assertNotIn(secret, payload)

    def test_malformed_or_foreign_editor_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = repository / "output"
            output.mkdir()
            session_path = output / "work-editor-session.json"
            session_path.write_text("[]\n", encoding="utf-8")
            (output / "work-editor.pid").write_text("123", encoding="ascii")
            with self.assertRaises(WorkflowError):
                inspect_work_editor_session(repository, lifecycle_state())

            session_path.write_text(json.dumps({
                "format": "bento/work-editor-session/v1",
                "pid": 123,
                "repository": str(repository / "foreign"),
                "host": "127.0.0.1",
                "port": 8765,
                "url": "http://127.0.0.1:8765/",
                "mode": "authoring",
            }), encoding="utf-8")
            with self.assertRaises(WorkflowError):
                inspect_work_editor_session(repository, lifecycle_state())


class BentoLifecycleApiTests(unittest.TestCase):
    def test_concurrent_api_action_returns_conflict(self) -> None:
        state = lifecycle_state("bento_authoring")
        entered = threading.Event()
        release = threading.Event()

        def stop(_root: Path, _state: dict, _mode: str) -> None:
            entered.set()
            release.wait(5)

        service = BentoLifecycleService(
            Path.cwd(), state_loader=lambda _root: state,
            inspect_session=lambda _root, _state: WorkEditorSession(
                mode="authoring", url="http://127.0.0.1:8765/",
            ),
            stop_editor=stop,
            begin_content_review=lambda _root, current: current["workflow"].update(stage="content_review"),
            launch_editor=lambda _root: None,
        )
        client = TestClient(create_app(
            Path.cwd(), frontend_dist=Path.cwd() / "missing", lifecycle_service=service,
        ))
        first = client.post("/api/bento/content/review", json={"confirmed": True})
        self.assertEqual(first.status_code, 202)
        self.assertTrue(entered.wait(2))
        try:
            duplicate = client.post("/api/bento/content/review", json={"confirmed": True})
            self.assertEqual(duplicate.status_code, 409)
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            terminal = client.get("/api/bento/lifecycle/status").json()
            if terminal["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(terminal["status"], "succeeded")

    def test_api_requires_exact_confirmation_and_returns_conflicts(self) -> None:
        state = lifecycle_state("html_review")
        service = BentoLifecycleService(
            Path.cwd(), state_loader=lambda _root: copy.deepcopy(state),
            inspect_session=lambda _root, _state: None,
        )
        client = TestClient(create_app(
            Path.cwd(), frontend_dist=Path.cwd() / "missing", lifecycle_service=service,
        ))

        for body in ({}, {"confirmed": False}, {"confirmed": True, "extra": "forbidden"}):
            with self.subTest(body=body):
                self.assertEqual(client.post("/api/bento/content/review", json=body).status_code, 422)
        conflict = client.post("/api/bento/content/review", json={"confirmed": True})
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("outputs", conflict.text)
        status = client.get("/api/bento/lifecycle/status")
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("documentRevision", status.text)

    def test_all_lifecycle_post_routes_return_202(self) -> None:
        cases = [
            ("bento_authoring", "/api/bento/content/review", "authoring"),
            ("content_review", "/api/bento/content/approve", "authoring"),
            ("bento_finalization", "/api/bento/final/approve", "finalization"),
            ("complete", "/api/bento/final/reopen", None),
            ("complete", "/api/bento/final/open", None),
        ]
        for stage, route, mode in cases:
            with self.subTest(route=route), tempfile.TemporaryDirectory() as temporary:
                state = lifecycle_state(stage)
                editor = {"mode": mode}

                def inspect(_root: Path, _state: dict):
                    return WorkEditorSession(mode=editor["mode"], url="http://127.0.0.1:8765/") if editor["mode"] else None

                service = BentoLifecycleService(
                    Path(temporary), state_loader=lambda _root: state, inspect_session=inspect,
                    stop_editor=lambda *_args: editor.update(mode=None),
                    launch_editor=lambda _root: None, open_final=lambda _root: None,
                    begin_content_review=lambda _root, current: current["workflow"].update(stage="content_review"),
                    approve_content=lambda _root, current: current["approvals"].update(bentoContent={
                        "status": "approved", "documentRevision": "d", "registryRevision": "r", "approvalDigest": "a",
                    }),
                    begin_finalization=lambda _root, current: current["workflow"].update(stage="bento_finalization"),
                    approve_final=lambda _root, current: current["approvals"].update(finalBento={
                        "status": "approved", "documentRevision": "d", "htmlRevision": "h",
                        "registryRevision": "r", "runtimeFingerprint": "f",
                    }),
                    complete=lambda _root, current: current["workflow"].update(stage="complete"),
                    reopen_finalization=lambda _root, current: current["workflow"].update(stage="bento_finalization"),
                )
                client = TestClient(create_app(
                    Path.cwd(), frontend_dist=Path.cwd() / "missing", lifecycle_service=service,
                ))
                self.assertEqual(client.post(route, json={"confirmed": True}).status_code, 202)


if __name__ == "__main__":
    unittest.main()
