from __future__ import annotations

import hmac
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bento_converter.errors import BentoConverterError
from bento_converter.visual_planning import load_visual_plan
from scripts.deck_workflow import (
    PLAN_FILES,
    VISUAL_PLAN_RELATIVE,
    PlanningRevisionConflict,
    WorkflowError,
    command_approve_plan,
    command_initialize,
    command_submit_plan,
    load_state,
    planning_action_guard,
    planning_is_ready,
    planning_review_signature,
)

from app.backend.models.view_models import (
    StoryboardDocument,
    StoryboardDocumentSection,
    StoryboardResponse,
    StoryboardSection,
    StoryboardSlide,
    StoryboardVisual,
)


MAX_PLANNING_BYTES = 2 * 1024 * 1024
SECTION_HEADING = re.compile(r"^##\s+Section\s+\d+\s*:\s*(.+?)\s*$", re.IGNORECASE)
SLIDE_HEADING = re.compile(r"^###\s+Slide\s+(\d+)\s*[—–-]\s*(.+?)\s*$", re.IGNORECASE)
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
MARKDOWN_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def _plain_inline(value: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"(`{1,3}|\*\*|__|~~)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _read_optional_text(path: Path) -> str:
    if not path.is_file():
        return ""
    payload = path.read_bytes()
    if len(payload) > MAX_PLANNING_BYTES:
        raise WorkflowError("Storyboard document is too large to display safely")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkflowError("Storyboard document is not valid UTF-8") from exc


def _document(text: str, *, fallback_title: str) -> StoryboardDocument:
    title = fallback_title
    sections: list[StoryboardDocumentSection] = []
    current = StoryboardDocumentSection(title="概要")
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            current.paragraphs.append(_plain_inline(" ".join(paragraph)))
            paragraph.clear()

    def flush_section() -> None:
        flush_paragraph()
        if current.paragraphs or current.bullets or current.title != "概要":
            sections.append(current.model_copy(deep=True))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = MARKDOWN_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = _plain_inline(heading.group(2))
            if level == 1 and title == fallback_title:
                title = heading_text or fallback_title
                continue
            flush_section()
            current = StoryboardDocumentSection(title=heading_text or "本文")
            continue
        bullet = MARKDOWN_BULLET.match(raw_line)
        if bullet:
            flush_paragraph()
            value = _plain_inline(bullet.group(1))
            if value:
                current.bullets.append(value)
            continue
        if not line:
            flush_paragraph()
        else:
            paragraph.append(line)
    flush_section()
    return StoryboardDocument(title=title, sections=sections)


@dataclass
class _ParsedSlide:
    number: int
    title: str
    points: list[str] = field(default_factory=list)


@dataclass
class _ParsedSection:
    identifier: str
    slides: list[_ParsedSlide] = field(default_factory=list)


def _slide_plan_sections(text: str) -> list[_ParsedSection]:
    sections: list[_ParsedSection] = []
    current_section: _ParsedSection | None = None
    current_slide: _ParsedSlide | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        section = SECTION_HEADING.match(line)
        if section:
            current_section = _ParsedSection(identifier=_plain_inline(section.group(1)))
            sections.append(current_section)
            current_slide = None
            continue
        slide = SLIDE_HEADING.match(line)
        if slide:
            if current_section is None:
                current_section = _ParsedSection(identifier="storyboard")
                sections.append(current_section)
            current_slide = _ParsedSlide(number=int(slide.group(1)), title=_plain_inline(slide.group(2)))
            current_section.slides.append(current_slide)
            continue
        if current_slide is not None:
            bullet = MARKDOWN_BULLET.match(raw_line)
            if bullet:
                point = _plain_inline(bullet.group(1))
                if point:
                    current_slide.points.append(point)
    return sections


class StoryboardService:
    """Read planning artifacts and delegate every transition to deck_workflow."""

    def __init__(
        self,
        repository: str | Path,
        *,
        state_loader: Callable[[Path], dict[str, Any]] = load_state,
        initialize: Callable[[Path, dict[str, Any]], None] = command_initialize,
        submit: Callable[..., None] = command_submit_plan,
        approve: Callable[..., None] = command_approve_plan,
    ):
        self.repository = Path(repository).resolve()
        self._state_loader = state_loader
        self._initialize = initialize
        self._submit = submit
        self._approve = approve
        self._action_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._token = ""
        self._token_signature = ""

    def _artifact_path(self, relative: str | Path) -> Path:
        path = (self.repository / relative).resolve()
        try:
            path.relative_to(self.repository)
        except ValueError as exc:
            raise WorkflowError("Storyboard configuration contains an unsafe document location") from exc
        return path

    def _signature(self, state: dict[str, Any]) -> str:
        return planning_review_signature(self.repository, state)

    def _action_token(self, state: dict[str, Any]) -> str:
        signature = self._signature(state)
        with self._token_lock:
            if signature != self._token_signature:
                self._token_signature = signature
                self._token = secrets.token_urlsafe(32)
            return self._token

    def _require_token(self, state: dict[str, Any], supplied: str) -> None:
        if not hmac.compare_digest(self._action_token(state), supplied):
            raise WorkflowError("構成案が更新されています。最新のStoryboardを読み直してください。")

    def _visuals(self) -> list[dict[str, Any]]:
        path = self._artifact_path(VISUAL_PLAN_RELATIVE)
        if not path.is_file():
            return []
        try:
            return list(load_visual_plan(path)["slides"])
        except BentoConverterError:
            return []

    def _sections(self, state: dict[str, Any], slide_plan_text: str) -> list[StoryboardSection]:
        parsed = _slide_plan_sections(slide_plan_text)
        visuals = self._visuals()
        visual_by_id = {str(entry["id"]): entry for entry in visuals}
        visual_order = [str(entry["id"]) for entry in visuals]
        parsed_slides = [slide for section in parsed for slide in section.slides]
        inferred_visual_ids: dict[int, str] = {}
        if (
            len(visual_order) == len(parsed_slides)
            and len(set(visual_order)) == len(visual_order)
            and [slide.number for slide in parsed_slides] == list(range(1, len(parsed_slides) + 1))
        ):
            inferred_visual_ids = {
                id(slide): visual_order[slide.number - 1] for slide in parsed_slides
            }
        single = state.get("schemaVersion") == 2 and state.get("authoring", {}).get("mode") != "modular"
        units = state.get("sections") if single else state.get("chapters")
        unit_items = list((units or {}).items())
        if not unit_items:
            unit_items = [(item.identifier or f"section-{index}", {"title": item.identifier}) for index, item in enumerate(parsed, start=1)]

        parsed_by_id: dict[str, list[int]] = {}
        for parsed_index, item in enumerate(parsed):
            if item.identifier:
                parsed_by_id.setdefault(item.identifier.casefold(), []).append(parsed_index)
        assignments: list[int | None] = [None] * len(unit_items)
        used_indices: set[int] = set()
        for unit_index, (unit_id, _entry) in enumerate(unit_items):
            candidates = [
                index for index in parsed_by_id.get(str(unit_id).casefold(), [])
                if index not in used_indices
            ]
            if len(candidates) == 1:
                assignments[unit_index] = candidates[0]
                used_indices.add(candidates[0])
        remaining = iter(index for index in range(len(parsed)) if index not in used_indices)
        for unit_index, assignment in enumerate(assignments):
            if assignment is None:
                fallback = next(remaining, None)
                assignments[unit_index] = fallback
                if fallback is not None:
                    used_indices.add(fallback)

        def visual_for(slide_id: str) -> StoryboardVisual | None:
            visual_entry = visual_by_id.get(slide_id)
            if not visual_entry:
                return None
            visual_value = visual_entry["visual"]
            return StoryboardVisual(
                recommended=bool(visual_value["recommended"]),
                type=str(visual_value["type"]),
                intent=str(visual_value.get("intent")) if visual_value.get("intent") else None,
                purpose=str(visual_entry.get("purpose")) if visual_entry.get("purpose") else None,
            )

        result: list[StoryboardSection] = []
        for unit_index, (unit_id, entry_value) in enumerate(unit_items):
            entry = entry_value if isinstance(entry_value, dict) else {}
            matched_index = assignments[unit_index]
            matched = parsed[matched_index] if matched_index is not None else None
            section_slides = matched.slides if matched else []
            planned_ids = entry.get("slideIds") if isinstance(entry.get("slideIds"), list) else []
            slides: list[StoryboardSlide] = []
            for local_index, parsed_slide in enumerate(section_slides):
                slide_id = str(planned_ids[local_index]) if local_index < len(planned_ids) else ""
                if not slide_id:
                    slide_id = inferred_visual_ids.get(id(parsed_slide), "")
                if not slide_id:
                    slide_id = f"storyboard-slide-{unit_id}-{parsed_slide.number}"
                section_title = str(entry.get("title") or (matched.identifier if matched else unit_id))
                slides.append(StoryboardSlide(
                    id=slide_id,
                    number=parsed_slide.number,
                    title=parsed_slide.title,
                    points=parsed_slide.points,
                    sectionId=str(unit_id),
                    sectionTitle=section_title,
                    visual=visual_for(slide_id),
                ))
            result.append(StoryboardSection(
                id=str(unit_id),
                title=str(entry.get("title") or (matched.identifier if matched else unit_id)),
                slides=slides,
            ))

        for parsed_index, item in enumerate(parsed):
            if parsed_index in used_indices:
                continue
            slides = []
            for parsed_slide in item.slides:
                slide_id = inferred_visual_ids.get(
                    id(parsed_slide), f"storyboard-slide-{item.identifier}-{parsed_slide.number}",
                )
                slides.append(StoryboardSlide(
                    id=slide_id, number=parsed_slide.number, title=parsed_slide.title,
                    points=parsed_slide.points, sectionId=item.identifier, sectionTitle=item.identifier,
                    visual=visual_for(slide_id),
                ))
            result.append(StoryboardSection(id=item.identifier, title=item.identifier, slides=slides))
        return result

    def view(self) -> StoryboardResponse:
        state = self._state_loader(self.repository)
        stage = str(state["workflow"]["stage"])
        request_relative = state.get("project", {}).get("request")
        request_text = _read_optional_text(self._artifact_path(request_relative)) if isinstance(request_relative, str) and request_relative else ""
        explanation = _read_optional_text(self._artifact_path(PLAN_FILES["explanationPolicy"]))
        story = _read_optional_text(self._artifact_path(PLAN_FILES["storyOutline"]))
        slide_plan = _read_optional_text(self._artifact_path(PLAN_FILES["slidePlan"]))
        ready = planning_is_ready(self.repository, state)
        next_actions = {
            "initialized": "資料を確認して構成作成を開始します。",
            "planning": "説明方針、全体ストーリー、スライド構成を確認して提出します。",
            "awaiting_plan_approval": "構成案を確認し、明示的な承認後にHTML制作へ進みます。",
            "html_authoring": "構成案は承認済みです。HTMLデザインの準備を待っています。",
        }
        return StoryboardResponse(
            stage=stage,
            request=_document(request_text, fallback_title="依頼内容"),
            explanationPolicy=_document(explanation, fallback_title="説明方針"),
            storyOutline=_document(story, fallback_title="全体ストーリー"),
            slidePlan=_document(slide_plan, fallback_title="スライド構成"),
            sections=self._sections(state, slide_plan),
            canInitialize=stage == "initialized",
            canSubmit=stage == "planning" and ready,
            canApprove=stage == "awaiting_plan_approval" and ready,
            nextActionLabel=next_actions.get(stage, "Storyboardの確認操作は完了しています。"),
            actionToken=self._action_token(state),
        )

    def _run_action(
        self, *, expected_stage: str, action_token: str,
        command: Callable[..., None], failure_message: str, protect_planning: bool = False,
    ) -> StoryboardResponse:
        with self._action_lock:
            try:
                state = self._state_loader(self.repository)
            except WorkflowError as exc:
                raise WorkflowError(failure_message) from exc
            if state["workflow"]["stage"] != expected_stage:
                raise WorkflowError("現在の段階ではこのStoryboard操作を実行できません。")
            if protect_planning:
                with planning_action_guard(self.repository, state) as lease:
                    if state["workflow"]["stage"] != expected_stage:
                        raise WorkflowError("現在の段階ではこのStoryboard操作を実行できません。")
                    self._require_token(state, action_token)
                    signature = self._signature(state)
                    try:
                        command(
                            self.repository,
                            state,
                            expected_planning_signature=signature,
                            inherited_writer_lease=lease,
                        )
                    except PlanningRevisionConflict:
                        raise
                    except WorkflowError as exc:
                        raise WorkflowError(failure_message) from exc
            else:
                self._require_token(state, action_token)
                try:
                    command(self.repository, state)
                except WorkflowError as exc:
                    raise WorkflowError(failure_message) from exc
            return self.view()

    def initialize(self, *, action_token: str) -> StoryboardResponse:
        return self._run_action(
            expected_stage="initialized", action_token=action_token, command=self._initialize,
            failure_message="一次資料を特定できませんでした。利用する資料を1件に整理して再試行してください。",
        )

    def submit(self, *, action_token: str) -> StoryboardResponse:
        return self._run_action(
            expected_stage="planning", action_token=action_token, command=self._submit,
            failure_message="構成案を提出できませんでした。説明方針、ストーリー、スライド構成、section構成を確認してください。",
            protect_planning=True,
        )

    def approve(self, *, action_token: str) -> StoryboardResponse:
        return self._run_action(
            expected_stage="awaiting_plan_approval", action_token=action_token, command=self._approve,
            failure_message="構成案を承認できませんでした。最新の構成内容を確認して再試行してください。",
            protect_planning=True,
        )
