from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from scripts.deck_workflow import (
    WorkflowError,
    command_approve_content,
    command_approve_final,
    command_begin_content_review,
    command_begin_finalization,
    command_complete,
    command_reopen_finalization,
    command_restart_finalization_from_authoring,
    _effective_state,
    load_state,
)

from app.backend.models.view_models import (
    LifecycleAction,
    LifecyclePhase,
    LifecycleStatusResponse,
)
from app.backend.services.editor_session_service import (
    EditorMode,
    WorkEditorSession,
    inspect_work_editor_session,
    launch_existing_work_editor,
    open_existing_completed_deck,
    stop_existing_work_editor,
)


LOGGER = logging.getLogger(__name__)
StateLoader = Callable[[Path], dict[str, Any]]
WorkflowCommand = Callable[[Path, dict[str, Any]], None]
RestartCommand = Callable[..., None]
SessionInspector = Callable[[Path, dict[str, Any]], WorkEditorSession | None]
EditorStopper = Callable[[Path, dict[str, Any], EditorMode], None]
RepositoryAction = Callable[[Path], None]


def load_lifecycle_state(repository: Path) -> dict[str, Any]:
    """Reload state with the workflow's revision-bound approval invalidation applied."""

    state = load_state(repository)
    return _effective_state(repository, state)


