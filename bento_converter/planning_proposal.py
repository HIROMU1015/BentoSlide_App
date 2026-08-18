"""Strict, revision-bound planning candidate parsing and impact analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import yaml

from .errors import BentoConverterError
from .visual_planning import validate_visual_plan


PLANNING_PROPOSAL_FORMAT = "bento/planning-proposal/v1"
PLANNING_AGENT_RESULT_FORMAT = "bento/planning-ai-result/v1"
PLANNING_CANDIDATE_FORMAT = "bento/planning-candidate/v1"
PLANNING_ARTIFACT_NAMES = (
    "explanation-policy",
    "story-outline",
    "slide-plan",
    "visual-plan",
)
PLANNING_ARTIFACT_FILENAMES = {
    "explanation-policy": "explanation-policy.md",
    "story-outline": "story-outline.md",
    "slide-plan": "slide-plan.md",
    "visual-plan": "visual-plan.yaml",
}
SECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLIDE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SECTION_HEADING = re.compile(r"^##\s+Section\s+(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE)
SLIDE_HEADING = re.compile(r"^###\s+Slide\s+(\d+)\s*[—–-]\s*(.+?)\s*$", re.IGNORECASE)
MARKDOWN_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


@dataclass(frozen=True)
class PlanningSlide:
    id: str
    number: int
    title: str
    points: tuple[str, ...]
    section_id: str
    section_title: str


@dataclass(frozen=True)
class PlanningSection:
    id: str
    title: str
    slide_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlanningCandidate:
    artifacts: dict[str, bytes]
    texts: dict[str, str]
    visual_plan: dict[str, Any]
    sections: tuple[PlanningSection, ...]
    slides: tuple[PlanningSlide, ...]
    signature: str


def _plain_inline(value: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"(`{1,3}|\*\*|__|~~)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _decode_utf8(name: str, payload: bytes) -> str:
    try:
        value = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BentoConverterError(f"Planning candidate {name} must be valid UTF-8") from exc
    if "\x00" in value:
        raise BentoConverterError(f"Planning candidate {name} must not contain NUL characters")
    return value


def _meaningful_markdown(text: str) -> bool:
    without_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    without_headings = re.sub(r"^\s*#+\s+.*$", "", without_comments, flags=re.MULTILINE)
    return bool(without_headings.strip())


def _normalize_sections(values: Sequence[Mapping[str, Any]]) -> tuple[PlanningSection, ...]:
    if not values:
        raise BentoConverterError("Planning candidate must contain at least one section")
    sections: list[PlanningSection] = []
    seen_sections: set[str] = set()
    seen_slides: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise BentoConverterError(f"Planning candidate sections[{index}] must be an object")
        if set(value) != {"id", "title", "slideIds"}:
            raise BentoConverterError(
                f"Planning candidate sections[{index}] must contain only id, title, and slideIds"
            )
        section_id = value.get("id")
        title = value.get("title")
        slide_ids = value.get("slideIds")
        if not isinstance(section_id, str) or not SECTION_ID_PATTERN.fullmatch(section_id):
            raise BentoConverterError(f"Planning candidate sections[{index}].id is invalid")
        if section_id in seen_sections:
            raise BentoConverterError(f"Planning candidate has duplicate section id: {section_id}")
        if not isinstance(title, str) or not title.strip() or title != title.strip():
            raise BentoConverterError(f"Planning candidate sections[{index}].title is invalid")
        if not isinstance(slide_ids, list) or not slide_ids:
            raise BentoConverterError(f"Planning candidate section {section_id} must contain slideIds")
        normalized_ids: list[str] = []
        for slide_id in slide_ids:
            if not isinstance(slide_id, str) or not SLIDE_ID_PATTERN.fullmatch(slide_id):
                raise BentoConverterError(f"Planning candidate section {section_id} has an invalid slide id")
            if slide_id in seen_slides:
                raise BentoConverterError(f"Planning candidate has duplicate slide id: {slide_id}")
            seen_slides.add(slide_id)
            normalized_ids.append(slide_id)
        seen_sections.add(section_id)
        sections.append(PlanningSection(section_id, title, tuple(normalized_ids)))
    return tuple(sections)


def _parse_slide_plan(text: str, sections: tuple[PlanningSection, ...]) -> tuple[PlanningSlide, ...]:
    parsed: list[tuple[str, list[dict[str, Any]]]] = []
    current: tuple[str, list[dict[str, Any]]] | None = None
    current_slide: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        section_match = SECTION_HEADING.match(line)
        if section_match:
            expected_number = len(parsed) + 1
            if int(section_match.group(1)) != expected_number:
                raise BentoConverterError("Planning candidate section numbers must be contiguous from 1")
            identifier = _plain_inline(section_match.group(2))
            current = (identifier, [])
            parsed.append(current)
            current_slide = None
            continue
        slide_match = SLIDE_HEADING.match(line)
        if slide_match:
            if current is None:
                raise BentoConverterError("Planning candidate slide must belong to an explicit section")
            title = _plain_inline(slide_match.group(2))
            if not title:
                raise BentoConverterError("Planning candidate slide title must not be blank")
            current_slide = {
                "number": int(slide_match.group(1)), "title": title, "points": [],
            }
            current[1].append(current_slide)
            continue
        if current_slide is not None:
            bullet = MARKDOWN_BULLET.match(raw_line)
            if bullet:
                point = _plain_inline(bullet.group(1))
                if point:
                    current_slide["points"].append(point)

    if len(parsed) != len(sections):
        raise BentoConverterError("Planning candidate slide plan and sections have different section counts")
    slides: list[PlanningSlide] = []
    for section_index, (identifier, parsed_slides) in enumerate(parsed):
        section = sections[section_index]
        if identifier != section.id:
            raise BentoConverterError("Planning candidate slide plan section order or IDs do not match sections")
        if len(parsed_slides) != len(section.slide_ids):
            raise BentoConverterError(
                f"Planning candidate section {section.id} slide count does not match slideIds"
            )
        for local_index, parsed_slide in enumerate(parsed_slides):
            expected_number = len(slides) + 1
            if parsed_slide["number"] != expected_number:
                raise BentoConverterError("Planning candidate slide numbers must be contiguous from 1")
            slides.append(PlanningSlide(
                id=section.slide_ids[local_index],
                number=expected_number,
                title=parsed_slide["title"],
                points=tuple(parsed_slide["points"]),
                section_id=section.id,
                section_title=section.title,
            ))
    if not slides:
        raise BentoConverterError("Planning candidate must contain at least one slide")
    return tuple(slides)


def candidate_signature(
    artifacts: Mapping[str, bytes], sections: Sequence[PlanningSection | Mapping[str, Any]],
) -> str:
    records = []
    for name in PLANNING_ARTIFACT_NAMES:
        payload = bytes(artifacts[name])
        records.append({
            "name": name,
            "byteLength": len(payload),
            "contentDigest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        })
    section_records = [
        {
            "id": section.id,
            "title": section.title,
            "slideIds": list(section.slide_ids),
        }
        if isinstance(section, PlanningSection) else {
            "id": section["id"], "title": section["title"], "slideIds": list(section["slideIds"]),
        }
        for section in sections
    ]
    canonical = {
        "format": PLANNING_CANDIDATE_FORMAT,
        "artifacts": records,
        "sections": section_records,
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_planning_candidate(
    artifacts: Mapping[str, bytes], sections: Sequence[Mapping[str, Any]],
) -> PlanningCandidate:
    if set(artifacts) != set(PLANNING_ARTIFACT_NAMES):
        raise BentoConverterError("Planning candidate must contain exactly four planning artifacts")
    normalized_artifacts = {name: bytes(artifacts[name]) for name in PLANNING_ARTIFACT_NAMES}
    texts = {name: _decode_utf8(name, payload) for name, payload in normalized_artifacts.items()}
    for name in ("explanation-policy", "story-outline", "slide-plan"):
        if not _meaningful_markdown(texts[name]):
            raise BentoConverterError(f"Planning candidate {name} must contain substantive content")
    normalized_sections = _normalize_sections(sections)
    slides = _parse_slide_plan(texts["slide-plan"], normalized_sections)
    try:
        visual_plan = yaml.safe_load(texts["visual-plan"])
    except yaml.YAMLError as exc:
        raise BentoConverterError(f"Cannot parse planning candidate visual plan: {exc}") from exc
    if not isinstance(visual_plan, dict):
        raise BentoConverterError("Planning candidate visual plan root must be an object")
    if set(visual_plan) != {"schemaVersion", "slides"}:
        raise BentoConverterError("Planning candidate visual plan contains unsupported fields")
    for index, entry in enumerate(visual_plan.get("slides") or []):
        if not isinstance(entry, dict) or set(entry) != {"id", "purpose", "visual"}:
            raise BentoConverterError(
                f"Planning candidate visual plan slides[{index}] contains unsupported fields"
            )
        visual = entry.get("visual")
        if not isinstance(visual, dict) or not set(visual) <= {
            "recommended", "type", "intent", "originKind",
        }:
            raise BentoConverterError(
                f"Planning candidate visual plan slides[{index}].visual contains unsupported fields"
            )
    validate_visual_plan(visual_plan)
    visual_ids = {str(entry["id"]) for entry in visual_plan["slides"]}
    slide_ids = {slide.id for slide in slides}
    if visual_ids != slide_ids:
        raise BentoConverterError("Planning candidate visual IDs must exactly match candidate slide IDs")
    signature = candidate_signature(normalized_artifacts, normalized_sections)
    return PlanningCandidate(
        artifacts=normalized_artifacts,
        texts=texts,
        visual_plan=visual_plan,
        sections=normalized_sections,
        slides=slides,
        signature=signature,
    )


def proposal_digest(
    *, proposal_id: str, base_signature: str, candidate_signature_value: str,
    base_context_signature: str, instruction: str, summary: str,
    impact_summary: str, impact: Mapping[str, Any],
) -> str:
    canonical = {
        "format": PLANNING_PROPOSAL_FORMAT,
        "proposalId": proposal_id,
        "basePlanningSignature": base_signature,
        "baseContextSignature": base_context_signature,
        "candidatePlanningSignature": candidate_signature_value,
        "instruction": instruction,
        "summary": summary,
        "impactSummary": impact_summary,
        "impact": impact,
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sections_as_dicts(sections: Sequence[PlanningSection]) -> list[dict[str, Any]]:
    return [
        {"id": section.id, "title": section.title, "slideIds": list(section.slide_ids)}
        for section in sections
    ]


def analyze_planning_impact(
    *, base: PlanningCandidate, candidate: PlanningCandidate,
) -> dict[str, Any]:
    base_by_id = {slide.id: slide for slide in base.slides}
    candidate_by_id = {slide.id: slide for slide in candidate.slides}
    slide_impacts: list[dict[str, Any]] = []
    for slide in base.slides:
        if slide.id not in candidate_by_id:
            slide_impacts.append({
                "id": slide.id, "title": slide.title, "change": "removed",
                "previousNumber": slide.number, "number": None,
            })
    for slide in candidate.slides:
        previous = base_by_id.get(slide.id)
        if previous is None:
            change = "added"
            previous_number = None
        else:
            changed = (
                previous.title != slide.title
                or previous.points != slide.points
                or previous.section_id != slide.section_id
                or previous.section_title != slide.section_title
            )
            moved = previous.number != slide.number or previous.section_id != slide.section_id
            if not changed and not moved:
                continue
            change = "changed" if changed else "moved"
            previous_number = previous.number
        slide_impacts.append({
            "id": slide.id, "title": slide.title, "change": change,
            "previousNumber": previous_number, "number": slide.number,
        })

    base_sections = {section.id: (index, section) for index, section in enumerate(base.sections)}
    candidate_sections = {section.id: (index, section) for index, section in enumerate(candidate.sections)}
    section_impacts: list[dict[str, str]] = []
    for section in base.sections:
        if section.id not in candidate_sections:
            section_impacts.append({"id": section.id, "title": section.title, "change": "removed"})
    for section in candidate.sections:
        previous_value = base_sections.get(section.id)
        if previous_value is None:
            section_impacts.append({"id": section.id, "title": section.title, "change": "added"})
            continue
        previous_index, previous = previous_value
        current_index = candidate_sections[section.id][0]
        if (
            previous_index != current_index
            or previous.title != section.title
            or previous.slide_ids != section.slide_ids
        ):
            section_impacts.append({"id": section.id, "title": section.title, "change": "changed"})

    base_visuals = {str(entry["id"]): entry for entry in base.visual_plan["slides"]}
    candidate_visuals = {str(entry["id"]): entry for entry in candidate.visual_plan["slides"]}
    visual_changes = len({
        slide_id for slide_id in set(base_visuals) | set(candidate_visuals)
        if base_visuals.get(slide_id) != candidate_visuals.get(slide_id)
    })
    return {
        "slides": slide_impacts,
        "sections": section_impacts,
        "explanationPolicyChanged": base.artifacts["explanation-policy"] != candidate.artifacts["explanation-policy"],
        "storyOutlineChanged": base.artifacts["story-outline"] != candidate.artifacts["story-outline"],
        "slidePlanChanged": base.artifacts["slide-plan"] != candidate.artifacts["slide-plan"],
        "visualChanges": visual_changes,
    }
