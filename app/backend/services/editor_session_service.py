from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scripts.deck_workflow import WorkflowError, load_state


LOGGER = logging.getLogger(__name__)
EditorMode = Literal["authoring", "finalization"]


@dataclass(frozen=True)
class WorkEditorSession:
    mode: EditorMode
    url: str


def _configured_path(repository: Path, state: dict[str, Any], field: str) -> Path:
    value = state.get("outputs", {}).get(field)
    if not isinstance(value, str) or not value:
        raise WorkflowError("The configured Bento editor artifact is unavailable")
    path = (repository / value).resolve()
    try:
        path.relative_to(repository)
    except ValueError as exc:
        raise WorkflowError("The configured Bento editor artifact is outside the repository") from exc
    return path


def inspect_work_editor_session(
    repository: str | Path,
    state: dict[str, Any],
) -> WorkEditorSession | None:
    """Validate the recorded editor identity without exposing its internal fields."""

    root = Path(repository).resolve()
    session_path = root / "output/work-editor-session.json"
    pid_path = root / "output/work-editor.pid"
    if not session_path.is_file() and not pid_path.is_file():
        return None
    if not session_path.is_file() or not pid_path.is_file():
        raise WorkflowError("The Bento editor session cannot be verified safely")
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("session payload must be an object")
        recorded_pid = int(pid_path.read_text(encoding="ascii").strip())
        session_pid = int(payload.get("pid"))
        session_repository = Path(str(payload.get("repository") or "")).resolve()
        port = int(payload.get("port"))
        mode = str(payload.get("mode"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise WorkflowError("The Bento editor session cannot be verified safely") from exc

    if (
        payload.get("format") != "bento/work-editor-session/v1"
        or recorded_pid != session_pid
        or session_pid < 1
        or session_repository != root
        or payload.get("host") != "127.0.0.1"
        or port != int(state.get("preview", {}).get("bentoPort") or 8765)
        or payload.get("url") != f"http://127.0.0.1:{port}/"
        or mode not in {"authoring", "finalization"}
    ):
        raise WorkflowError("The Bento editor session cannot be verified safely")

    if mode == "authoring":
        expected_target = _configured_path(root, state, "authoringHtml")
        expected_registry = _configured_path(root, state, "authoringRegistry")
        actual_registry = payload.get("targetRegistry")
    else:
        expected_target = _configured_path(root, state, "finalHtml")
        expected_registry = _configured_path(root, state, "finalRegistry")
        actual_registry = payload.get("registry")
    try:
        actual_target = Path(str(payload.get("target") or "")).resolve()
        resolved_registry = Path(str(actual_registry or "")).resolve()
    except (OSError, ValueError) as exc:
        raise WorkflowError("The Bento editor session cannot be verified safely") from exc
    if actual_target != expected_target or resolved_registry != expected_registry:
        raise WorkflowError("The Bento editor session does not match the current workflow")

    return WorkEditorSession(mode=mode, url=f"http://127.0.0.1:{port}/")


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
            or payload.get("managedEngine") not in {None, "", "work-editor"}
        ):
            return
        payload["managedEngine"] = "work-editor"
        temporary = app_session.with_name(f".{app_session.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(temporary, app_session)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        LOGGER.warning("Could not record the Work editor as App-managed", exc_info=True)


def _run_workspace_script(
    repository: Path, script_name: str, *, timeout: int, no_clipboard: bool = False,
) -> None:
    if os.name != "nt":
        raise WorkflowError("Bento editor lifecycle actions require the Windows launcher")
    arguments = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(repository / "scripts" / script_name),
        ]
    if no_clipboard:
        arguments.append("-NoClipboard")
    # A WMI-detached editor can inherit anonymous pipe handles. Capturing with
    # subprocess.PIPE would then make communicate() wait for the editor itself.
    with tempfile.TemporaryFile() as capture:
        completed = subprocess.run(
            arguments,
            cwd=repository,
            stdout=capture,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if completed.returncode != 0:
        raise WorkflowError("The existing Bento editor launcher did not complete safely")


def stop_existing_work_editor(repository: Path, state: dict[str, Any], expected_mode: EditorMode) -> None:
    session = inspect_work_editor_session(repository, state)
    if session is None:
        return
    if session.mode != expected_mode:
        raise WorkflowError("The Bento editor session does not match the requested action")
    _run_workspace_script(repository, "stop_bento_editor.ps1", timeout=45)
    if (repository / "output/work-editor-session.json").exists() or (
        repository / "output/work-editor.pid"
    ).exists():
        raise WorkflowError("The Bento editor did not stop safely")


def launch_existing_work_editor(repository: Path) -> None:
    """Use the stage-aware launcher and then verify its configured session."""

    editor_session = repository / "output/work-editor-session.json"
    editor_existed = editor_session.is_file()
    _run_workspace_script(
        repository, "start_deck_workspace.ps1", timeout=90, no_clipboard=True,
    )
    state = load_state(repository)
    stage = str(state["workflow"]["stage"])
    expected_mode: EditorMode = "finalization" if stage == "bento_finalization" else "authoring"
    session = inspect_work_editor_session(repository, state)
    if session is None or session.mode != expected_mode:
        raise WorkflowError("The Bento editor did not start for the current workflow")
    _record_managed_editor(repository, editor_existed=editor_existed)


def open_existing_completed_deck(repository: Path) -> None:
    """Open only the configured completed artifact through the stage-aware launcher."""

    _run_workspace_script(
        repository, "start_deck_workspace.ps1", timeout=45, no_clipboard=True,
    )