class BentoLifecycleService:
    """Coordinate Bento approval/finalization through existing safe workflow boundaries."""

    _active_lock = threading.Lock()
    _active_repositories: set[str] = set()
    _step_counts: dict[LifecycleAction, int] = {
        "content-review": 3,
        "content-approve": 4,
        "final-approve": 3,
        "final-reopen": 2,
        "final-open": 1,
    }

    def __init__(
        self,
        repository: str | Path,
        *,
        state_loader: StateLoader = load_lifecycle_state,
        begin_content_review: WorkflowCommand = command_begin_content_review,
        approve_content: WorkflowCommand = command_approve_content,
        begin_finalization: WorkflowCommand = command_begin_finalization,
        restart_finalization: RestartCommand = command_restart_finalization_from_authoring,
        approve_final: WorkflowCommand = command_approve_final,
        complete: WorkflowCommand = command_complete,
        reopen_finalization: WorkflowCommand = command_reopen_finalization,
        inspect_session: SessionInspector = inspect_work_editor_session,
        stop_editor: EditorStopper = stop_existing_work_editor,
        launch_editor: RepositoryAction = launch_existing_work_editor,
        open_final: RepositoryAction = open_existing_completed_deck,
    ) -> None:
        self.repository = Path(repository).resolve()
        self._state_loader = state_loader
        self._begin_content_review = begin_content_review
        self._approve_content = approve_content
        self._begin_finalization = begin_finalization
        self._restart_finalization = restart_finalization
        self._approve_final = approve_final
        self._complete = complete
        self._reopen_finalization = reopen_finalization
        self._inspect_session = inspect_session
        self._stop_editor = stop_editor
        self._launch_editor = launch_editor
        self._open_final = open_final
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        state = self._state_loader(self.repository)
        stage = str(state["workflow"]["stage"])
        self._status = LifecycleStatusResponse(
            status="idle", stage=stage, completedSteps=0, totalSteps=1,
            message=self._idle_message(stage),
            availableActions=self._available_actions(state),
        )

    @property
    def _repository_key(self) -> str:
        return os.path.normcase(str(self.repository))

    def _state(self) -> tuple[dict[str, Any], str]:
        state = self._state_loader(self.repository)
        return state, str(state["workflow"]["stage"])

    @staticmethod
    def _content_approved(state: dict[str, Any]) -> bool:
        approval = state.get("approvals", {}).get("bentoContent")
        return isinstance(approval, dict) and approval.get("status") == "approved" and all(
            isinstance(approval.get(field), str) and approval[field]
            for field in ("documentRevision", "registryRevision", "approvalDigest")
        )

    @staticmethod
    def _final_approved(state: dict[str, Any]) -> bool:
        approval = state.get("approvals", {}).get("finalBento")
        return isinstance(approval, dict) and approval.get("status") == "approved" and all(
            isinstance(approval.get(field), str) and approval[field]
            for field in ("documentRevision", "htmlRevision", "registryRevision", "runtimeFingerprint")
        )

    def _available_actions(self, state: dict[str, Any]) -> list[LifecycleAction]:
        stage = str(state["workflow"]["stage"])
        if stage == "bento_authoring":
            return ["content-review"]
        if stage == "content_review":
            try:
                session = self._inspect_session(self.repository, state)
            except WorkflowError:
                return []
            return ["content-approve"] if session is not None else ["content-review"]
        if stage == "bento_finalization":
            if self._final_approved(state):
                return ["final-approve"]
            try:
                session = self._inspect_session(self.repository, state)
            except WorkflowError:
                return []
            return ["final-approve"] if session is not None else ["final-reopen"]
        if stage == "complete":
            return ["final-open", "final-reopen"]
        return []

    @staticmethod
    def _idle_message(stage: str) -> str:
        return {
            "bento_authoring": "BentoSlideの内容を編集できます。",
            "content_review": "BentoSlideの内容を確認できます。",
            "bento_finalization": "最終版の見た目を調整できます。",
            "complete": "BentoSlideは完成しています。",
        }.get(stage, "現在のワークフローではBento承認操作を利用できません。")

    def status(self) -> LifecycleStatusResponse:
        with self._lock:
            current = self._status.model_copy(deep=True)
        if current.status == "running":
            return current
        try:
            state, stage = self._state()
            current.stage = stage
            if current.status == "failed" and current.retryable and current.action is not None:
                current.availableActions = [current.action]
            else:
                current.availableActions = self._available_actions(state)
                if current.status == "idle":
                    current.message = self._idle_message(stage)
        except BaseException:
            current.availableActions = []
        return current

    def start(self, action: LifecycleAction) -> LifecycleStatusResponse:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WorkflowError("別のBento承認処理がすでに実行中です")
            state, stage = self._state()
            available = self._available_actions(state)
            retry = (
                self._status.status == "failed"
                and self._status.retryable
                and self._status.action == action
            )
            if action not in available and not (retry and self._can_resume(action, stage)):
                raise WorkflowError("現在のBentoSlide状態ではこの操作を開始できません")
            self._preflight(action, state, stage)
            with self._active_lock:
                if self._repository_key in self._active_repositories:
                    raise WorkflowError("別のBento承認処理がすでに実行中です")
                self._active_repositories.add(self._repository_key)

            phase = self._initial_phase(action)
            self._status = LifecycleStatusResponse(
                status="running", action=action, phase=phase, stage=stage,
                completedSteps=0, totalSteps=self._step_counts[action],
                message=self._phase_message(phase), availableActions=[],
            )
            thread = threading.Thread(
                target=self._run,
                args=(action,),
                name=f"bentoslide-lifecycle-{uuid.uuid4().hex[:8]}",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                with self._active_lock:
                    self._active_repositories.discard(self._repository_key)
                self._status = LifecycleStatusResponse(
                    status="failed", action=action, phase=phase, stage=stage,
                    completedSteps=0, totalSteps=self._step_counts[action],
                    message="Bento承認処理を開始できませんでした。",
                    error="バックグラウンド処理を開始できませんでした。",
                    retryable=True, availableActions=[action],
                )
                raise
            return self.status()

    def _preflight(self, action: LifecycleAction, state: dict[str, Any], stage: str) -> None:
        session = self._inspect_session(self.repository, state)
        expected: EditorMode | None = None
        if stage in {"bento_authoring", "content_review"}:
            expected = "authoring"
        elif stage == "bento_finalization":
            expected = "finalization"
        if session is not None and (expected is None or session.mode != expected):
            raise WorkflowError("Bento editor sessionが現在の操作と一致しません")
        if action == "final-reopen" and stage == "complete" and session is not None:
            raise WorkflowError("完成状態に予期しないBento editor sessionがあります")

    @staticmethod
    def _can_resume(action: LifecycleAction, stage: str) -> bool:
        return stage in {
            "content-review": {"bento_authoring", "content_review"},
            "content-approve": {"content_review", "bento_finalization"},
            "final-approve": {"bento_finalization"},
            "final-reopen": {"complete", "bento_finalization"},
            "final-open": {"complete"},
        }[action]

    @staticmethod
    def _initial_phase(action: LifecycleAction) -> LifecyclePhase:
        return {
            "content-review": "stopping-editor",
            "content-approve": "stopping-editor",
            "final-approve": "stopping-editor",
            "final-reopen": "reopening-final",
            "final-open": "opening-final",
        }[action]

    @staticmethod
    def _phase_message(phase: LifecyclePhase) -> str:
        return {
            "stopping-editor": "Bento編集画面を安全に停止しています",
            "validating-content": "BentoSlideの内容を検証しています",
            "approving-content": "現在の内容を承認しています",
            "initializing-final": "最終調整用の成果物を準備しています",
            "starting-editor": "Bento編集画面を起動しています",
            "approving-final": "現在の最終版を検証・承認しています",
            "completing": "完成状態へ移行しています",
            "reopening-final": "最終調整を再開しています",
            "opening-final": "完成版を開いています",
            "complete": "処理が完了しました",
        }[phase]

    def _update(self, action: LifecycleAction, phase: LifecyclePhase, completed: int) -> None:
        _, stage = self._state()
        with self._lock:
            self._status = LifecycleStatusResponse(
                status="running", action=action, phase=phase, stage=stage,
                completedSteps=completed, totalSteps=self._step_counts[action],
                message=self._phase_message(phase), availableActions=[],
            )

    def _run(self, action: LifecycleAction) -> None:
        try:
            {
                "content-review": self._run_content_review,
                "content-approve": self._run_content_approve,
                "final-approve": self._run_final_approve,
                "final-reopen": self._run_final_reopen,
                "final-open": self._run_final_open,
            }[action]()
            state, stage = self._state()
            with self._lock:
                self._status = LifecycleStatusResponse(
                    status="succeeded", action=action, phase="complete", stage=stage,
                    completedSteps=self._step_counts[action], totalSteps=self._step_counts[action],
                    message=self._success_message(action),
                    availableActions=self._available_actions(state),
                )
        except BaseException:
            LOGGER.exception("Bento lifecycle action failed")
            current = self.status()
            try:
                _, stage = self._state()
            except BaseException:
                stage = "unknown"
            retryable = self._can_resume(action, stage) if stage != "unknown" else False
            public_error = self._public_error(action)
            with self._lock:
                self._status = LifecycleStatusResponse(
                    status="failed", action=action, phase=current.phase, stage=stage,
                    completedSteps=current.completedSteps, totalSteps=self._step_counts[action],
                    message=public_error, error=public_error, retryable=retryable,
                    availableActions=[action] if retryable else [],
                )
        finally:
            with self._active_lock:
                self._active_repositories.discard(self._repository_key)

    def _run_content_review(self) -> None:
        state, stage = self._state()
        if stage == "bento_authoring":
            self._stop_editor(self.repository, state, "authoring")
            self._update("content-review", "validating-content", 1)
            state, stage = self._state()
            if stage != "bento_authoring":
                raise WorkflowError("Bento authoring state changed before content review")
            self._begin_content_review(self.repository, state)
            self._update("content-review", "starting-editor", 2)
            state, stage = self._state()
        if stage != "content_review":
            raise WorkflowError("Content review did not reach the expected workflow state")
        self._launch_editor(self.repository)

    def _run_content_approve(self) -> None:
        state, stage = self._state()
        if stage == "content_review":
            self._stop_editor(self.repository, state, "authoring")
            self._update("content-approve", "approving-content", 1)
            state, stage = self._state()
            if not self._content_approved(state):
                self._approve_content(self.repository, state)
            state, stage = self._state()
            if not self._content_approved(state):
                raise WorkflowError("Content approval was not bound to the current authoring artifacts")
            self._update("content-approve", "initializing-final", 2)
            try:
                self._begin_finalization(self.repository, state)
            except WorkflowError as exc:
                if not str(exc).startswith("Existing final artifacts differ from approved authoring content"):
                    raise
                state, stage = self._state()
                if stage != "content_review" or not self._content_approved(state):
                    raise WorkflowError("Existing final artifacts cannot be archived safely") from exc
                self._restart_finalization(
                    self.repository, state, confirmation="ARCHIVE-AND-RESTART-FINALIZATION",
                )
            self._update("content-approve", "starting-editor", 3)
            state, stage = self._state()
        if stage != "bento_finalization":
            raise WorkflowError("Finalization did not reach the expected workflow state")
        self._launch_editor(self.repository)

    def _run_final_approve(self) -> None:
        state, stage = self._state()
        if stage != "bento_finalization":
            raise WorkflowError("Final approval requires finalization")
        self._stop_editor(self.repository, state, "finalization")
        self._update("final-approve", "approving-final", 1)
        state, stage = self._state()
        if not self._final_approved(state):
            self._approve_final(self.repository, state)
        state, stage = self._state()
        if not self._final_approved(state):
            raise WorkflowError("Final approval was not bound to the current final artifacts")
        self._update("final-approve", "completing", 2)
        self._complete(self.repository, state)
        _, stage = self._state()
        if stage != "complete":
            raise WorkflowError("The BentoSlide workflow did not reach complete")

    def _run_final_reopen(self) -> None:
        state, stage = self._state()
        if stage == "complete":
            self._reopen_finalization(self.repository, state)
            self._update("final-reopen", "starting-editor", 1)
            state, stage = self._state()
        if stage != "bento_finalization" or self._final_approved(state):
            raise WorkflowError("Finalization was not reopened safely")
        self._launch_editor(self.repository)

    def _run_final_open(self) -> None:
        _, stage = self._state()
        if stage != "complete":
            raise WorkflowError("Only a completed BentoSlide can be opened")
        self._open_final(self.repository)

    @staticmethod
    def _success_message(action: LifecycleAction) -> str:
        return {
            "content-review": "内容確認を開始しました。",
            "content-approve": "内容を承認し、最終調整を開始しました。",
            "final-approve": "最終版を承認し、BentoSlideが完成しました。",
            "final-reopen": "最終調整を再開しました。",
            "final-open": "完成版を開きました。",
        }[action]

    @staticmethod
    def _public_error(action: LifecycleAction) -> str:
        return {
            "content-review": "内容確認を開始できませんでした。編集画面の状態を確認して再試行してください。",
            "content-approve": "内容承認または最終調整の準備を完了できませんでした。安全な途中状態から再試行できます。",
            "final-approve": "最終承認または完成処理を完了できませんでした。最終版を確認して再試行してください。",
            "final-reopen": "最終調整を再開できませんでした。完成版の状態を確認してください。",
            "final-open": "完成版を開けませんでした。完成状態を確認してください。",
        }[action]
