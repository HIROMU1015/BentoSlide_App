from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from bento_converter.artifact_transaction import ArtifactTransactionStore
from bento_converter.html_pipeline import build_from_html
from scripts.deck_workflow import (
    WorkflowError,
    _repo_path,
    command_begin_authoring,
    command_mark_converted,
    command_prepare_conversion,
    load_state,
)

from app.backend.models.view_models import ConversionPhase, ConversionStatusResponse


LOGGER = logging.getLogger(__name__)
BuildFunction = Callable[..., Any]
StateLoader = Callable[[Path], dict[str, Any]]
WorkflowCommand = Callable[[Path, dict[str, Any]], None]
EditorLauncher = Callable[[Path], None]


def _record_managed_editor(repository: Path, *, editor_existed: bool) -> None:
    if editor_existed:
        return
    app_session = repository / "output/bentoslide-app-session.json"
    editor_session = repository / "output/work-editor-session.json"
    if not app_session.is_file() or not editor_session.is_file():
        return
    try:
        payload = json.loads(app_session.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(payload, dict)
            or payload.get("format") != "bento/application-session/v1"
            or Path(str(payload.get("repository") or "")).resolve() != repository.resolve()
            or payload.get("managedEngine") not in {None, ""}
        ):
            return
        payload["managedEngine"] = "work-editor"
        temporary = app_session.with_name(f".{app_session.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, app_session)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        LOGGER.warning("Could not record the Work editor as App-managed", exc_info=True)


def launch_existing_work_editor(repository: Path) -> None:
    """Use the existing workspace launcher; do not reproduce Work editor setup here."""

    if os.name != "nt":
        return
    script = repository / "scripts/start_deck_workspace.ps1"
    editor_session = repository / "output/work-editor-session.json"
    editor_existed = editor_session.is_file()
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-NoClipboard",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("The existing Bento Work editor launcher did not complete successfully")
    _record_managed_editor(repository, editor_existed=editor_existed)


class ConversionService:
    """Run one existing-engine conversion per repository without blocking HTTP."""

    _active_lock = threading.Lock()
    _active_repositories: set[str] = set()

    def __init__(
        self,
        repository: str | Path,
        *,
        state_loader: StateLoader = load_state,
        prepare_conversion: WorkflowCommand = command_prepare_conversion,
        build: BuildFunction = build_from_html,
        mark_converted: WorkflowCommand = command_mark_converted,
        begin_authoring: WorkflowCommand = command_begin_authoring,
        launch_editor: EditorLauncher = launch_existing_work_editor,
    ) -> None:
        self.repository = Path(repository).resolve()
        self._state_loader = state_loader
        self._prepare_conversion = prepare_conversion
        self._build = build
        self._mark_converted = mark_converted
        self._begin_authoring = begin_authoring
        self._launch_editor = launch_editor
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._status = self._initial_status()

    @property
    def _repository_key(self) -> str:
        return os.path.normcase(str(self.repository))

    def _initial_status(self) -> ConversionStatusResponse:
        state = self._state_loader(self.repository)
        stage = str(state["workflow"]["stage"])
        if stage == "converting":
            return ConversionStatusResponse(
                status="failed", phase="building", completedSteps=1,
                message="前回の変換が中断されました。再試行できます。",
                error="変換処理が完了する前にバックエンドが終了しました。",
                retryable=True,
            )
        if stage == "bento_validation":
            return ConversionStatusResponse(
                status="failed", phase="starting-authoring", completedSteps=3,
                message="Bento編集の準備が中断されました。再試行できます。",
                error="Bento編集へ移行する前にバックエンドが終了しました。",
                retryable=True,
            )
        return ConversionStatusResponse(
            status="idle", phase=None, completedSteps=0,
            message="BentoSlideへの変換を開始できます。",
        )

    def status(self) -> ConversionStatusResponse:
        with self._lock:
            return self._status.model_copy(deep=True)

    def start(self) -> ConversionStatusResponse:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WorkflowError("BentoSlideへの変換はすでに実行中です")
            state = self._state_loader(self.repository)
            stage = str(state["workflow"]["stage"])
            retry = self._status.status == "failed" and self._status.retryable
            allowed = stage == "ready_for_conversion" or (
                retry and stage in {"converting", "bento_validation", "bento_authoring"}
            )
            if not allowed:
                raise WorkflowError("BentoSlideへの変換はHTML全体の承認後に開始できます")

            with self._active_lock:
                if self._repository_key in self._active_repositories:
                    raise WorkflowError("BentoSlideへの変換はすでに実行中です")
                self._active_repositories.add(self._repository_key)

            phase, completed = self._resume_position(stage)
            self._status = ConversionStatusResponse(
                status="running", phase=phase, completedSteps=completed,
                message=self._phase_message(phase),
            )
            thread = threading.Thread(
                target=self._run,
                name=f"bentoslide-conversion-{uuid.uuid4().hex[:8]}",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                with self._active_lock:
                    self._active_repositories.discard(self._repository_key)
                self._status = ConversionStatusResponse(
                    status="failed", phase=phase, completedSteps=completed,
                    message="変換処理を開始できませんでした。",
                    error="バックグラウンド処理を開始できませんでした。",
                    retryable=True,
                )
                raise
            return self.status()

    @staticmethod
    def _resume_position(stage: str) -> tuple[ConversionPhase, int]:
        return {
            "ready_for_conversion": ("validating", 0),
            "converting": ("building", 1),
            "bento_validation": ("starting-authoring", 3),
            "bento_authoring": ("starting-authoring", 3),
        }[stage]

    @staticmethod
    def _phase_message(phase: ConversionPhase) -> str:
        return {
            "validating": "承認済みHTMLを確認しています",
            "building": "BentoSlideへ変換しています",
            "validating-output": "変換結果を検証しています",
            "starting-authoring": "Bento編集画面を準備しています",
            "complete": "BentoSlideへの変換が完了しました",
        }[phase]

    def _update(self, phase: ConversionPhase, completed: int) -> None:
        with self._lock:
            self._status = ConversionStatusResponse(
                status="running", phase=phase, completedSteps=completed,
                message=self._phase_message(phase),
            )

    def _state(self) -> tuple[dict[str, Any], str]:
        state = self._state_loader(self.repository)
        return state, str(state["workflow"]["stage"])

    def _build_arguments(self, state: dict[str, Any]) -> dict[str, Any]:
        authoring = state["authoring"]
        output = _repo_path(
            self.repository, state["outputs"]["generatedHtml"], field="outputs.generatedHtml",
        )
        arguments: dict[str, Any] = {
            "base_path": self.repository / "Bento_Slides.base.bento.html",
            "output_path": output,
            "browser_check": True,
            "incremental": False,
        }
        if authoring["mode"] == "modular":
            chapters = state.get("chapters", {})
            if not chapters:
                raise WorkflowError("Modular conversion has no registered chapters")
            html_directories = {
                _repo_path(self.repository, chapter["html"], field="chapters.html").parent
                for chapter in chapters.values()
            }
            registry_directories = {
                _repo_path(self.repository, chapter["registry"], field="chapters.registry").parent
                for chapter in chapters.values()
            }
            if len(html_directories) != 1 or len(registry_directories) != 1:
                raise WorkflowError("Registered modular sources must share one HTML and registry directory")
            arguments.update(
                html_dir=next(iter(html_directories)),
                registry_dir=next(iter(registry_directories)),
            )
        else:
            arguments.update(
                html_path=_repo_path(
                    self.repository, authoring["entryHtml"], field="authoring.entryHtml",
                ),
                registry_path=_repo_path(
                    self.repository, authoring["registry"], field="authoring.registry",
                ),
            )
        return arguments

    def _install_configured_registry(self, state: dict[str, Any]) -> None:
        generated_html = _repo_path(
            self.repository, state["outputs"]["generatedHtml"], field="outputs.generatedHtml",
        )
        built_registry = generated_html.parent / "diagnostics/merged-registry.json"
        configured_registry = _repo_path(
            self.repository, state["outputs"]["generatedRegistry"], field="outputs.generatedRegistry",
        )
        if configured_registry == built_registry:
            return
        payload = built_registry.read_bytes()
        store = ArtifactTransactionStore(self.repository, [configured_registry])

        def validate_registry_install() -> None:
            if configured_registry.read_bytes() != payload:
                raise WorkflowError("Configured generated registry differs from the conversion result")

        store.commit(
            {configured_registry: payload},
            operation="conversion-registry-install",
            validate_committed=validate_registry_install,
        )

    def _run(self) -> None:
        try:
            state, stage = self._state()
            if stage == "ready_for_conversion":
                self._update("validating", 0)
                self._prepare_conversion(self.repository, state)
                state, stage = self._state()

            if stage == "converting":
                self._update("building", 1)
                self._build(**self._build_arguments(state))
                state, _ = self._state()
                self._install_configured_registry(state)
                self._update("validating-output", 2)
                state, stage = self._state()
                self._mark_converted(self.repository, state)
                state, stage = self._state()

            if stage == "bento_validation":
                self._update("starting-authoring", 3)
                self._begin_authoring(self.repository, state)
                state, stage = self._state()

            if stage != "bento_authoring":
                raise WorkflowError("Conversion did not reach Bento authoring")
            self._update("starting-authoring", 3)
            self._launch_editor(self.repository)
            with self._lock:
                self._status = ConversionStatusResponse(
                    status="succeeded", phase="complete", completedSteps=4,
                    message=self._phase_message("complete"),
                )
        except BaseException as exc:
            LOGGER.exception("BentoSlide conversion failed")
            current = self.status()
            try:
                _, stage = self._state()
            except BaseException:
                stage = "unknown"
            retryable = stage in {
                "ready_for_conversion", "converting", "bento_validation", "bento_authoring",
            }
            phase = current.phase or "validating"
            public_error = self._public_error(phase)
            with self._lock:
                self._status = ConversionStatusResponse(
                    status="failed", phase=phase, completedSteps=current.completedSteps,
                    message=public_error, error=public_error, retryable=retryable,
                )
        finally:
            with self._active_lock:
                self._active_repositories.discard(self._repository_key)

    @staticmethod
    def _public_error(phase: ConversionPhase) -> str:
        return {
            "validating": "承認済みHTMLの再確認に失敗しました。HTML全体の承認状態を確認してください。",
            "building": "BentoSlideの生成に失敗しました。変換元HTMLとregistryを確認してください。",
            "validating-output": "生成結果の検証に失敗しました。変換ログを確認して再試行してください。",
            "starting-authoring": "Bento編集画面の準備に失敗しました。競合中の編集画面を確認して再試行してください。",
            "complete": "BentoSlideへの変換完了後の確認に失敗しました。",
        }[phase]
