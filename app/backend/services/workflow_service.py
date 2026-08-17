from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bento_converter.section_approval import read_html_deck_outline
from scripts.deck_workflow import load_state, user_status_summary

from app.backend.models.view_models import (
    ProjectInfo,
    ProjectResponse,
    SlideItem,
    SlidesResponse,
    StateResponse,
    UiMode,
)
from app.backend.services.editor_session_service import inspect_work_editor_session
from scripts.deck_workflow import WorkflowError


STAGE_TO_MODE: dict[str, UiMode] = {
    "initialized": "storyboard",
    "planning": "storyboard",
    "awaiting_plan_approval": "storyboard",
    "html_authoring": "html-design",
    "html_review": "html-design",
    "ready_for_conversion": "converting",
    "converting": "converting",
    "bento_validation": "bento-edit",
    "bento_authoring": "bento-edit",
    "content_review": "bento-edit",
    "bento_finalization": "final-edit",
    "complete": "complete",
    "blocked": "blocked",
}


def ui_mode_for_stage(stage: str) -> UiMode:
    return STAGE_TO_MODE.get(stage, "blocked")


def _active_proposal(state: dict[str, Any]) -> dict[str, Any] | None:
    proposal = state.get("authoring", {}).get("htmlChange")
    if isinstance(proposal, dict) and proposal.get("status") in {"proposed", "approved"}:
        return proposal
    return None


def verified_local_session_url(
    repository: Path,
    *,
    filename: str,
    expected_format: str,
    expected_port: int,
) -> str | None:
    session_path = repository / "output" / filename
    try:
        session = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(session, dict) or session.get("format") != expected_format:
        return None
    try:
        session_repository = Path(str(session.get("repository") or "")).resolve()
        session_port = int(session.get("port"))
    except (OSError, TypeError, ValueError):
        return None
    expected_url = f"http://127.0.0.1:{expected_port}/"
    if session_repository != repository.resolve() or session_port != expected_port or session.get("url") != expected_url:
        return None
    return expected_url


class WorkflowService:
    def __init__(self, repository: str | Path):
        self.repository = Path(repository).resolve()

    def state(self) -> dict[str, Any]:
        return load_state(self.repository)

    def project(self) -> ProjectResponse:
        state = self.state()
        project = state["project"]
        return ProjectResponse(project=ProjectInfo(title=str(project["title"]), kind=str(project["kind"])))

    def state_view(self) -> StateResponse:
        state = self.state()
        stage = str(state["workflow"]["stage"])
        summary = user_status_summary(state)
        bento_stages = {"bento_authoring", "content_review", "bento_finalization"}
        editor_url = None
        if stage in bento_stages:
            try:
                session = inspect_work_editor_session(self.repository, state)
                expected_mode = "finalization" if stage == "bento_finalization" else "authoring"
                if session is not None and session.mode == expected_mode:
                    editor_url = session.url
            except WorkflowError:
                editor_url = None
        return StateResponse(
            mode=ui_mode_for_stage(stage),
            stage=stage,
            statusLabel=str(summary["current"]),
            nextActionLabel=str(summary["next"]),
            canConvert=stage == "ready_for_conversion",
            canEditBento=stage in bento_stages,
            hasCandidate=_active_proposal(state) is not None,
            isBlocked=stage == "blocked",
            bentoEditorUrl=editor_url,
        )

    def html_source(self, state: dict[str, Any] | None = None) -> Path:
        current = state or self.state()
        relative = current.get("authoring", {}).get("entryHtml")
        if not isinstance(relative, str) or not relative:
            raise FileNotFoundError("The current project does not have a single HTML authoring source")
        source = (self.repository / relative).resolve()
        source.relative_to(self.repository)
        if not source.is_file():
            raise FileNotFoundError(f"HTML authoring source does not exist: {source}")
        return source

    def slides(self, *, view: str = "current") -> SlidesResponse:
        state = self.state()
        source = self.html_source(state)
        if view == "candidate":
            proposal = _active_proposal(state)
            relative = proposal.get("candidateHtml") if proposal else None
            if not isinstance(relative, str):
                raise FileNotFoundError("There is no active candidate HTML")
            source = (self.repository / relative).resolve()
            source.relative_to(self.repository)
        outline = read_html_deck_outline(source)
        sections = state.get("sections", {})
        slides = [
            SlideItem(
                id=slide_id,
                title=outline.slide_titles.get(slide_id, slide_id),
                number=index,
                sectionTitle=str(sections.get(outline.slide_section_ids[slide_id], {}).get("title") or "") or None,
            )
            for index, slide_id in enumerate(outline.ordered_slide_ids, start=1)
        ]
        return SlidesResponse(view="candidate" if view == "candidate" else "current", slides=slides)
