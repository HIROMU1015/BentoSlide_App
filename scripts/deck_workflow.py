"""Validate and advance the repository-centered BentoSlide workflow state."""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from bento_converter.artifact_transaction import (
    ArtifactLeaseConflict,
    ArtifactTransactionStore,
    WriterLease,
    bytes_revision,
    file_revision,
    recover_repository_transactions,
)
from bento_converter.authoring_storage import (
    AuthoringArtifactStorage,
    validate_authoring_document,
    visible_document_text,
)
from bento_converter.bento_validator import validate_bento_doc
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint, serialize_bento_doc
from bento_converter.html_change import (
    HTML_CHANGE_FORMAT,
    HtmlChangeImpact,
    analyze_html_change,
    html_change_proposal_digest,
)
from bento_converter.html_change_review import (
    POST_APPLY_REVIEW_FORMAT,
    collect_html_change_browser_evidence,
)
from bento_converter.html_pipeline import build_from_html
from bento_converter.html_source import REGISTRY_FORMAT
from bento_converter.planning_proposal import (
    candidate_signature as planning_candidate_signature,
    validate_planning_candidate,
)
from bento_converter.registry_document import (
    canonical_registry_json,
    load_registry,
    normalize_registry,
    registry_revision,
    validate_registry,
)
from bento_converter.section_approval import (
    HtmlDeckStructureEvidence,
    SectionApprovalEvidence,
    compute_html_deck_structure_evidence,
    compute_section_approval_evidence,
)
from bento_converter.section_candidate import write_section_candidate
from bento_converter.segment import (
    canonical_projection_hash,
    merge_segment,
    registry_dependency_closure,
    slide_hashes,
)
from bento_converter.work_editor_storage import (
    WorkEditorStorage,
    document_revision,
    protected_content_fingerprint,
    validate_editor_document,
)
from bento_converter.visual_planning import load_visual_plan, validate_visual_plan


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_RELATIVE = Path("workflow/deck.schema.json")
LEGACY_SCHEMA_RELATIVE = Path("workflow/deck.v1.schema.json")
STATE_RELATIVE = Path("deck.yaml")
SOURCE_MANIFEST_FORMAT = 1
CHAPTER_PATTERN = re.compile(r"^chapter-[0-9]{2,}$")
PROJECT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
PLAN_FILES = {
    "explanationPolicy": Path("planning/explanation-policy.md"),
    "storyOutline": Path("planning/story-outline.md"),
    "slidePlan": Path("planning/slide-plan.md"),
}
VISUAL_PLAN_RELATIVE = Path("planning/visual-plan.yaml")
PLANNING_ARTIFACT_FILES = {
    "explanation-policy": PLAN_FILES["explanationPolicy"],
    "story-outline": PLAN_FILES["storyOutline"],
    "slide-plan": PLAN_FILES["slidePlan"],
    "visual-plan": VISUAL_PLAN_RELATIVE,
}
WORK_LOG_RELATIVE = Path("planning/work-log.md")
STAGE_OWNER = {
    "initialized": "work",
    "planning": "work",
    "awaiting_plan_approval": "work",
    "html_authoring": "work",
    "html_review": "work",
    "ready_for_conversion": "codex",
    "converting": "codex",
    "bento_validation": "codex",
    "bento_authoring": "work",
    "content_review": "work",
    "bento_finalization": "work",
    "complete": "codex",
}
STAGE_SOURCE = {
    "initialized": "sources",
    "planning": "planning",
    "awaiting_plan_approval": "planning",
    "html_authoring": "html",
    "html_review": "html",
    "ready_for_conversion": "html",
    "converting": "html",
    "bento_validation": "generated",
    "bento_authoring": "authoring",
    "content_review": "authoring",
    "bento_finalization": "final",
    "complete": "final",
}

WHOLE_DECK_STRATEGY = "whole_deck"
ROLLING_SECTIONS_STRATEGY = "rolling_sections"
ACTIVE_HTML_CHANGE_STATUSES = {"proposed", "approved"}
HTML_DECK_REVIEW_FORMAT = "bento/html-deck-review-baseline/v1"

LEGACY_STAGE_SOURCE = {
    **STAGE_SOURCE,
    "html_authoring": "chapters",
    "html_review": "chapters",
    "ready_for_conversion": "chapters",
    "converting": "chapters",
}


class WorkflowError(RuntimeError):
    """A requested workflow operation is unsafe or invalid."""


class PlanningRevisionConflict(WorkflowError):
    """The reviewed planning snapshot changed before its workflow transition."""


def _authoring_strategy(state: dict[str, Any]) -> str:
    """Return an explicit strategy while preserving older v2 rolling states."""

    return str(state.get("authoring", {}).get("strategy") or ROLLING_SECTIONS_STRATEGY)


def _html_change(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("authoring", {}).get("htmlChange")
    return value if isinstance(value, dict) else None


def _html_review_baseline(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("authoring", {}).get("htmlReview")
    return value if isinstance(value, dict) else None


def _has_active_html_change(state: dict[str, Any]) -> bool:
    proposal = _html_change(state)
    return bool(proposal and proposal.get("status") in ACTIVE_HTML_CHANGE_STATUSES)


def _post_apply_review(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    value = proposal.get("postApplyReview") if isinstance(proposal, dict) else None
    return value if isinstance(value, dict) else None


def _has_unfinished_html_change(state: dict[str, Any]) -> bool:
    proposal = _html_change(state)
    if not proposal:
        return False
    if proposal.get("status") in ACTIVE_HTML_CHANGE_STATUSES:
        return True
    review = _post_apply_review(proposal)
    return bool(proposal.get("status") == "applied" and (not review or review.get("status") != "checked"))


def _expected_handoff(state: dict[str, Any], *, stage: str | None = None) -> dict[str, bool]:
    """Derive schema-v2 handoff flags from the effective workflow stage."""

    effective = stage or state["workflow"]["stage"]
    if effective == "blocked":
        blocked_from = state["workflow"].get("blockedFrom")
        effective = blocked_from.get("stage") if isinstance(blocked_from, dict) else "initialized"
    return {
        "readyForCodex": effective in {"ready_for_conversion", "converting"},
        "readyForBentoAuthoring": effective in {"bento_validation", "bento_authoring"},
        "readyForContentReview": effective == "content_review",
        "readyForFinalEditing": effective == "bento_finalization",
    }


def _normalize_handoff(state: dict[str, Any], *, stage: str | None = None) -> None:
    if state.get("schemaVersion") == 2:
        state["handoff"].update(_expected_handoff(state, stage=stage))


class ChapterHtmlParser(HTMLParser):
    """Collect stable IDs and registry references without rendering the chapter."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_slide: str | None = None
        self.slide_ids: list[str] = []
        self.elements: dict[str, list[str]] = {}
        self.references: list[tuple[str, str, str | None, str | None]] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        slide_id = values.get("data-slide-id")
        if slide_id:
            self.current_slide = slide_id
            self.slide_ids.append(slide_id)
            self.elements.setdefault(slide_id, [])
        element_id = values.get("data-bento-id")
        if element_id:
            if not self.current_slide:
                raise WorkflowError(f"data-bento-id={element_id!r} is outside a data-slide-id section")
            self.elements.setdefault(self.current_slide, []).append(element_id)
        for attribute, collection in (
            ("data-equation-id", "equations"),
            ("data-figure-id", "figures"),
            ("data-chart-id", "charts"),
            ("data-table-id", "tables"),
            ("data-asset-id", "assets"),
        ):
            reference = values.get(attribute)
            if reference:
                self.references.append((collection, reference, self.current_slide, values.get("data-latex")))

    handle_startendtag = handle_starttag

    def handle_data(self, data: str) -> None:
        self.text_chunks.append(data)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_root(value: str | Path | None = None) -> Path:
    root = Path(value).resolve() if value else ROOT.resolve()
    if not root.is_dir():
        raise WorkflowError(f"Repository does not exist: {root}")
    return root


def _repo_path(root: Path, value: str, *, field: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise WorkflowError(f"{field} must be repository-relative: {value}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkflowError(f"{field} escapes the repository: {value}") from exc
    return resolved


def _sidecar_path(html_path: Path) -> Path:
    name = html_path.name
    if name.endswith(".bento.html"):
        return html_path.with_name(name[: -len(".bento.html")] + ".bento.json")
    return html_path.with_suffix(".bento.json")


def _final_baseline_path(root: Path, state: dict[str, Any]) -> Path:
    final_html = _repo_path(root, state["outputs"]["finalHtml"], field="outputs.finalHtml")
    name = final_html.name
    stem = name[: -len(".bento.html")] if name.endswith(".bento.html") else final_html.stem
    return final_html.parent / "revisions" / f"{stem}.baseline.bento.json"


def _final_registry_baseline_path(root: Path, state: dict[str, Any]) -> Path:
    final_html = _repo_path(root, state["outputs"]["finalHtml"], field="outputs.finalHtml")
    name = final_html.name
    stem = name[: -len(".bento.html")] if name.endswith(".bento.html") else final_html.stem
    return final_html.parent / "revisions" / f"{stem}.baseline.registry.json"


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object: {path}")
    return value


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(destination: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write_bytes(destination, payload)


def load_source_manifest(root: Path, state: dict[str, Any], *, require_exists: bool = True) -> dict[str, Any]:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Source manifests require deck schema v2")
    path = _repo_path(root, state["sources"]["manifest"], field="sources.manifest")
    if not path.is_file():
        if require_exists:
            raise WorkflowError(f"Source manifest does not exist: {state['sources']['manifest']}")
        return {"schemaVersion": SOURCE_MANIFEST_FORMAT, "authorityMode": state["sources"]["authorityMode"], "items": []}
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise WorkflowError(f"Cannot read source manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SOURCE_MANIFEST_FORMAT:
        raise WorkflowError("Source manifest schemaVersion must be 1")
    if manifest.get("authorityMode") not in {"single", "multiple", "imported"}:
        raise WorkflowError("Source manifest authorityMode is invalid")
    if manifest["authorityMode"] != state["sources"]["authorityMode"]:
        raise WorkflowError("Source manifest authorityMode differs from deck.yaml")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise WorkflowError("Source manifest items must be an array")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise WorkflowError(f"Source manifest item {index} must be an object")
        source_id = item.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source_id):
            raise WorkflowError(f"Source manifest item {index} has an invalid id")
        if source_id in seen:
            raise WorkflowError(f"Duplicate source manifest id: {source_id}")
        seen.add(source_id)
        source_path = item.get("path")
        if not isinstance(source_path, str) or not source_path:
            raise WorkflowError(f"Source manifest item {source_id!r} requires a path")
        resolved_source = _repo_path(root, source_path, field=f"sources.items[{index}].path")
        if require_exists and not resolved_source.exists():
            raise WorkflowError(f"Source manifest item does not exist: {source_path}")
        if not isinstance(item.get("type"), str) or not item["type"]:
            raise WorkflowError(f"Source manifest item {source_id!r} requires a type")
        if item.get("role") not in {"primary", "evidence", "reference", "supplementary", "imported"}:
            raise WorkflowError(f"Source manifest item {source_id!r} has an invalid role")
    if manifest["authorityMode"] == "single":
        primaries = [item for item in items if item.get("role") == "primary"]
        if len(primaries) > 1:
            raise WorkflowError("Single-authority source manifest has multiple primary items")
    return manifest


def content_approval_digest(document_revision_value: str, registry_revision_value: str) -> str:
    for label, value in (("document", document_revision_value), ("registry", registry_revision_value)):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise WorkflowError(f"Invalid {label} revision for content approval")
    payload = (
        "bento/content-approval/v1\0" + document_revision_value + "\0" + registry_revision_value
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_state(root: Path) -> dict[str, Any]:
    path = root / STATE_RELATIVE
    try:
        state = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise WorkflowError(f"Cannot read deck.yaml: {exc}") from exc
    if not isinstance(state, dict):
        raise WorkflowError("deck.yaml root must be a mapping")
    validate_state(root, state)
    return state


def validate_state(root: Path, state: dict[str, Any]) -> None:
    version = state.get("schemaVersion")
    if version not in {1, 2}:
        raise WorkflowError(f"Unsupported deck.yaml schemaVersion: {version!r}")
    schema_path = root / (LEGACY_SCHEMA_RELATIVE if version == 1 else SCHEMA_RELATIVE)
    schema = _read_json(schema_path, label="deck schema")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(state),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        formatted = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "deck"
            formatted.append(f"{location}: {error.message}")
        raise WorkflowError("deck.yaml schema validation failed:\n- " + "\n- ".join(formatted))

    workflow = state["workflow"]
    stage = workflow["stage"]
    stage_sources = LEGACY_STAGE_SOURCE if version == 1 else STAGE_SOURCE
    if stage != "blocked":
        if workflow["owner"] != STAGE_OWNER[stage]:
            raise WorkflowError(f"workflow.owner must be {STAGE_OWNER[stage]!r} for stage {stage!r}")
        if workflow["sourceOfTruth"] != stage_sources[stage]:
            raise WorkflowError(f"workflow.sourceOfTruth must be {stage_sources[stage]!r} for stage {stage!r}")
    current = workflow["currentChapter"]
    if current is not None and current not in state["chapters"]:
        raise WorkflowError(f"workflow.currentChapter is not registered: {current}")
    if version == 2:
        current_section = workflow["currentSection"]
        if current_section is not None and current_section not in state["sections"]:
            raise WorkflowError(f"workflow.currentSection is not registered: {current_section}")
        if state["authoring"]["currentSection"] != current_section:
            raise WorkflowError("authoring.currentSection must match workflow.currentSection")
    blocked_from = workflow.get("blockedFrom")
    if stage == "blocked":
        if not workflow["blockingReason"]:
            raise WorkflowError("workflow.blockingReason is required while blocked")
        if not isinstance(blocked_from, dict):
            raise WorkflowError("workflow.blockedFrom is required while blocked")
        previous_stage = blocked_from["stage"]
        if previous_stage in {"blocked", "complete"}:
            raise WorkflowError(f"workflow.blockedFrom.stage cannot be {previous_stage!r}")
        if blocked_from["owner"] != STAGE_OWNER[previous_stage]:
            raise WorkflowError("workflow.blockedFrom.owner does not match its stage")
        if blocked_from["sourceOfTruth"] != stage_sources[previous_stage]:
            raise WorkflowError("workflow.blockedFrom.sourceOfTruth does not match its stage")
        previous_current = blocked_from["currentChapter"]
        if previous_current is not None and previous_current not in state["chapters"]:
            raise WorkflowError(f"workflow.blockedFrom.currentChapter is not registered: {previous_current}")
        if version == 2:
            previous_section = blocked_from["currentSection"]
            if previous_section is not None and previous_section not in state["sections"]:
                raise WorkflowError(f"workflow.blockedFrom.currentSection is not registered: {previous_section}")
    elif workflow["blockingReason"] is not None or blocked_from is not None:
        raise WorkflowError("workflow.blockingReason and blockedFrom must be null outside the blocked stage")
    if version == 2:
        expected_handoff = _expected_handoff(state)
        if state["handoff"] != expected_handoff:
            raise WorkflowError(
                f"handoff flags do not match workflow stage {stage!r}: "
                f"expected={expected_handoff}, actual={state['handoff']}"
            )
    for field in ("request",):
        _repo_path(root, state["project"][field], field=f"project.{field}")
    if state["project"]["primarySource"]:
        _repo_path(root, state["project"]["primarySource"], field="project.primarySource")
    for index, value in enumerate(state["project"]["supplementarySources"]):
        _repo_path(root, value, field=f"project.supplementarySources[{index}]")
    for chapter_id, chapter in state["chapters"].items():
        if not CHAPTER_PATTERN.fullmatch(chapter_id):
            raise WorkflowError(f"Invalid chapter id: {chapter_id}")
        _repo_path(root, chapter["html"], field=f"chapters.{chapter_id}.html")
        _repo_path(root, chapter["registry"], field=f"chapters.{chapter_id}.registry")
    if version == 2:
        manifest_path = _repo_path(root, state["sources"]["manifest"], field="sources.manifest")
        if manifest_path.is_file():
            load_source_manifest(root, state)
        mode = state["authoring"]["mode"]
        strategy = _authoring_strategy(state)
        if mode == "modular":
            if state["authoring"]["entryHtml"] is not None or state["authoring"]["registry"] is not None:
                raise WorkflowError("Modular authoring must use chapters rather than authoring.entryHtml/registry")
            if state["authoring"].get("strategy") is not None:
                raise WorkflowError("Modular authoring cannot declare a single-HTML authoring strategy")
            if state["authoring"].get("htmlChange") is not None:
                raise WorkflowError("Modular authoring cannot retain an HTML change proposal")
            if state["authoring"].get("htmlReview") is not None:
                raise WorkflowError("Modular authoring cannot retain a whole-deck HTML review baseline")
        else:
            if state["authoring"]["entryHtml"] is None or state["authoring"]["registry"] is None:
                raise WorkflowError(f"{mode} authoring requires entryHtml and registry")
            _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
            _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
            if state["chapters"]:
                raise WorkflowError(f"{mode} authoring must use sections rather than chapters")
            if workflow["currentChapter"] is not None:
                raise WorkflowError(f"{mode} authoring cannot have a current chapter")
            if strategy == WHOLE_DECK_STRATEGY and stage in {
                "html_authoring", "html_review", "ready_for_conversion", "converting",
            } and workflow["currentSection"] is not None:
                raise WorkflowError("Whole-deck HTML authoring cannot expose a current section")
            review_baseline = _html_review_baseline(state)
            if review_baseline is not None:
                if strategy != WHOLE_DECK_STRATEGY:
                    raise WorkflowError("An HTML review baseline requires whole-deck authoring")
                if review_baseline["format"] != HTML_DECK_REVIEW_FORMAT:
                    raise WorkflowError("Unknown whole-deck HTML review baseline format")
                for relative in review_baseline["dependencyRevisions"]:
                    _repo_path(
                        root, relative,
                        field=f"authoring.htmlReview.dependencyRevisions.{relative}",
                    )
            proposal = _html_change(state)
            if proposal is not None:
                if strategy != WHOLE_DECK_STRATEGY:
                    raise WorkflowError("HTML change proposals require whole-deck authoring")
                for field in ("candidateHtml", "candidateRegistry", "proposalPath"):
                    proposal_path = _repo_path(
                        root, proposal[field], field=f"authoring.htmlChange.{field}",
                    )
                    if field != "proposalPath" and proposal_path in {
                        _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml"),
                        _repo_path(root, state["authoring"]["registry"], field="authoring.registry"),
                    }:
                        raise WorkflowError("An HTML change candidate cannot overwrite the canonical source")
                if proposal["format"] == HTML_CHANGE_FORMAT:
                    for manifest_field in ("baseDependencyRevisions", "candidateDependencyRevisions"):
                        for relative in proposal[manifest_field]:
                            _repo_path(
                                root, relative,
                                field=f"authoring.htmlChange.{manifest_field}.{relative}",
                            )
                proposal_status = proposal["status"]
                try:
                    current_proposal_digest = html_change_proposal_digest(proposal)
                except BentoConverterError as exc:
                    raise WorkflowError(str(exc)) from exc
                if proposal["proposalDigest"] != current_proposal_digest:
                    raise WorkflowError("HTML change proposalDigest does not match its explanation and impact")
                if proposal_status in ACTIVE_HTML_CHANGE_STATUSES and stage != "html_review":
                    raise WorkflowError("An active HTML change proposal is allowed only during whole-deck HTML review")
                if proposal["format"] == HTML_CHANGE_FORMAT and proposal_status in {"proposed", "approved"} and (
                    not review_baseline
                    or review_baseline["evidenceDigest"] != proposal["baseReviewDigest"]
                ):
                    raise WorkflowError("An active HTML change must remain bound to its base review baseline")
                if proposal["format"] == HTML_CHANGE_FORMAT and proposal_status == "applied" and (
                    not review_baseline
                    or review_baseline["evidenceDigest"] != proposal["candidateReviewDigest"]
                ):
                    raise WorkflowError("An applied HTML change must open its candidate review baseline")
                if proposal_status == "proposed" and any(
                    proposal[field] is not None for field in ("approvedAt", "appliedAt", "cancelledAt")
                ):
                    raise WorkflowError("A proposed HTML change cannot retain approval or completion timestamps")
                if proposal_status == "proposed" and (
                    proposal["approvedProposalDigest"] is not None
                    or proposal["postApplyReview"] is not None
                ):
                    raise WorkflowError("A proposed HTML change cannot retain approval or post-apply evidence")
                if proposal_status == "approved" and (
                    proposal["approvedAt"] is None
                    or proposal["appliedAt"] is not None
                    or proposal["cancelledAt"] is not None
                ):
                    raise WorkflowError("An approved HTML change requires only approvedAt")
                if proposal_status == "approved" and (
                    proposal["approvedProposalDigest"] != proposal["proposalDigest"]
                    or proposal["postApplyReview"] is not None
                ):
                    raise WorkflowError("An approved HTML change must bind the exact proposal digest")
                if proposal_status == "applied" and (
                    proposal["approvedAt"] is None or proposal["appliedAt"] is None
                    or proposal["cancelledAt"] is not None
                ):
                    raise WorkflowError("An applied HTML change requires approvedAt and appliedAt")
                if proposal_status == "applied" and (
                    proposal["approvedProposalDigest"] != proposal["proposalDigest"]
                    or not isinstance(proposal["postApplyReview"], dict)
                ):
                    raise WorkflowError("An applied HTML change requires its approved digest and post-apply review")
                if proposal_status == "cancelled" and (
                    proposal["cancelledAt"] is None or proposal["appliedAt"] is not None
                ):
                    raise WorkflowError("A cancelled HTML change requires cancelledAt and cannot be applied")
                if proposal_status == "cancelled" and proposal["postApplyReview"] is not None:
                    raise WorkflowError("A cancelled HTML change cannot retain post-apply evidence")
                review = _post_apply_review(proposal)
                if review is not None:
                    review_root = (
                        root / "output" / "html-change-reviews" / proposal["proposalId"]
                    ).resolve()
                    report_path = _repo_path(
                        root, review["reportPath"],
                        field="authoring.htmlChange.postApplyReview.reportPath",
                    )
                    environment_path = _repo_path(
                        root, review["environmentPath"],
                        field="authoring.htmlChange.postApplyReview.environmentPath",
                    )
                    if report_path != review_root / "browser-report.json":
                        raise WorkflowError("Post-apply browser report path is not proposal-scoped")
                    if environment_path != review_root / "browser-environment.json":
                        raise WorkflowError("Post-apply browser environment path is not proposal-scoped")
                    screenshot_paths: list[Path] = []
                    for slide_id, screenshot in review["screenshots"].items():
                        screenshot_path = _repo_path(
                            root, screenshot["path"],
                            field=f"authoring.htmlChange.postApplyReview.screenshots.{slide_id}.path",
                        )
                        if screenshot_path.parent != review_root / "screenshots":
                            raise WorkflowError("Post-apply screenshot path is not proposal-scoped")
                        screenshot_paths.append(screenshot_path)
                    if len(screenshot_paths) != len(set(screenshot_paths)):
                        raise WorkflowError("Post-apply slides cannot share a screenshot path")
                    if review["format"] != POST_APPLY_REVIEW_FORMAT:
                        raise WorkflowError("Unknown post-apply HTML review format")
                    if review["proposalDigest"] != proposal["proposalDigest"]:
                        raise WorkflowError("Post-apply review is bound to a different proposal")
                    if review["htmlRevision"] != proposal["candidateHtmlRevision"]:
                        raise WorkflowError("Post-apply review HTML revision differs from the applied candidate")
                    if review["registryRevision"] != proposal["candidateRegistryRevision"]:
                        raise WorkflowError("Post-apply review registry revision differs from the applied candidate")
                    renderable_affected = [
                        slide_id for slide_id in proposal["affectedSlideIds"]
                        if slide_id not in set(proposal["removedSlideIds"])
                    ]
                    if review["affectedSlideIds"] != renderable_affected:
                        raise WorkflowError(
                            "Post-apply review must cover every affected slide that remains in the deck"
                        )
                    evidence_fields = (
                        "reportRevision", "environmentRevision", "browserEnvironmentDigest", "checkedAt",
                    )
                    if review["status"] == "pending" and (
                        any(review[field] is not None for field in evidence_fields)
                        or review["screenshots"]
                    ):
                        raise WorkflowError("Pending post-apply review cannot retain completed evidence")
                    if review["status"] == "checked" and (
                        any(review[field] is None for field in evidence_fields)
                        or set(review["screenshots"]) != set(review["affectedSlideIds"])
                    ):
                        raise WorkflowError("Checked post-apply review requires complete current evidence")
                    if review["status"] == "pending" and stage != "html_review":
                        raise WorkflowError("Pending post-apply review is allowed only during HTML review")
            registered_slides: set[str] = set()
            registered_bento_slides: set[str] = set()
            for section_id, section in state["sections"].items():
                duplicates = registered_slides.intersection(section["slideIds"])
                if duplicates:
                    raise WorkflowError(f"Slide IDs are registered in multiple sections: {sorted(duplicates)}")
                registered_slides.update(section["slideIds"])
                bento_slide_ids = section.get("bentoSlideIds", [])
                bento_duplicates = registered_bento_slides.intersection(bento_slide_ids)
                if bento_duplicates:
                    raise WorkflowError(
                        f"Bento slide IDs belong to multiple sections: {sorted(bento_duplicates)}"
                    )
                registered_bento_slides.update(bento_slide_ids)
                canonical = section.get("canonical")
                if canonical is not None:
                    expected = {
                        "planned": "planning", "html_authoring": "html", "html_review": "html",
                        "bento_integration": "html", "bento_authoring": "bento", "accepted": "bento",
                    }.get(section["status"])
                    if expected is not None and canonical != expected:
                        raise WorkflowError(f"Section {section_id} canonical must be {expected!r} while {section['status']!r}")
                html_approved = section["status"] in {"approved", "bento_integration", "bento_authoring", "accepted"}
                if html_approved and section["approvalDigest"] is None:
                    raise WorkflowError(f"HTML-approved section has no approval digest: {section_id}")
                if not html_approved and section["approvalDigest"] is not None:
                    raise WorkflowError(f"Section retains an HTML approval digest before approval: {section_id}")
                bento_values = [section.get(key) for key in (
                    "bentoDocumentRevision", "bentoRegistryRevision", "bentoSectionDigest",
                )]
                if section["status"] in {"bento_authoring", "accepted"} and any(value is None for value in bento_values):
                    raise WorkflowError(f"Bento section is missing revision binding: {section_id}")
                if canonical == "bento" and bento_slide_ids and section["slideIds"] != bento_slide_ids:
                    raise WorkflowError(
                        f"Bento-canonical section membership differs from its installed slides: {section_id}"
                    )
                if section["status"] == "accepted" and section.get("acceptedAt") is None:
                    raise WorkflowError(f"Accepted section has no timestamp: {section_id}")
    resolved_outputs = {
        field: _repo_path(root, value, field=f"outputs.{field}")
        for field, value in state["outputs"].items() if value is not None
    }
    if len(set(resolved_outputs.values())) != len(resolved_outputs):
        raise WorkflowError("Generated/authoring/final Bento and registry output paths must be distinct")
    if resolved_outputs["generatedJson"] != _sidecar_path(resolved_outputs["generatedHtml"]):
        raise WorkflowError("outputs.generatedJson must be the sidecar path derived from outputs.generatedHtml")
    if resolved_outputs["finalJson"] != _sidecar_path(resolved_outputs["finalHtml"]):
        raise WorkflowError("outputs.finalJson must be the sidecar path derived from outputs.finalHtml")
    if version == 2:
        authoring_values = [state["outputs"][field] for field in ("authoringHtml", "authoringJson", "authoringRegistry")]
        if any(value is None for value in authoring_values) and not all(value is None for value in authoring_values):
            raise WorkflowError("Authoring HTML, JSON, and registry paths must all be set or all be null")
        late_compatibility = state["migration"]["lateStageCompatibility"]
        if all(value is None for value in authoring_values):
            late_stage = stage in {"bento_finalization", "complete"} or (
                stage == "blocked" and blocked_from["stage"] == "bento_finalization"
            )
            if not late_compatibility or not late_stage:
                raise WorkflowError("Authoring outputs may be null only for migrated late-stage decks")
        else:
            if resolved_outputs["authoringJson"] != _sidecar_path(resolved_outputs["authoringHtml"]):
                raise WorkflowError("outputs.authoringJson must be the sidecar path derived from outputs.authoringHtml")
        if state["outputs"]["finalRegistry"] is None:
            raise WorkflowError("outputs.finalRegistry is required in schema v2")
        approval = state["approvals"]["bentoContent"]
        approval_values = (
            approval["documentRevision"], approval["registryRevision"],
            approval["approvalDigest"], approval["approvedAt"],
        )
        if approval["status"] == "pending" and any(value is not None for value in approval_values):
            raise WorkflowError("Pending Bento content approval must not retain revision metadata")
        if approval["status"] == "approved":
            if any(value is None for value in approval_values):
                raise WorkflowError("Approved Bento content requires document/registry revisions, digest, and timestamp")
            expected_digest = content_approval_digest(approval["documentRevision"], approval["registryRevision"])
            if approval["approvalDigest"] != expected_digest:
                raise WorkflowError("Bento content approval digest does not match its revisions")
        final_approval = state["approvals"]["finalBento"]
        if isinstance(final_approval, dict):
            revision_values = (
                final_approval["documentRevision"], final_approval["htmlRevision"],
                final_approval["registryRevision"], final_approval["runtimeFingerprint"],
                final_approval["approvedAt"],
            )
            if final_approval["status"] == "pending" and any(value is not None for value in revision_values):
                raise WorkflowError("Pending final Bento approval must not retain revision metadata")
            if final_approval["status"] == "approved" and any(value is None for value in revision_values):
                raise WorkflowError("Approved final Bento requires document, HTML, registry, runtime, and timestamp revisions")
    baseline = state["validation"].get("finalBaseline")
    if baseline is not None:
        document_field = "path" if version == 1 else "documentPath"
        baseline_path = _repo_path(root, baseline[document_field], field=f"validation.finalBaseline.{document_field}")
        if baseline_path != _final_baseline_path(root, state):
            raise WorkflowError(f"validation.finalBaseline.{document_field} does not match outputs.finalHtml")
        if version == 2:
            registry_path = _repo_path(root, baseline["registryPath"], field="validation.finalBaseline.registryPath")
            if registry_path != _final_registry_baseline_path(root, state):
                raise WorkflowError("validation.finalBaseline.registryPath does not match outputs.finalHtml")
    current_url = state["preview"]["currentUrl"]
    if current_url:
        port = int(current_url.rsplit(":", 1)[1].rstrip("/"))
        if port < 1 or port > 65535:
            raise WorkflowError("preview.currentUrl contains an invalid port")


def atomic_write_state(root: Path, state: dict[str, Any]) -> None:
    validate_state(root, state)
    destination = root / STATE_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _state_payload(state)
    handle = tempfile.NamedTemporaryFile(prefix=".deck.", suffix=".yaml.tmp", dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _state_payload(state: dict[str, Any]) -> bytes:
    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False).encode("utf-8")


def _work_log_payload(root: Path, message: str) -> bytes:
    path = root / WORK_LOG_RELATIVE
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Work log\n\n"
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return (existing + f"- {utc_now()} — {message}\n").encode("utf-8")


def append_work_log(root: Path, message: str) -> None:
    path = root / "planning/work-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Work log\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"- {utc_now()} — {message}\n")


def _transition(state: dict[str, Any], stage: str, status: str, *, current: str | None = None) -> None:
    workflow = state["workflow"]
    workflow["stage"] = stage
    workflow["status"] = status
    workflow["owner"] = STAGE_OWNER[stage]
    workflow["sourceOfTruth"] = (
        LEGACY_STAGE_SOURCE[stage] if state.get("schemaVersion") == 1 else STAGE_SOURCE[stage]
    )
    if state.get("schemaVersion") == 2:
        if state["authoring"]["mode"] == "modular":
            workflow["currentChapter"] = current
            workflow["currentSection"] = None
            state["authoring"]["currentSection"] = None
        else:
            workflow["currentChapter"] = None
            workflow["currentSection"] = current
            state["authoring"]["currentSection"] = current
    else:
        workflow["currentChapter"] = current
    workflow["blockingReason"] = None
    workflow["blockedFrom"] = None
    _normalize_handoff(state, stage=stage)


def _require_stage(state: dict[str, Any], *allowed: str) -> None:
    actual = state["workflow"]["stage"]
    if actual not in allowed:
        raise WorkflowError(f"Stage {actual!r} does not allow this operation; expected one of {allowed}")


def discover_source_candidates(root: Path, state: dict[str, Any]) -> tuple[Path | None, list[Path]]:
    if state.get("schemaVersion") == 2:
        manifest = load_source_manifest(root, state)
        candidates = [
            _repo_path(root, item["path"], field=f"sources.items.{item['id']}.path")
            for item in manifest["items"]
        ]
        primaries = [
            _repo_path(root, item["path"], field=f"sources.items.{item['id']}.path")
            for item in manifest["items"] if item.get("role") == "primary"
        ]
        if manifest["authorityMode"] == "single" and len(primaries) != 1:
            raise WorkflowError("Single-authority source manifest requires exactly one primary item")
        return (primaries[0] if len(primaries) == 1 else None), candidates
    sources = (root / "sources").resolve()
    if not sources.is_dir():
        raise WorkflowError("sources/ does not exist")
    candidates: list[Path] = []
    for path in sources.rglob("*.pdf"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(sources)
        except ValueError:
            continue
        candidates.append(resolved)
    candidates.sort(key=lambda path: path.relative_to(root).as_posix().casefold())

    explicit = state["project"].get("primarySource")
    if explicit:
        selected = _repo_path(root, explicit, field="project.primarySource")
        if selected.suffix.casefold() != ".pdf" or not selected.is_file():
            raise WorkflowError(f"Configured primarySource is not an existing PDF: {explicit}")
        if selected not in candidates:
            raise WorkflowError(f"Configured primarySource must remain under sources/: {explicit}")
        return selected, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    if not candidates:
        raise WorkflowError("No primary PDF was found under sources/. Put it in sources/private/.")
    display = ", ".join(path.relative_to(root).as_posix() for path in candidates)
    raise WorkflowError(f"Multiple PDF sources found; set project.primarySource in deck.yaml: {display}")


def ensure_source_manifest(root: Path, state: dict[str, Any]) -> None:
    """Create the unambiguous first manifest entry without asking for mechanics."""

    if state.get("schemaVersion") != 2:
        return
    manifest_path = _repo_path(root, state["sources"]["manifest"], field="sources.manifest")
    manifest = load_source_manifest(root, state, require_exists=False)
    if manifest.get("items"):
        return
    private = root / "sources/private"
    supported = {".pdf", ".md", ".markdown", ".txt", ".html", ".htm", ".json", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".svg"}
    candidates = sorted(
        (path for path in private.rglob("*") if path.is_file() and path.suffix.lower() in supported),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    ) if private.is_dir() else []
    if not candidates:
        raise WorkflowError("No source material was found under sources/private/")
    if len(candidates) != 1:
        display = ", ".join(path.relative_to(root).as_posix() for path in candidates)
        raise WorkflowError(f"Source authority is genuinely ambiguous; choose one primary source: {display}")
    source = candidates[0]
    identifier = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "primary-source"
    candidate_manifest = {
        "schemaVersion": SOURCE_MANIFEST_FORMAT, "authorityMode": "single",
        "items": [{
            "id": identifier, "path": source.relative_to(root).as_posix(),
            "type": _source_type(source.as_posix()), "role": "primary",
        }],
    }
    payload = yaml.safe_dump(candidate_manifest, allow_unicode=True, sort_keys=False).encode("utf-8")
    ArtifactTransactionStore(root, (manifest_path,)).commit(
        {manifest_path: payload}, operation="auto-register-unambiguous-source",
    )


def _meaningful_markdown(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*#+\s+.*$", "", text, flags=re.MULTILINE)
    return bool(text.strip())


def validate_planning(root: Path) -> None:
    missing = [str(path) for path in PLAN_FILES.values() if not _meaningful_markdown(root / path)]
    if missing:
        raise WorkflowError("Planning artifacts are missing substantive content: " + ", ".join(missing))
    visual_plan = root / VISUAL_PLAN_RELATIVE
    if visual_plan.exists():
        try:
            load_visual_plan(visual_plan)
        except BentoConverterError as exc:
            raise WorkflowError(str(exc)) from exc


def _planned_units(state: dict[str, Any]) -> dict[str, Any]:
    single = state.get("schemaVersion") == 2 and state.get("authoring", {}).get("mode") != "modular"
    units = state.get("sections") if single else state.get("chapters")
    return units if isinstance(units, dict) else {}


def planning_artifact_paths(root: Path, state: dict[str, Any]) -> tuple[Path, ...]:
    """Return the review-bound planning inputs in a stable repository-safe order."""

    paths: list[Path] = []
    request = state.get("project", {}).get("request")
    if isinstance(request, str) and request:
        paths.append(_repo_path(root, request, field="project.request"))
    paths.extend((root / relative).resolve() for relative in PLAN_FILES.values())
    paths.append((root / VISUAL_PLAN_RELATIVE).resolve())
    return tuple(paths)


def planning_action_artifact_paths(root: Path, state: dict[str, Any]) -> tuple[Path, ...]:
    """Return every artifact protected while a planning action is committed."""

    return (
        (root / STATE_RELATIVE).resolve(),
        *planning_artifact_paths(root, state),
        (root / WORK_LOG_RELATIVE).resolve(),
    )


def planning_review_signature(root: Path, state: dict[str, Any]) -> str:
    """Hash an unambiguous canonical record of the exact planning review inputs."""

    root = root.resolve()
    artifacts: list[dict[str, Any]] = []
    review_paths = ((root / STATE_RELATIVE).resolve(), *planning_artifact_paths(root, state))
    for path in review_paths:
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            payload = path.read_bytes()
            artifacts.append({
                "path": relative,
                "status": "present",
                "byteLength": len(payload),
                "contentDigest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            })
        else:
            artifacts.append({
                "path": relative,
                "status": "missing",
                "byteLength": 0,
                "contentDigest": None,
            })
    canonical = {
        "format": "bento/planning-review-signature/v1",
        "stage": str(state.get("workflow", {}).get("stage") or ""),
        "artifacts": artifacts,
        "units": list(_planned_units(state).items()),
    }
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def planning_is_ready(root: Path, state: dict[str, Any]) -> bool:
    """Check submission/approval prerequisites without changing repository state."""

    try:
        validate_planning(root)
    except WorkflowError:
        return False
    return bool(_planned_units(state))


@contextmanager
def planning_action_guard(
    root: Path,
    state: dict[str, Any],
    *,
    inherited_writer_lease: WriterLease | None = None,
) -> Iterator[WriterLease]:
    """Hold the cross-process planning/state lease and refresh the caller's state."""

    required = set(planning_action_artifact_paths(root, state))
    lease = inherited_writer_lease or WriterLease(root, required)
    acquired_here = inherited_writer_lease is None
    try:
        if acquired_here:
            lease.acquire()
        elif not lease.acquired or not required.issubset(set(lease.artifacts)):
            raise PlanningRevisionConflict(
                "構成案の保護状態が更新されています。最新のStoryboardを読み直してください。"
            )
        fresh = load_state(root)
        fresh_required = set(planning_action_artifact_paths(root, fresh))
        if not fresh_required.issubset(set(lease.artifacts)):
            raise PlanningRevisionConflict(
                "構成案の保存場所が更新されています。最新のStoryboardを読み直してください。"
            )
        state.clear()
        state.update(fresh)
        yield lease
    except ArtifactLeaseConflict as exc:
        raise PlanningRevisionConflict(
            "構成案を別の処理が更新中です。完了後にStoryboardを読み直してください。"
        ) from exc
    finally:
        if acquired_here:
            lease.release()


@contextmanager
def planning_writer_guard_if_needed(
    root: Path, state: dict[str, Any],
) -> Iterator[WriterLease | None]:
    """Join the planning lease for generic state writes that can run early."""

    if state.get("workflow", {}).get("stage") in {
        "initialized", "planning", "awaiting_plan_approval",
    }:
        with planning_action_guard(root, state) as lease:
            yield lease
        return
    yield None


def _load_chapter(root: Path, chapter_id: str, entry: dict[str, Any]) -> tuple[ChapterHtmlParser, dict[str, Any]]:
    html_path = _repo_path(root, entry["html"], field=f"chapters.{chapter_id}.html")
    registry_path = _repo_path(root, entry["registry"], field=f"chapters.{chapter_id}.registry")
    if not html_path.is_file():
        raise WorkflowError(f"Chapter HTML does not exist: {entry['html']}")
    if not registry_path.is_file():
        raise WorkflowError(f"Chapter registry does not exist: {entry['registry']}")
    registry = _read_json(registry_path, label="chapter registry")
    if registry.get("format") != REGISTRY_FORMAT:
        raise WorkflowError(f"{entry['registry']}: format must be {REGISTRY_FORMAT!r}")
    if registry.get("chapterId") != chapter_id:
        raise WorkflowError(f"{entry['registry']}: chapterId must be {chapter_id!r}")
    parser = ChapterHtmlParser()
    try:
        html_source = html_path.read_text(encoding="utf-8-sig")
        parser.feed(html_source)
        parser.close()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"Cannot read chapter HTML {entry['html']}: {exc}") from exc
    if not parser.slide_ids:
        raise WorkflowError(f"Chapter contains no data-slide-id sections: {entry['html']}")
    duplicate_slides = sorted({value for value in parser.slide_ids if parser.slide_ids.count(value) > 1})
    if duplicate_slides:
        raise WorkflowError(f"Duplicate slide IDs in {entry['html']}: {duplicate_slides}")
    for slide_id, values in parser.elements.items():
        duplicate_elements = sorted({value for value in values if values.count(value) > 1})
        if duplicate_elements:
            raise WorkflowError(f"Duplicate element IDs in slide {slide_id}: {duplicate_elements}")
    definitions = {name: registry.get(name, {}) for name in ("assets", "equations", "figures", "charts", "tables")}
    for collection, reference, slide_id, latex in parser.references:
        if not isinstance(definitions[collection], dict) or reference not in definitions[collection]:
            raise WorkflowError(f"{entry['html']}: {collection}.{reference} is not defined in the paired registry")
        if collection == "equations" and latex is not None:
            expected = definitions[collection][reference].get("latex") if isinstance(definitions[collection][reference], dict) else None
            if latex.strip() != str(expected).strip():
                raise WorkflowError(f"Equation {reference} data-latex does not match registry latex")
        definition = definitions[collection].get(reference)
        if collection == "equations" and isinstance(definition, dict) and "usedOnSlides" in definition:
            used = definition["usedOnSlides"]
            if not isinstance(used, list) or slide_id not in used:
                raise WorkflowError(f"Equation {reference} does not list slide {slide_id} in usedOnSlides")
    protected = registry.get("protected", {})
    if protected and not isinstance(protected, dict):
        raise WorkflowError(f"{entry['registry']}: protected must be an object")
    all_elements = {value for values in parser.elements.values() for value in values}
    for slide_id in protected.get("slideIds", []):
        if slide_id not in parser.slide_ids:
            raise WorkflowError(f"Registry-protected slide is absent from HTML: {slide_id}")
    for element_id in protected.get("elementIds", []):
        if element_id not in all_elements:
            raise WorkflowError(f"Registry-protected element is absent from HTML: {element_id}")
    for required in protected.get("requiredText", []):
        if required not in "".join(parser.text_chunks):
            raise WorkflowError(f"Registry-protected required text is absent from HTML: {required}")
    return parser, registry


def validate_chapters(root: Path, state: dict[str, Any], *, require_complete: bool = False) -> None:
    if not state["chapters"]:
        raise WorkflowError("No chapters are registered in deck.yaml")
    all_slides: set[str] = set()
    for chapter_id in sorted(state["chapters"]):
        entry = state["chapters"][chapter_id]
        if require_complete and (entry["status"] != "complete" or entry["visualApproval"] != "approved"):
            raise WorkflowError(f"Chapter is not complete and visually approved: {chapter_id}")
        parser, _ = _load_chapter(root, chapter_id, entry)
        duplicates = all_slides.intersection(parser.slide_ids)
        if duplicates:
            raise WorkflowError(f"Slide IDs are duplicated across chapters: {sorted(duplicates)}")
        all_slides.update(parser.slide_ids)


def load_single_section_evidence(
    root: Path, state: dict[str, Any],
) -> dict[str, SectionApprovalEvidence]:
    if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
        raise WorkflowError("Single section validation requires schema v2 single/imported authoring")
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    if not registry_path.is_file():
        raise WorkflowError(f"Authoring registry does not exist: {state['authoring']['registry']}")
    registry = _read_json(registry_path, label="single HTML registry")
    try:
        validate_registry(registry, allow_v1=True)
        return compute_section_approval_evidence(html_path, registry, repository=root)
    except BentoConverterError as exc:
        raise WorkflowError(str(exc)) from exc


def validate_sections(root: Path, state: dict[str, Any], *, require_approved: bool = False) -> dict[str, SectionApprovalEvidence]:
    if not state["sections"]:
        raise WorkflowError("No sections are registered in deck.yaml")
    evidence = load_single_section_evidence(root, state)
    registered = set(state["sections"])
    discovered = set(evidence)
    if registered != discovered:
        raise WorkflowError(
            f"HTML/state section IDs differ; missing in HTML={sorted(registered - discovered)}, "
            f"unregistered in HTML={sorted(discovered - registered)}"
        )
    for section_id, entry in state["sections"].items():
        actual_slides = list(evidence[section_id].slide_ids)
        if (
            entry.get("canonical") == "html"
            and entry["status"] != "html_authoring"
            and entry["slideIds"] != actual_slides
        ):
            raise WorkflowError(
                f"Section {section_id!r} slideIds differ from HTML: "
                f"state={entry['slideIds']}, HTML={actual_slides}"
            )
        if entry["status"] in {"approved", "bento_integration"} and entry["approvalDigest"] != evidence[section_id].digest:
            raise WorkflowError(
                f"Approved section changed after approval: {section_id}; unlock and review it again"
            )
        if require_approved and entry["status"] not in {"approved", "accepted"}:
            raise WorkflowError(f"Section is not approved: {section_id}")
    return evidence


def validate_html_authoring(root: Path, state: dict[str, Any], *, require_approved: bool = False) -> None:
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        validate_sections(root, state, require_approved=require_approved)
    else:
        validate_chapters(root, state, require_complete=require_approved)


def _load_sidecar(path: Path) -> dict[str, Any]:
    return _read_json(path, label="Bento JSON sidecar")


def _atomic_write_bento_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (serialize_bento_doc(document) + "\n").encode("utf-8")
    handle = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def initialize_final_baseline(root: Path, state: dict[str, Any], document: dict[str, Any]) -> None:
    if state["validation"].get("finalBaseline") is not None:
        return
    path = _final_baseline_path(root, state)
    _atomic_write_bento_json(path, document)
    state["validation"]["finalBaseline"] = {
        "path": path.relative_to(root).as_posix(),
        "documentRevision": document_revision(document),
        "protectedContentFingerprint": protected_content_fingerprint(document),
    }


def _baseline_document(
    root: Path,
    state: dict[str, Any],
    generated_document: dict[str, Any],
    *,
    allow_missing: bool,
) -> tuple[dict[str, Any], str]:
    metadata = state["validation"].get("finalBaseline")
    if metadata is None:
        if not allow_missing:
            raise WorkflowError("Final content baseline has not been initialized")
        return generated_document, protected_content_fingerprint(generated_document)
    path_field = "documentPath" if state.get("schemaVersion") == 2 else "path"
    path = _repo_path(root, metadata[path_field], field=f"validation.finalBaseline.{path_field}")
    if not path.is_file():
        raise WorkflowError(f"Final content baseline does not exist: {metadata[path_field]}")
    document = _load_sidecar(path)
    if document_revision(document) != metadata["documentRevision"]:
        raise WorkflowError("Final content baseline document revision does not match deck.yaml")
    fingerprint = protected_content_fingerprint(document)
    if fingerprint != metadata["protectedContentFingerprint"]:
        raise WorkflowError("Final content baseline fingerprint does not match deck.yaml")
    return document, fingerprint


def load_final_baseline(
    root: Path, state: dict[str, Any], generated_document: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Load and verify the immutable finalization baseline recorded in deck.yaml."""

    return _baseline_document(root, state, generated_document, allow_missing=False)


def validate_output_bundle(
    root: Path,
    state: dict[str, Any],
    *,
    require_final: bool,
    allow_missing_baseline: bool = False,
) -> dict[str, Any]:
    outputs = state["outputs"]
    generated_html_path = _repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml")
    generated_json_path = _repo_path(root, outputs["generatedJson"], field="outputs.generatedJson")
    if not generated_html_path.is_file() or not generated_json_path.is_file():
        raise WorkflowError("Generated Bento HTML and JSON sidecar must both exist")
    generated_html = load_html(generated_html_path)
    generated_document = extract_bento_doc(generated_html)
    validate_bento_doc(generated_document)
    if generated_document != _load_sidecar(generated_json_path):
        raise WorkflowError("Generated Bento HTML #bento-doc and JSON sidecar differ")

    output_root = generated_html_path.parent
    registry_output = (
        _repo_path(root, outputs["generatedRegistry"], field="outputs.generatedRegistry")
        if state.get("schemaVersion") == 2 else output_root / "diagnostics/merged-registry.json"
    )
    required = [
        output_root / "conversion-report.json",
        output_root / "diagnostics/computed-layout.json",
        registry_output,
        output_root / "diagnostics/resource-scan.json",
        output_root / "diagnostics/browser-check.json",
        output_root / "diagnostics/browser-environment.json",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise WorkflowError("Required conversion diagnostics are missing: " + ", ".join(missing))
    report = _read_json(required[0], label="conversion report")
    resource_scan = _read_json(required[3], label="resource scan")
    browser_check = _read_json(required[4], label="browser check")
    browser_environment = _read_json(required[5], label="browser environment")
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    if summary.get("criticalElementFail", 0) != 0:
        raise WorkflowError("Conversion report contains critical visual failures")
    if summary.get("unresolvedLocalResourceReferences", 0) != 0:
        raise WorkflowError("Conversion report contains unresolved local resources")
    if resource_scan.get("passed") is not True or resource_scan.get("unresolved"):
        raise WorkflowError("Recursive resource scan did not pass")
    if browser_check.get("serialize_roundtrip") is not True:
        raise WorkflowError("Bento browser serialize round-trip did not pass")
    environment_value = browser_environment.get("browserEnvironment")
    profiles = environment_value.get("profiles", {}) if isinstance(environment_value, dict) else {}
    if (
        browser_environment.get("format") != "bento/browser-environment/v1"
        or not isinstance(browser_environment.get("environmentDigest"), str)
        or not browser_environment["environmentDigest"].startswith("sha256:")
        or not isinstance(profiles, dict)
        or "sourceLayout" not in profiles
        or "bentoCheck" not in profiles
    ):
        raise WorkflowError("Browser environment evidence is missing required profiles or digest")

    result = {
        "generatedDocument": generated_document,
        "generatedRuntime": runtime_fingerprint(generated_html),
        "registry": required[2],
    }
    if not require_final:
        return result

    final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
    final_json_path = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
    if not final_html_path.is_file() or not final_json_path.is_file():
        raise WorkflowError("Final Bento HTML and JSON sidecar must both exist")
    final_html = load_html(final_html_path)
    final_document = extract_bento_doc(final_html)
    if final_document != _load_sidecar(final_json_path):
        raise WorkflowError("Final Bento HTML #bento-doc and JSON sidecar differ")
    if state.get("schemaVersion") == 2:
        final_registry_value = outputs.get("finalRegistry")
        if not isinstance(final_registry_value, str):
            raise WorkflowError("Final registry path is unavailable")
        final_registry_path = _repo_path(root, final_registry_value, field="outputs.finalRegistry")
        if not final_registry_path.is_file():
            raise WorkflowError("Final Bento registry must exist")
        registry = _read_json(final_registry_path, label="final registry")
        validate_registry(registry, allow_v1=False)
    else:
        registry = _read_json(required[2], label="merged registry")
    baseline_document, baseline_fingerprint = _baseline_document(
        root, state, generated_document, allow_missing=allow_missing_baseline,
    )
    if state.get("schemaVersion") == 2:
        baseline = state["validation"].get("finalBaseline")
        if not isinstance(baseline, dict):
            raise WorkflowError("Final registry baseline has not been initialized")
        baseline_registry_path = _repo_path(
            root, baseline["registryPath"], field="validation.finalBaseline.registryPath",
        )
        if not baseline_registry_path.is_file():
            raise WorkflowError("Final registry baseline does not exist")
        baseline_registry = _read_json(baseline_registry_path, label="final registry baseline")
        validate_registry(baseline_registry, allow_v1=False)
        if registry_revision(baseline_registry) != baseline["registryRevision"]:
            raise WorkflowError("Final registry baseline revision does not match deck.yaml")
        if registry != baseline_registry:
            raise WorkflowError("Final registry changed after content approval")
    validate_editor_document(final_document, current=baseline_document, registry=registry, allow_content_edit=False)
    if protected_content_fingerprint(final_document) != baseline_fingerprint:
        raise WorkflowError(
            "Final Bento content/structure differs from its finalization baseline; only presentation edits are allowed"
        )
    if runtime_fingerprint(final_html) != result["generatedRuntime"]:
        raise WorkflowError("Final Bento runtime differs from generated runtime")
    result["finalDocument"] = final_document
    result["finalHtmlRevision"] = file_revision(final_html_path)
    result["finalRegistryRevision"] = registry_revision(registry)
    result["finalRuntimeFingerprint"] = "sha256:" + runtime_fingerprint(final_html)
    return result


def authoring_storage(root: Path, state: dict[str, Any]) -> AuthoringArtifactStorage:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento authoring storage requires deck schema v2")
    outputs = state["outputs"]
    if any(outputs[field] is None for field in ("authoringHtml", "authoringJson", "authoringRegistry")):
        raise WorkflowError("Migrated late-stage decks do not have authoring artifacts")
    target = _repo_path(root, outputs["authoringHtml"], field="outputs.authoringHtml")
    target_registry = _repo_path(root, outputs["authoringRegistry"], field="outputs.authoringRegistry")
    generated = _repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml")
    generated_registry = _repo_path(root, outputs["generatedRegistry"], field="outputs.generatedRegistry")
    # Rolling section promotion may establish authoring before a whole-deck
    # generated bundle exists. Once initialized, the target itself is a safe
    # runtime/registry source for opening revision-checked authoring storage.
    source = generated if generated.is_file() and generated_registry.is_file() else target
    source_registry = generated_registry if generated.is_file() and generated_registry.is_file() else target_registry
    storage = AuthoringArtifactStorage(
        source=source,
        source_registry=source_registry,
        target=target,
        target_registry=target_registry,
        repository=root, state_path=root / STATE_RELATIVE,
    )
    expected_sidecar = _repo_path(root, outputs["authoringJson"], field="outputs.authoringJson")
    if storage.sidecar != expected_sidecar:
        raise WorkflowError("Authoring storage sidecar differs from outputs.authoringJson")
    return storage


def _source_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".pdf": "paper", ".md": "document", ".markdown": "document",
        ".txt": "document", ".html": "html", ".htm": "html",
        ".json": "dataset", ".csv": "dataset", ".tsv": "dataset",
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".svg": "image",
    }.get(suffix, "document")


def _migration_manifest(state: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    used: set[str] = set()

    def add(path: str, role: str, preferred_id: str) -> None:
        source_id = preferred_id
        suffix = 2
        while source_id in used:
            source_id = f"{preferred_id}-{suffix}"
            suffix += 1
        used.add(source_id)
        items.append({"id": source_id, "path": path, "type": _source_type(path), "role": role})

    primary = state["project"].get("primarySource")
    if primary:
        add(primary, "primary", "primary-source")
    for index, path in enumerate(state["project"].get("supplementarySources", []), start=1):
        add(path, "supplementary", f"supplementary-{index}")
    return {"schemaVersion": 1, "authorityMode": "single", "items": items}


def _migration_sections(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for chapter_id, chapter in state["chapters"].items():
        approved = chapter["status"] == "complete" and chapter["visualApproval"] == "approved"
        status = "approved" if approved else chapter["status"]
        if status == "complete":
            status = "review"
        result[chapter_id] = {
            "title": chapter_id,
            "status": status,
            "slideIds": [],
            "bentoSlideIds": [],
            "approvalDigest": None,
        }
    return result


def _migrated_stage_source(stage: str) -> str:
    return STAGE_SOURCE[stage]


def _migration_registry_snapshot(
    root: Path, state: dict[str, Any], source_manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    stage = state["workflow"]["stage"]
    previous_stage = state["workflow"].get("blockedFrom", {}).get("stage") if stage == "blocked" else None
    needs_snapshot = stage in {"bento_validation", "bento_finalization", "complete"} or previous_stage in {
        "bento_validation", "bento_finalization",
    }
    if not needs_snapshot:
        return None, None
    generated_html = _repo_path(root, state["outputs"]["generatedHtml"], field="outputs.generatedHtml")
    registry_path = generated_html.parent / "diagnostics" / "merged-registry.json"
    if not registry_path.is_file():
        raise WorkflowError(f"Late-stage migration requires merged registry: {registry_path}")
    try:
        registry = load_registry(registry_path)
        normalized = normalize_registry(registry, unit_id="deck", source_manifest=source_manifest)
        validate_registry(normalized, allow_v1=False)
    except BentoConverterError as exc:
        raise WorkflowError(f"Late-stage merged registry validation failed: {exc}") from exc
    return normalized, registry_revision(normalized)


def _validate_v1_late_artifacts(root: Path, state: dict[str, Any]) -> None:
    stage = state["workflow"]["stage"]
    previous_stage = state["workflow"].get("blockedFrom", {}).get("stage") if stage == "blocked" else None
    if stage not in {"bento_validation", "bento_finalization", "complete"} and previous_stage not in {
        "bento_validation", "bento_finalization",
    }:
        return
    outputs = state["outputs"]
    final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
    final_json_path = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
    if not final_html_path.is_file() or not final_json_path.is_file():
        raise WorkflowError("Late-stage migration requires the existing final HTML/JSON pair")
    final_document = extract_bento_doc(load_html(final_html_path))
    if final_document != _load_sidecar(final_json_path):
        raise WorkflowError("Late-stage migration final HTML and JSON sidecar differ")
    validate_bento_doc(final_document)
    baseline = state["validation"].get("finalBaseline")
    if not isinstance(baseline, dict):
        raise WorkflowError("Late-stage migration requires the existing final baseline")
    baseline_path = _repo_path(root, baseline["path"], field="validation.finalBaseline.path")
    if not baseline_path.is_file():
        raise WorkflowError(f"Late-stage migration baseline does not exist: {baseline['path']}")
    baseline_document = _load_sidecar(baseline_path)
    if document_revision(baseline_document) != baseline["documentRevision"]:
        raise WorkflowError("Late-stage migration baseline revision does not match deck.yaml")
    if protected_content_fingerprint(baseline_document) != baseline["protectedContentFingerprint"]:
        raise WorkflowError("Late-stage migration baseline fingerprint does not match deck.yaml")


def migrate_v1_state(
    root: Path, state: dict[str, Any], *, dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if state.get("schemaVersion") == 2:
        report = {
            "format": "bento/deck-migration-report/v1", "changed": False,
            "fromSchemaVersion": 2, "toSchemaVersion": 2, "dryRun": dry_run,
        }
        return copy.deepcopy(state), report, load_source_manifest(root, state, require_exists=False)
    if state.get("schemaVersion") != 1:
        raise WorkflowError("Only deck schema v1 can be migrated")

    manifest = _migration_manifest(state)
    manifest_path = root / "sources/source-manifest.yaml"
    if manifest_path.exists():
        try:
            existing_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise WorkflowError(f"Cannot validate existing source manifest before migration: {exc}") from exc
        if existing_manifest != manifest:
            raise WorkflowError("Existing sources/source-manifest.yaml differs from the v1 migration result")
    _validate_v1_late_artifacts(root, state)
    _registry, registry_revision_value = _migration_registry_snapshot(root, state, manifest)
    now = utc_now()
    stage = state["workflow"]["stage"]
    blocked_from = copy.deepcopy(state["workflow"].get("blockedFrom"))
    late_stage = stage in {"bento_finalization", "complete"} or (
        stage == "blocked" and isinstance(blocked_from, dict) and blocked_from["stage"] == "bento_finalization"
    )
    outputs = state["outputs"]
    final_html = Path(outputs["finalHtml"])
    final_registry = final_html.with_name(
        final_html.name[: -len(".bento.html")] + ".registry.json"
        if final_html.name.endswith(".bento.html") else final_html.stem + ".registry.json"
    ).as_posix()
    migrated_workflow = copy.deepcopy(state["workflow"])
    migrated_workflow["sourceOfTruth"] = (
        _migrated_stage_source(blocked_from["stage"]) if stage == "blocked"
        else _migrated_stage_source(stage)
    )
    migrated_workflow["currentSection"] = migrated_workflow.get("currentChapter")
    if blocked_from is not None:
        blocked_from["sourceOfTruth"] = _migrated_stage_source(blocked_from["stage"])
        blocked_from["currentSection"] = blocked_from.get("currentChapter")
        migrated_workflow["blockedFrom"] = blocked_from
    baseline = state["validation"].get("finalBaseline")
    migrated_baseline = None
    if baseline is not None:
        if registry_revision_value is None:
            raise WorkflowError("A v1 final baseline cannot migrate without a validated merged registry")
        migrated_baseline = {
            "documentPath": baseline["path"],
            "documentRevision": baseline["documentRevision"],
            "registryPath": _final_registry_baseline_path(root, {
                "outputs": {"finalHtml": outputs["finalHtml"]}
            }).relative_to(root).as_posix(),
            "registryRevision": registry_revision_value,
            "protectedContentFingerprint": baseline["protectedContentFingerprint"],
        }
    approved_content = late_stage and baseline is not None and registry_revision_value is not None
    bento_content = {
        "status": "approved" if approved_content else "pending",
        "documentRevision": baseline["documentRevision"] if approved_content else None,
        "registryRevision": registry_revision_value if approved_content else None,
        "approvalDigest": (
            content_approval_digest(baseline["documentRevision"], registry_revision_value)
            if approved_content else None
        ),
        "approvedAt": now if approved_content else None,
    }
    final_approval = _pending_final_approval()
    if late_stage and state["approvals"].get("finalBento") == "approved":
        if registry_revision_value is None:
            raise WorkflowError("Approved late-stage final cannot migrate without a registry revision")
        final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
        final_html_value = load_html(final_html_path)
        final_approval = {
            "status": "approved",
            "documentRevision": document_revision(extract_bento_doc(final_html_value)),
            "htmlRevision": file_revision(final_html_path),
            "registryRevision": registry_revision_value,
            "runtimeFingerprint": "sha256:" + runtime_fingerprint(final_html_value),
            "approvedAt": state["validation"].get("checkedAt") or now,
        }
    migrated = {
        "schemaVersion": 2,
        "project": {
            "kind": "paper_explanation",
            "title": state["project"]["title"],
            "request": state["project"]["request"],
            "primarySource": state["project"]["primarySource"],
            "supplementarySources": state["project"]["supplementarySources"],
        },
        "sources": {"manifest": "sources/source-manifest.yaml", "authorityMode": manifest["authorityMode"]},
        "authoring": {
            "mode": "modular", "entryHtml": None, "registry": None,
            "currentSection": migrated_workflow["currentSection"],
        },
        "workflow": migrated_workflow,
        "approvals": {
            **state["approvals"],
            "bentoContent": bento_content,
            "finalBento": final_approval,
        },
        "sections": _migration_sections(state),
        "chapters": copy.deepcopy(state["chapters"]),
        "handoff": {
            "readyForCodex": state["handoff"]["readyForCodex"],
            "readyForBentoAuthoring": stage == "bento_authoring",
            "readyForContentReview": stage == "content_review",
            "readyForFinalEditing": state["handoff"]["readyForFinalEditing"],
        },
        "outputs": {
            "generatedHtml": outputs["generatedHtml"],
            "generatedJson": outputs["generatedJson"],
            "generatedRegistry": str(Path(outputs["generatedHtml"]).parent / "diagnostics/merged-registry.json").replace("\\", "/"),
            "authoringHtml": None if late_stage else "output/presentation.authoring.bento.html",
            "authoringJson": None if late_stage else "output/presentation.authoring.bento.json",
            "authoringRegistry": None if late_stage else "output/presentation.authoring.registry.json",
            "finalHtml": outputs["finalHtml"],
            "finalJson": outputs["finalJson"],
            "finalRegistry": final_registry,
        },
        "preview": copy.deepcopy(state["preview"]),
        "validation": {
            "finalStatus": state["validation"]["finalStatus"],
            "checkedAt": state["validation"]["checkedAt"],
            "finalBaseline": migrated_baseline,
        },
        "migration": {
            "fromSchemaVersion": 1,
            "migratedAt": now,
            "lateStageCompatibility": late_stage,
        },
    }
    validate_state(root, migrated)
    report = {
        "format": "bento/deck-migration-report/v1",
        "changed": True,
        "fromSchemaVersion": 1,
        "toSchemaVersion": 2,
        "dryRun": dry_run,
        "stage": stage,
        "authoringMode": "modular",
        "lateStageCompatibility": late_stage,
        "sourceManifestItems": len(manifest["items"]),
        "registryRevision": registry_revision_value,
    }
    return migrated, report, manifest


def command_migrate(
    root: Path, state: dict[str, Any], *, dry_run: bool, report_path: Path | None,
) -> None:
    try:
        migrated, report, manifest = migrate_v1_state(root, state, dry_run=dry_run)
    except WorkflowError as exc:
        failure_report = {
            "format": "bento/deck-migration-report/v1", "changed": False,
            "fromSchemaVersion": state.get("schemaVersion"), "toSchemaVersion": 2,
            "dryRun": dry_run, "status": "failed", "reasons": [str(exc)],
        }
        if not dry_run:
            _atomic_write_json(report_path or (root / "output/migration-report.json"), failure_report)
        raise
    if dry_run or not report["changed"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    original_state = (root / STATE_RELATIVE).read_bytes()
    backup = root / "deck.v1.backup.yaml"
    if backup.exists() and backup.read_bytes() != original_state:
        raise WorkflowError(f"Migration backup already exists with different content: {backup}")
    manifest_path = _repo_path(root, migrated["sources"]["manifest"], field="sources.manifest")
    manifest_payload = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode("utf-8")
    destination = report_path or (root / "output/migration-report.json")
    payloads: dict[Path, bytes] = {
        root / STATE_RELATIVE: yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    if not backup.exists():
        payloads[backup] = original_state
    if not manifest_path.exists():
        payloads[manifest_path] = manifest_payload
    normalized_registry, _ = _migration_registry_snapshot(root, state, manifest)
    if normalized_registry is not None:
        registry_payload = (
            json.dumps(normalized_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        final_registry = _repo_path(root, migrated["outputs"]["finalRegistry"], field="outputs.finalRegistry")
        payloads[final_registry] = registry_payload
        baseline = migrated["validation"]["finalBaseline"]
        if baseline is not None:
            baseline_registry = _repo_path(root, baseline["registryPath"], field="validation.finalBaseline.registryPath")
            payloads[baseline_registry] = registry_payload
    transaction = ArtifactTransactionStore(root, payloads)

    def validate_migration_commit() -> None:
        installed_state = yaml.safe_load((root / STATE_RELATIVE).read_text(encoding="utf-8-sig"))
        if not isinstance(installed_state, dict):
            raise WorkflowError("Migrated deck.yaml root is not a mapping")
        validate_state(root, installed_state)
        if normalized_registry is not None:
            installed_final_registry = load_registry(
                _repo_path(root, installed_state["outputs"]["finalRegistry"], field="outputs.finalRegistry")
            )
            if installed_final_registry != normalized_registry:
                raise WorkflowError("Migrated final registry snapshot differs from the validated registry")
            baseline_metadata = installed_state["validation"]["finalBaseline"]
            if baseline_metadata is not None:
                installed_baseline_registry = load_registry(
                    _repo_path(root, baseline_metadata["registryPath"], field="validation.finalBaseline.registryPath")
                )
                if installed_baseline_registry != normalized_registry:
                    raise WorkflowError("Migrated registry baseline differs from the validated registry")
                if registry_revision(installed_baseline_registry) != baseline_metadata["registryRevision"]:
                    raise WorkflowError("Migrated registry baseline revision differs from deck.yaml")

    transaction.commit(
        payloads,
        operation="schema-v1-to-v2-migration",
        target_registry_revision=report.get("registryRevision"),
        report_path=destination,
        report_payload=report,
        validate_committed=validate_migration_commit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _effective_state(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Derive revision-bound approval status without mutating deck.yaml."""

    effective = copy.deepcopy(state)
    if state.get("schemaVersion") != 2:
        return effective
    outputs = state["outputs"]
    content_approval = effective["approvals"]["bentoContent"]
    if content_approval["status"] == "approved" and outputs.get("authoringHtml") and outputs.get("authoringRegistry"):
        try:
            authoring_document = extract_bento_doc(load_html(_repo_path(
                root, outputs["authoringHtml"], field="outputs.authoringHtml",
            )))
            if authoring_document != _load_sidecar(_repo_path(
                root, outputs["authoringJson"], field="outputs.authoringJson",
            )):
                raise WorkflowError("Authoring HTML and JSON sidecar differ")
            authoring_registry = _read_json(_repo_path(
                root, outputs["authoringRegistry"], field="outputs.authoringRegistry",
            ), label="authoring registry")
            if (
                content_approval["documentRevision"] != document_revision(authoring_document)
                or content_approval["registryRevision"] != registry_revision(authoring_registry)
            ):
                effective["approvals"]["bentoContent"] = _pending_content_approval()
        except (BentoConverterError, WorkflowError, OSError):
            effective["approvals"]["bentoContent"] = _pending_content_approval()

    final_approval = effective["approvals"]["finalBento"]
    if isinstance(final_approval, dict) and final_approval.get("status") == "approved":
        try:
            final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
            final_registry_path = _repo_path(root, outputs["finalRegistry"], field="outputs.finalRegistry")
            final_html = load_html(final_html_path)
            final_document = extract_bento_doc(final_html)
            if final_document != _load_sidecar(_repo_path(
                root, outputs["finalJson"], field="outputs.finalJson",
            )):
                raise WorkflowError("Final HTML and JSON sidecar differ")
            current_values = {
                "documentRevision": document_revision(final_document),
                "htmlRevision": file_revision(final_html_path),
                "registryRevision": registry_revision(_read_json(final_registry_path, label="final registry")),
                "runtimeFingerprint": "sha256:" + runtime_fingerprint(final_html),
            }
            if any(final_approval.get(field) != value for field, value in current_values.items()):
                effective["approvals"]["finalBento"] = _pending_final_approval()
                effective["validation"]["finalStatus"] = "pending"
                effective["validation"]["checkedAt"] = None
        except (BentoConverterError, WorkflowError, OSError):
            effective["approvals"]["finalBento"] = _pending_final_approval()
            effective["validation"]["finalStatus"] = "pending"
            effective["validation"]["checkedAt"] = None
    return effective


def validate_current_stage(root: Path, state: dict[str, Any]) -> None:
    """Validate the artifacts promised by the current workflow stage."""

    stage = state["workflow"]["stage"]
    if stage in {"initialized", "planning", "blocked"}:
        return
    if stage == "awaiting_plan_approval":
        validate_planning(root)
        return
    if stage in {"html_authoring", "html_review", "ready_for_conversion", "converting"}:
        validate_html_authoring(
            root, state, require_approved=stage in {"ready_for_conversion", "converting"},
        )
        proposal = _html_change(state)
        if proposal and proposal.get("status") == "applied":
            review = _post_apply_review(proposal)
            if review and review.get("status") == "checked":
                _require_current_post_apply_review(root, state)
        return
    if stage == "bento_validation":
        validate_output_bundle(root, state, require_final=state.get("schemaVersion") == 1)
        if state.get("schemaVersion") == 2:
            authoring_storage(root, state).status()
        return
    if stage == "bento_authoring":
        authoring_storage(root, state).status()
        return
    if stage == "content_review":
        _validated_content_review_status(root, state)
        return
    if stage in {"bento_finalization", "complete"}:
        bundle = validate_output_bundle(root, state, require_final=True)
        if stage == "complete":
            approval = state["approvals"]["finalBento"]
            if not isinstance(approval, dict) or approval.get("status") != "approved":
                raise WorkflowError("Completed schema v2 deck requires revision-bound final approval")
            current = _final_approval_snapshot(bundle, approved_at=approval["approvedAt"])
            for field in ("documentRevision", "htmlRevision", "registryRevision", "runtimeFingerprint"):
                if approval.get(field) != current[field]:
                    raise WorkflowError("Completed deck has stale final approval")
        return
    raise WorkflowError(f"Unsupported workflow stage: {stage}")


def command_status(root: Path, state: dict[str, Any], *, as_json: bool) -> None:
    state = _effective_state(root, state)
    if as_json:
        # ASCII-safe JSON survives Windows PowerShell 5 native-process decoding;
        # ConvertFrom-Json restores the original Unicode path strings.
        print(json.dumps(state, ensure_ascii=True, indent=2))
        return
    summary = user_status_summary(state)
    print(f"現在: {summary['current']}")
    if summary.get("section"):
        print(f"対象: {summary['section']}")
    print(f"次: {summary['next']}")
    print(f"開くもの: {summary['route']}")
    if summary["validActions"]:
        print("できること:")
        for action in summary["validActions"]:
            print(f"- {action}")
    if summary.get("blockingReason"):
        print(f"停止理由: {summary['blockingReason']}")


def workspace_route(state: dict[str, Any]) -> str:
    stage = state["workflow"]["stage"]
    if stage in {"planning", "html_authoring", "html_review"}:
        return "html-preview"
    if stage in {"bento_authoring", "content_review"}:
        return "authoring-editor"
    if stage == "bento_finalization":
        return "final-editor"
    if stage == "complete":
        return "final-viewer"
    return "none"


def user_status_summary(state: dict[str, Any]) -> dict[str, Any]:
    stage = state["workflow"]["stage"]
    labels = {
        "initialized": ("資料の準備前", "依頼内容と参照資料を確認します"),
        "planning": ("構成を作成中", "構成案を確認できる状態にします"),
        "awaiting_plan_approval": ("構成案の確認待ち", "内容を確認して承認または修正を伝えてください"),
        "html_authoring": ("現在のセクションをHTMLで作成中", "見た目を確認できる状態にします"),
        "html_review": ("現在のセクションのHTML確認", "承認後にBentoへ取り込みます"),
        "ready_for_conversion": ("従来方式の全体変換待ち", "BentoSlideへ変換します"),
        "converting": ("BentoSlideへ変換中", "変換結果を検証します"),
        "bento_validation": ("BentoSlideを検証中", "編集可能な状態へ進めます"),
        "bento_authoring": ("現在のセクションをBentoSlideで編集中", "仕上がったらセクションを確定します"),
        "content_review": ("資料全体の内容確認", "内容承認後に最終調整へ進みます"),
        "bento_finalization": ("資料全体を最終調整中", "技術検証と最終承認を行います"),
        "complete": ("資料は完成", "完成版を閲覧できます"),
        "blocked": ("処理を停止中", "原因を解消して再開します"),
    }
    if _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
        labels.update({
            "html_authoring": ("資料全体のHTMLを作成中", "資料全体を確認できる状態にします"),
            "html_review": ("資料全体のHTML確認", "変更案を確認するか、全体をBentoへ進めます"),
            "ready_for_conversion": ("資料全体のHTML承認済み", "BentoSlideへ変換します"),
        })
    current, next_action = labels.get(stage, (stage, "状態を確認します"))
    current_id = state["workflow"].get("currentSection") or state["workflow"].get("currentChapter")
    section = state.get("sections", {}).get(current_id, {}).get("title") if current_id else None
    return {
        "current": current, "section": section or current_id, "next": next_action,
        "route": workspace_route(state), "validActions": valid_actions(state),
        "blockingReason": state["workflow"].get("blockingReason"),
    }


def valid_actions(state: dict[str, Any]) -> list[str]:
    """Return deterministic high-level operations; user wording is resolved by the agent."""

    stage = state["workflow"]["stage"]
    if stage == "initialized":
        return ["capture-request", "advance"]
    if stage == "planning":
        return ["edit-current", "advance"]
    if stage == "awaiting_plan_approval":
        return ["approve-current", "edit-current"]
    if stage == "html_authoring":
        return ["edit-current", "advance"]
    if stage == "html_review":
        if _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
            proposal = _html_change(state)
            if proposal and proposal.get("status") == "proposed":
                if proposal.get("format") != HTML_CHANGE_FORMAT:
                    return ["cancel-html-change"]
                return ["approve-html-change", "cancel-html-change"]
            if proposal and proposal.get("status") == "approved":
                if proposal.get("format") != HTML_CHANGE_FORMAT:
                    return ["cancel-html-change"]
                return ["apply-html-change", "cancel-html-change"]
            if proposal and proposal.get("status") == "applied":
                if proposal.get("format") != HTML_CHANGE_FORMAT:
                    return ["adopt-whole-deck"]
                review = _post_apply_review(proposal)
                if not review or review.get("status") != "checked":
                    return ["check-html-change"]
            if _html_review_baseline(state) is None:
                return ["adopt-whole-deck"]
            return ["approve-current", "edit-current", "propose-html-change"]
        current = state["workflow"].get("currentSection")
        entry = state.get("sections", {}).get(current, {}) if current else {}
        return (
            ["advance", "promote-current-section", "edit-current"]
            if entry.get("status") == "bento_integration"
            else ["approve-current", "edit-current"]
        )
    if stage == "bento_authoring":
        return ["edit-current", "finish-current-section", "reopen-current-section"]
    if stage == "content_review":
        return (
            ["advance", "reopen-current-section"]
            if state["approvals"]["bentoContent"]["status"] == "approved"
            else ["approve-current", "edit-current", "reopen-current-section"]
        )
    if stage == "bento_finalization":
        return (
            ["advance", "reopen-current-section"]
            if _final_approval_status(state["approvals"]["finalBento"]) == "approved"
            else ["approve-current", "edit-current", "reopen-current-section"]
        )
    if stage == "complete":
        return ["reopen-current-section", "reopen-finalization"]
    if stage == "blocked":
        return ["resume"]
    return []


def command_capture_request(root: Path, state: dict[str, Any], *, text: str) -> None:
    cleaned = text.strip()
    if not cleaned:
        raise WorkflowError("Request text must not be blank")
    payload = (
        "# Presentation request\n\n"
        "This file is the persisted brief captured from the conversation.\n\n"
        "## Request\n\n" + cleaned + "\n"
    ).encode("utf-8")
    with planning_action_guard(root, state) as lease:
        destination = _repo_path(root, state["project"]["request"], field="project.request")
        ArtifactTransactionStore(
            root, (destination,), inherited_writer_lease=lease,
        ).commit(
            {destination: payload}, operation="capture-presentation-request",
        )
        append_work_log(root, "Captured the current presentation request in REQUEST.md")


def _validate_planning_artifact_payload(artifact: str, payload: bytes) -> None:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkflowError("Planning artifacts must be valid UTF-8") from exc
    if "\x00" in text:
        raise WorkflowError("Planning artifacts must not contain NUL characters")
    if artifact != "visual-plan":
        return
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"Cannot parse visual plan: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError("Visual plan root must be an object")
    try:
        validate_visual_plan(value)
    except BentoConverterError as exc:
        raise WorkflowError(str(exc)) from exc


def command_write_planning_artifacts(
    root: Path,
    state: dict[str, Any],
    payloads: Mapping[str, bytes],
    *,
    inherited_writer_lease: WriterLease | None = None,
) -> None:
    """Transactionally write only known planning files under the review lease."""

    if not payloads:
        raise WorkflowError("At least one planning artifact is required")
    unknown = sorted(set(payloads) - set(PLANNING_ARTIFACT_FILES))
    if unknown:
        raise WorkflowError("Unknown planning artifact: " + ", ".join(unknown))
    normalized = {artifact: bytes(payload) for artifact, payload in payloads.items()}
    for artifact, payload in normalized.items():
        _validate_planning_artifact_payload(artifact, payload)

    with planning_action_guard(
        root, state, inherited_writer_lease=inherited_writer_lease,
    ) as lease:
        _require_stage(state, "planning", "awaiting_plan_approval")
        destinations = {
            artifact: (root / PLANNING_ARTIFACT_FILES[artifact]).resolve()
            for artifact in normalized
        }
        transaction_payloads = {
            destinations[artifact]: payload for artifact, payload in normalized.items()
        }
        ArtifactTransactionStore(
            root, tuple(destinations.values()), inherited_writer_lease=lease,
        ).commit(
            transaction_payloads, operation="write-planning-artifacts",
        )
        append_work_log(
            root, "Updated planning artifacts: " + ", ".join(sorted(normalized)),
        )


def command_route(state: dict[str, Any], *, as_json: bool) -> None:
    payload = {"route": workspace_route(state), **user_status_summary(state)}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if as_json else payload["route"])


def command_advance(
    root: Path, state: dict[str, Any], *, browser_executable: Path | None = None,
    browser_check: bool = True,
) -> None:
    """Advance mechanical work only; never record an implicit human approval."""

    stage = state["workflow"]["stage"]
    if stage == "initialized":
        command_initialize(root, state)
        return
    if stage == "planning":
        command_submit_plan(root, state)
        return
    if stage == "html_authoring":
        if _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
            command_complete_html_deck(root, state)
            return
        section_id, entry = _rolling_section(state)
        if entry["status"] == "planned":
            command_begin_section(root, state, section_id)
            state = load_state(root)
        command_complete_section(root, state, section_id)
        return
    if stage == "html_review":
        if _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
            proposal = _html_change(state)
            if proposal and proposal.get("status") == "approved":
                command_apply_html_change(root, state)
                return
            if proposal and proposal.get("status") == "applied":
                review = _post_apply_review(proposal)
                if not review or review.get("status") != "checked":
                    command_check_html_change(
                        root, state, browser_executable=browser_executable,
                    )
                    return
            raise WorkflowError("The whole-deck HTML or its pending change still needs explicit approval")
        _, entry = _rolling_section(state)
        if entry["status"] == "bento_integration":
            command_promote_current_section(
                root, state, browser_executable=browser_executable, browser_check=browser_check,
            )
            return
        raise WorkflowError("The current HTML still needs explicit approval")
    if stage == "content_review" and state["approvals"]["bentoContent"]["status"] == "approved":
        if state.get("schemaVersion") == 2 and state["validation"].get("finalBaseline") is not None:
            _initialize_v2_finalization(
                root, state, archive_existing=True, require_archive=False,
            )
            append_work_log(
                root,
                "Re-entered finalization from approved authoring content with safe archive when required",
            )
        else:
            command_begin_finalization(root, state)
        return
    if stage == "bento_finalization" and _final_approval_status(state["approvals"]["finalBento"]) == "approved":
        command_complete(root, state)
        return
    if stage in {"awaiting_plan_approval", "bento_authoring", "content_review", "bento_finalization"}:
        raise WorkflowError("The workflow is at a human checkpoint; use approve-current or request a revision")
    raise WorkflowError(f"advance has no safe automatic action in stage {stage!r}")


def command_set_project(root: Path, state: dict[str, Any], *, kind: str, title: str) -> None:
    if not isinstance(kind, str) or not PROJECT_KIND_PATTERN.fullmatch(kind):
        raise WorkflowError("project kind must match ^[a-z][a-z0-9_-]*$")
    if (
        not isinstance(title, str)
        or not title
        or title != title.strip()
        or "\r" in title
        or "\n" in title
    ):
        raise WorkflowError("project title must be a non-empty single line without outer whitespace")

    with planning_action_guard(root, state):
        if state.get("schemaVersion") != 2:
            raise WorkflowError("set-project requires deck schema v2")
        _require_stage(state, "initialized", "planning")
        next_state = copy.deepcopy(state)
        next_state["project"]["kind"] = kind
        next_state["project"]["title"] = title
        atomic_write_state(root, next_state)
        append_work_log(root, f"Set project metadata: kind={kind!r}, title={title!r}")


def command_initialize(root: Path, state: dict[str, Any]) -> None:
    with planning_action_guard(root, state):
        _require_stage(state, "initialized")
        ensure_source_manifest(root, state)
        selected, _ = discover_source_candidates(root, state)
        state["project"]["primarySource"] = selected.relative_to(root).as_posix() if selected else None
        _transition(state, "planning", "in_progress")
        atomic_write_state(root, state)
        append_work_log(root, f"Initialized planning with primary source {state['project']['primarySource']}")


def command_configure_chapters(
    root: Path,
    state: dict[str, Any],
    chapter_ids: Iterable[str],
    *,
    inherited_writer_lease: WriterLease | None = None,
) -> None:
    values = list(dict.fromkeys(chapter_ids))
    if not values or any(not CHAPTER_PATTERN.fullmatch(value) for value in values):
        raise WorkflowError("Chapter IDs must use chapter-XX names")
    with planning_action_guard(
        root, state, inherited_writer_lease=inherited_writer_lease,
    ):
        _require_stage(state, "planning", "awaiting_plan_approval")
        if state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular":
            raise WorkflowError("configure-chapters is only available in modular authoring mode")
        for existing, entry in state["chapters"].items():
            if existing not in values and entry["status"] != "planned":
                raise WorkflowError(f"Cannot remove chapter after authoring has begun: {existing}")
        state["chapters"] = {
            chapter_id: state["chapters"].get(chapter_id, {
                "html": f"chapters/{chapter_id}.preview.html",
                "registry": f"chapters/{chapter_id}.registry.json",
                "status": "planned",
                "visualApproval": "pending",
            })
            for chapter_id in values
        }
        atomic_write_state(root, state)
        append_work_log(root, "Configured chapters: " + ", ".join(values))


def _configured_sections(
    state: dict[str, Any], sections: Sequence[Mapping[str, Any]], *, require_slide_ids: bool = True,
) -> dict[str, dict[str, Any]]:
    values = [str(section["id"]) for section in sections]
    if not values or len(values) != len(set(values)):
        raise WorkflowError("Planning sections must have unique stable IDs")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) for value in values):
        raise WorkflowError("Section IDs must use stable alphanumeric, dot, underscore, or hyphen names")
    for existing, entry in state["sections"].items():
        if existing not in values and entry["status"] != "planned":
            raise WorkflowError(f"Cannot remove section after authoring has begun: {existing}")
    configured: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_id = str(section["id"])
        title = section.get("title")
        slide_ids = section.get("slideIds")
        if not isinstance(title, str) or not title.strip() or title != title.strip():
            raise WorkflowError(f"Section title is invalid: {section_id}")
        if not isinstance(slide_ids, list) or (require_slide_ids and not slide_ids) or any(
            not isinstance(slide_id, str) or not slide_id for slide_id in slide_ids
        ):
            raise WorkflowError(f"Section slideIds are invalid: {section_id}")
        entry = copy.deepcopy(state["sections"].get(section_id, {
            "title": section_id,
            "status": "planned",
            "canonical": "planning",
            "slideIds": [],
            "bentoSlideIds": [],
            "approvalDigest": None,
            "bentoDocumentRevision": None,
            "bentoRegistryRevision": None,
            "bentoSectionDigest": None,
            "acceptedAt": None,
        }))
        if entry["status"] != "planned":
            raise WorkflowError(f"Cannot replace section after authoring has begun: {section_id}")
        entry.update(title=title, slideIds=list(slide_ids))
        configured[section_id] = entry
    return configured


def command_configure_sections(
    root: Path,
    state: dict[str, Any],
    section_ids: Iterable[str],
    *,
    inherited_writer_lease: WriterLease | None = None,
) -> None:
    values = list(dict.fromkeys(section_ids))
    if not values or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) for value in values):
        raise WorkflowError("Section IDs must use stable alphanumeric, dot, underscore, or hyphen names")
    with planning_action_guard(
        root, state, inherited_writer_lease=inherited_writer_lease,
    ):
        _require_stage(state, "planning", "awaiting_plan_approval")
        if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
            raise WorkflowError("configure-sections requires schema v2 single/imported authoring")
        state["sections"] = _configured_sections(state, [
            {
                "id": section_id,
                "title": str(state["sections"].get(section_id, {}).get("title") or section_id),
                "slideIds": list(state["sections"].get(section_id, {}).get("slideIds") or []),
            }
            for section_id in values
        ], require_slide_ids=False)
        atomic_write_state(root, state)
        append_work_log(root, "Configured sections: " + ", ".join(values))


def command_apply_planning_proposal(
    root: Path,
    state: dict[str, Any],
    *,
    candidate_payloads: Mapping[str, bytes],
    candidate_sections: Sequence[Mapping[str, Any]],
    expected_base_planning_signature: str,
    expected_candidate_planning_signature: str,
    proposal_path: Path,
    expected_proposal_revision: str,
    applied_proposal_payload: bytes,
    inherited_writer_lease: WriterLease | None = None,
    fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Atomically apply one reviewed planning snapshot and its section membership."""

    candidate = validate_planning_candidate(candidate_payloads, candidate_sections)
    if not hmac.compare_digest(candidate.signature, expected_candidate_planning_signature):
        raise PlanningRevisionConflict(
            "Planning Candidateが更新されています。最新の変更案を読み直してください。"
        )
    resolved_proposal = proposal_path.resolve()
    try:
        proposal_relative = resolved_proposal.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkflowError("Planning Proposal metadata must remain inside the repository") from exc
    parts = proposal_relative.parts
    if (
        len(parts) != 4
        or parts[0] != ".bento-ai"
        or parts[1] != "runs"
        or not re.fullmatch(r"[0-9a-f]{32}", parts[2])
        or parts[3] != "proposal.json"
        or resolved_proposal.is_symlink()
    ):
        raise WorkflowError("Planning Proposal metadata location is invalid")
    try:
        applied_value = json.loads(applied_proposal_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("Applied Planning Proposal metadata is invalid") from exc
    if not isinstance(applied_value, dict) or applied_value.get("status") != "applied":
        raise WorkflowError("Applied Planning Proposal metadata must record applied status")

    with planning_action_guard(
        root, state, inherited_writer_lease=inherited_writer_lease,
    ) as lease:
        _require_stage(state, "planning")
        if state.get("schemaVersion") != 2 or state.get("authoring", {}).get("mode") not in {"single", "imported"}:
            raise WorkflowError("AI Planning Proposal requires schema v2 single/imported authoring")
        current_signature = planning_review_signature(root, state)
        if not hmac.compare_digest(current_signature, expected_base_planning_signature):
            raise PlanningRevisionConflict(
                "現在のplanningが変更されています。最新の内容から変更案を作り直してください。"
            )
        if file_revision(resolved_proposal) != expected_proposal_revision:
            raise PlanningRevisionConflict(
                "Planning Proposalの状態が更新されています。最新の変更案を読み直してください。"
            )

        next_state = copy.deepcopy(state)
        section_values = [
            {"id": section.id, "title": section.title, "slideIds": list(section.slide_ids)}
            for section in candidate.sections
        ]
        next_state["sections"] = _configured_sections(next_state, section_values)
        validate_state(root, next_state)
        deck_path = (root / STATE_RELATIVE).resolve()
        work_log = (root / WORK_LOG_RELATIVE).resolve()
        planning_targets = {
            name: (root / PLANNING_ARTIFACT_FILES[name]).resolve()
            for name in candidate.artifacts
        }
        payloads: dict[Path, bytes] = {
            deck_path: _state_payload(next_state),
            work_log: _work_log_payload(root, "Applied the reviewed AI Planning Proposal"),
            resolved_proposal: bytes(applied_proposal_payload),
            **{
                planning_targets[name]: payload
                for name, payload in candidate.artifacts.items()
            },
        }
        store = ArtifactTransactionStore(
            root,
            tuple(payloads),
            inherited_writer_lease=lease,
            fault_injector=fault_injector,
        )

        def validate_base() -> None:
            fresh = load_state(root)
            if not hmac.compare_digest(
                planning_review_signature(root, fresh), expected_base_planning_signature,
            ):
                raise PlanningRevisionConflict(
                    "現在のplanningが変更されています。最新の内容から変更案を作り直してください。"
                )
            if file_revision(resolved_proposal) != expected_proposal_revision:
                raise PlanningRevisionConflict(
                    "Planning Proposalの状態が更新されています。最新の変更案を読み直してください。"
                )
            if not hmac.compare_digest(
                planning_candidate_signature(candidate.artifacts, candidate.sections),
                expected_candidate_planning_signature,
            ):
                raise PlanningRevisionConflict(
                    "Planning Candidateが更新されています。最新の変更案を読み直してください。"
                )

        def validate_committed() -> None:
            installed = load_state(root)
            validate_planning(root)
            if installed.get("sections") != next_state.get("sections"):
                raise WorkflowError("Applied Planning Proposal section state differs after commit")
            for name, target in planning_targets.items():
                if target.read_bytes() != candidate.artifacts[name]:
                    raise WorkflowError("Applied Planning Proposal artifact differs after commit")
            if resolved_proposal.read_bytes() != applied_proposal_payload:
                raise WorkflowError("Applied Planning Proposal status differs after commit")

        result = store.commit(
            payloads,
            operation="apply-ai-planning-proposal",
            validate_base=validate_base,
            validate_committed=validate_committed,
        )
        state.clear()
        state.update(next_state)
        return result


def command_submit_plan(
    root: Path,
    state: dict[str, Any],
    *,
    expected_planning_signature: str | None = None,
    inherited_writer_lease: WriterLease | None = None,
) -> None:
    with planning_action_guard(root, state, inherited_writer_lease=inherited_writer_lease):
        _require_stage(state, "planning")
        validate_planning(root)
        if not _planned_units(state):
            raise WorkflowError("Register the planned sections or chapters before requesting approval")
        current_signature = planning_review_signature(root, state)
        if expected_planning_signature is not None and not hmac.compare_digest(
            current_signature, expected_planning_signature,
        ):
            raise PlanningRevisionConflict(
                "構成案が更新されています。最新のStoryboardを読み直してください。"
            )
        _transition(state, "awaiting_plan_approval", "awaiting_approval")
        atomic_write_state(root, state)
        append_work_log(root, "Submitted explanation policy, story outline, and slide plan for approval")


def command_approve_plan(
    root: Path,
    state: dict[str, Any],
    *,
    expected_planning_signature: str | None = None,
    inherited_writer_lease: WriterLease | None = None,
) -> None:
    with planning_action_guard(root, state, inherited_writer_lease=inherited_writer_lease):
        _require_stage(state, "awaiting_plan_approval")
        validate_planning(root)
        single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular"
        planned_units = _planned_units(state)
        if not planned_units:
            raise WorkflowError("No sections or chapters are configured")
        current_signature = planning_review_signature(root, state)
        if expected_planning_signature is not None and not hmac.compare_digest(
            current_signature, expected_planning_signature,
        ):
            raise PlanningRevisionConflict(
                "構成案が更新されています。最新のStoryboardを読み直してください。"
            )
        for key in ("explanationPolicy", "storyOutline", "slidePlan"):
            state["approvals"][key] = "approved"
        first = next(iter(planned_units))
        if single and _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
            state["authoring"]["htmlChange"] = None
            state["authoring"]["htmlReview"] = None
            for entry in state["sections"].values():
                entry.update({"status": "html_authoring", "canonical": "html", "approvalDigest": None})
            _transition(state, "html_authoring", "in_progress", current=None)
        else:
            if single and state["sections"][first].get("canonical") is not None:
                state["sections"][first].update({"status": "html_authoring", "canonical": "html"})
            _transition(state, "html_authoring", "in_progress", current=first)
        atomic_write_state(root, state)
        append_work_log(root, "Recorded plan approval and opened HTML authoring")


def _select_section(state: dict[str, Any], requested: str | None) -> str:
    if requested:
        if requested not in state["sections"]:
            raise WorkflowError(f"Section is not registered: {requested}")
        return requested
    current = state["workflow"].get("currentSection")
    if current and state["sections"][current]["status"] not in {"approved", "accepted"}:
        return current
    for section_id, entry in state["sections"].items():
        if entry["status"] not in {"approved", "accepted"}:
            return section_id
    raise WorkflowError("All registered sections are approved")


def command_begin_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    section_id = _select_section(state, requested)
    entry = state["sections"][section_id]
    if entry["status"] not in {"planned", "authoring", "html_authoring"}:
        raise WorkflowError(f"Section cannot enter authoring from status {entry['status']!r}: {section_id}")
    entry["status"] = "html_authoring" if entry.get("canonical") is not None else "authoring"
    if entry.get("canonical") is not None:
        entry["canonical"] = "html"
    entry["approvalDigest"] = None
    _transition(state, "html_authoring", "in_progress", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Began authoring section {section_id}")


def command_complete_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    section_id = _select_section(state, requested)
    if state["sections"][section_id]["status"] not in {"authoring", "html_authoring"}:
        raise WorkflowError(f"Section is not in authoring: {section_id}")
    evidence = load_single_section_evidence(root, state)
    if set(evidence) != set(state["sections"]):
        raise WorkflowError("Single HTML section IDs must exactly match deck.yaml before review")
    current = evidence[section_id]
    entry = state["sections"][section_id]
    entry["slideIds"] = list(current.slide_ids)
    entry["status"] = "html_review" if entry.get("canonical") is not None else "review"
    entry["approvalDigest"] = None
    _transition(state, "html_review", "awaiting_approval", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Validated section {section_id} and requested visual approval")


def command_approve_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_review")
    section_id = requested or state["workflow"].get("currentSection")
    if not section_id or section_id not in state["sections"]:
        raise WorkflowError("No current section is available for visual approval")
    entry = state["sections"][section_id]
    if entry["status"] not in {"review", "html_review"}:
        raise WorkflowError(f"Section is not awaiting visual approval: {section_id}")
    evidence = load_single_section_evidence(root, state)
    if set(evidence) != set(state["sections"]):
        raise WorkflowError("Single HTML section IDs must exactly match deck.yaml before approval")
    current = evidence[section_id]
    if entry["slideIds"] != list(current.slide_ids):
        raise WorkflowError(f"Section slide membership changed during review: {section_id}")
    # Revalidate every previously approved section. A global CSS/theme edit will
    # change every digest and cannot silently ride along with one section review.
    for approved_id, approved in state["sections"].items():
        if approved["status"] == "approved" and approved["approvalDigest"] != evidence[approved_id].digest:
            raise WorkflowError(f"Approved section changed after approval: {approved_id}; unlock it first")
    entry["status"] = "approved"
    if entry.get("canonical") is not None:
        entry["canonical"] = "html"
    entry["approvalDigest"] = current.digest
    remaining = [key for key, value in state["sections"].items() if value["status"] != "approved"]
    if remaining:
        next_section = remaining[0]
        state["sections"][next_section]["status"] = "authoring"
        if state["sections"][next_section].get("canonical") is not None:
            state["sections"][next_section]["canonical"] = "html"
        _transition(state, "html_authoring", "in_progress", current=next_section)
    else:
        validate_sections(root, state, require_approved=True)
        state["handoff"]["readyForCodex"] = True
        _transition(state, "ready_for_conversion", "ready")
    atomic_write_state(root, state)
    append_work_log(root, f"Approved visual composition for section {section_id}")


def command_unlock_section(root: Path, state: dict[str, Any], section_id: str) -> None:
    _require_stage(state, "html_authoring", "html_review", "ready_for_conversion")
    if section_id not in state["sections"]:
        raise WorkflowError(f"Section is not registered: {section_id}")
    entry = state["sections"][section_id]
    if entry["status"] != "approved":
        raise WorkflowError(f"Section is not approved: {section_id}")
    entry["status"] = "authoring"
    entry["approvalDigest"] = None
    state["handoff"]["readyForCodex"] = False
    _transition(state, "html_authoring", "in_progress", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Unlocked section {section_id} for HTML authoring")


def _whole_deck_evidence(
    root: Path, state: dict[str, Any], *, html_path: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> HtmlDeckStructureEvidence:
    if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
        raise WorkflowError("Whole-deck HTML authoring requires schema v2 single/imported mode")
    source = html_path or _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_value = registry or _read_json(
        _repo_path(root, state["authoring"]["registry"], field="authoring.registry"),
        label="single HTML registry",
    )
    evidence = compute_html_deck_structure_evidence(source, registry_value, repository=root)
    if set(evidence.section_digests) != set(state["sections"]):
        raise WorkflowError(
            "Whole-deck HTML section IDs must exactly match deck.yaml: "
            f"state={sorted(state['sections'])}, HTML={sorted(evidence.section_digests)}"
        )
    return evidence


def _review_dependency_paths(root: Path, revisions: dict[str, str]) -> tuple[Path, ...]:
    return tuple(
        _repo_path(root, relative, field=f"HTML review dependency {relative}")
        for relative in sorted(revisions)
    )


def _whole_deck_review_record(
    root: Path,
    state: dict[str, Any],
    evidence: HtmlDeckStructureEvidence,
    *,
    source: str,
    proposal_digest: str | None = None,
    html_revision: str | None = None,
    registry_revision_value: str | None = None,
) -> dict[str, Any]:
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    return {
        "format": HTML_DECK_REVIEW_FORMAT,
        "htmlRevision": html_revision or file_revision(html_path),
        "registryRevision": registry_revision_value or file_revision(registry_path),
        "evidenceDigest": evidence.review_digest,
        "dependencyRevisions": dict(evidence.dependency_hashes),
        "source": source,
        "proposalDigest": proposal_digest,
        "openedAt": utc_now(),
    }


def _require_current_html_review(
    root: Path, state: dict[str, Any],
) -> HtmlDeckStructureEvidence:
    baseline = _html_review_baseline(state)
    if not baseline:
        raise WorkflowError(
            "Open a whole-deck HTML review before approval or proposing changes"
        )
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    if file_revision(html_path) != baseline["htmlRevision"]:
        raise WorkflowError("Whole-deck HTML review baseline is stale for the canonical HTML")
    if file_revision(registry_path) != baseline["registryRevision"]:
        raise WorkflowError("Whole-deck HTML review baseline is stale for the canonical registry")
    for relative, expected in baseline["dependencyRevisions"].items():
        path = _repo_path(root, relative, field=f"authoring.htmlReview dependency {relative}")
        if file_revision(path) != expected:
            raise WorkflowError(f"Whole-deck HTML review dependency changed after review opened: {relative}")
    registry = _read_json(registry_path, label="canonical HTML registry")
    evidence = _whole_deck_evidence(root, state, html_path=html_path, registry=registry)
    if evidence.review_digest != baseline["evidenceDigest"]:
        raise WorkflowError("Whole-deck HTML review evidence changed after review opened")
    if evidence.dependency_hashes != baseline["dependencyRevisions"]:
        raise WorkflowError("Whole-deck HTML review dependency set changed after review opened")
    return evidence


def _set_whole_deck_html_review(
    root: Path,
    state: dict[str, Any],
    evidence: HtmlDeckStructureEvidence,
    *,
    source: str,
    proposal_digest: str | None = None,
    html_revision: str | None = None,
    registry_revision_value: str | None = None,
) -> None:
    for section_id, section_digest in evidence.section_digests.items():
        slide_ids = [
            slide_id for slide_id in evidence.ordered_slide_ids
            if evidence.slide_section_ids[slide_id] == section_id
        ]
        state["sections"][section_id].update({
            "status": "html_review", "canonical": "html",
            "slideIds": slide_ids, "bentoSlideIds": [],
            "approvalDigest": None, "bentoDocumentRevision": None,
            "bentoRegistryRevision": None, "bentoSectionDigest": None,
            "acceptedAt": None,
        })
    state["authoring"]["htmlReview"] = _whole_deck_review_record(
        root,
        state,
        evidence,
        source=source,
        proposal_digest=proposal_digest,
        html_revision=html_revision,
        registry_revision_value=registry_revision_value,
    )
    state["handoff"]["readyForCodex"] = False
    _transition(state, "html_review", "awaiting_approval", current=None)


def command_adopt_whole_deck(root: Path, state: dict[str, Any]) -> None:
    """Adopt an existing pre-conversion full HTML deck without touching its bytes."""

    _require_stage(state, "html_authoring", "html_review")
    proposal = _html_change(state)
    adoptable_legacy_apply = bool(
        proposal
        and proposal.get("format") != HTML_CHANGE_FORMAT
        and proposal.get("status") == "applied"
    )
    if _has_unfinished_html_change(state) and not adoptable_legacy_apply:
        raise WorkflowError("Resolve the current HTML change before adopting the deck")
    if (
        state["workflow"]["stage"] == "html_review"
        and _authoring_strategy(state) == WHOLE_DECK_STRATEGY
        and _html_review_baseline(state) is not None
    ):
        raise WorkflowError(
            "A whole-deck review is already open; use a reviewed candidate for changes"
        )
    if any(
        entry["status"] in {"bento_integration", "bento_authoring", "accepted"}
        or entry.get("bentoSlideIds")
        for entry in state["sections"].values()
    ):
        raise WorkflowError("Cannot adopt whole-deck authoring after section Bento promotion has begun")
    evidence = _whole_deck_evidence(root, state)
    next_state = copy.deepcopy(state)
    next_state["authoring"]["strategy"] = WHOLE_DECK_STRATEGY
    next_state["authoring"]["htmlChange"] = None
    _set_whole_deck_html_review(root, next_state, evidence, source="adopted-deck")
    atomic_write_state(root, next_state)
    append_work_log(root, "Adopted the existing full HTML deck as one review checkpoint")


def command_complete_html_deck(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "html_authoring")
    if _authoring_strategy(state) != WHOLE_DECK_STRATEGY:
        raise WorkflowError("complete-html-deck requires whole-deck authoring")
    if _has_active_html_change(state):
        raise WorkflowError("Resolve the active HTML change proposal before completing the deck")
    evidence = _whole_deck_evidence(root, state)
    next_state = copy.deepcopy(state)
    _set_whole_deck_html_review(root, next_state, evidence, source="completed-authoring")
    atomic_write_state(root, next_state)
    append_work_log(root, "Validated the complete HTML deck and opened one whole-deck review")


def command_apply_initial_html_candidate(
    root: Path,
    state: dict[str, Any],
    *,
    candidate_html: Path,
    candidate_registry: Path,
    expected_base_planning_signature: str,
    expected_state_revision: str,
    expected_input_revisions: Mapping[str, str | None],
    expected_candidate_html_revision: str,
    expected_candidate_registry_revision: str,
    expected_candidate_review_digest: str,
    candidate_dependency_revisions: Mapping[str, str],
    expected_evidence_revisions: Mapping[str, str],
    proposal_path: Path,
    expected_proposal_revision: str,
    applied_proposal_payload: bytes,
    proposal_digest: str,
    fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Atomically install one reviewed initial HTML/registry pair and open HTML review."""

    root = root.resolve()
    source_html = candidate_html.resolve()
    source_registry = candidate_registry.resolve()
    marker = proposal_path.resolve()
    for path, label in (
        (source_html, "HTML Candidate"),
        (source_registry, "HTML Candidate registry"),
        (marker, "HTML Candidate metadata"),
    ):
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise WorkflowError(f"{label} must remain inside the repository") from exc
        if path.is_symlink() or not path.is_file():
            raise WorkflowError(f"{label} is missing or unsafe")
        parts = relative.parts
        if len(parts) < 4 or parts[0] != ".bento-ai" or parts[1] != "runs" or not re.fullmatch(r"[0-9a-f]{32}", parts[2]):
            raise WorkflowError(f"{label} is not generation-scoped")
    job_id = marker.relative_to(root).parts[2]
    expected_directory = root / ".bento-ai" / "runs" / job_id
    if source_html != expected_directory / "candidate" / "deck.preview.html":
        raise WorkflowError("HTML Candidate location is invalid")
    if source_registry != expected_directory / "candidate" / "deck.registry.json":
        raise WorkflowError("HTML Candidate registry location is invalid")
    if marker != expected_directory / "html-generation.json":
        raise WorkflowError("HTML Candidate metadata location is invalid")
    try:
        applied_value = json.loads(applied_proposal_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("Applied HTML Candidate metadata is invalid") from exc
    if (
        not isinstance(applied_value, dict)
        or applied_value.get("status") != "applied"
        or applied_value.get("candidateDigest") != proposal_digest
        or applied_value.get("generationId") != job_id
    ):
        raise WorkflowError("Applied HTML Candidate metadata does not match the reviewed candidate")

    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    deck_path = (root / STATE_RELATIVE).resolve()
    work_log = (root / WORK_LOG_RELATIVE).resolve()
    input_paths = {
        _repo_path(root, relative, field="HTML generation input"): revision
        for relative, revision in expected_input_revisions.items()
    }
    dependency_paths = {
        _repo_path(root, relative, field="HTML Candidate dependency"): revision
        for relative, revision in candidate_dependency_revisions.items()
    }
    evidence_paths = {
        _repo_path(root, relative, field="HTML Candidate browser evidence"): revision
        for relative, revision in expected_evidence_revisions.items()
    }
    lease_paths = {
        *planning_action_artifact_paths(root, state),
        canonical_html,
        canonical_registry,
        source_html,
        source_registry,
        marker,
        *input_paths,
        *dependency_paths,
        *evidence_paths,
    }
    lease = WriterLease(root, lease_paths)
    try:
        lease.acquire()
        with planning_action_guard(root, state, inherited_writer_lease=lease):
            _require_stage(state, "html_authoring")
            if (
                state.get("schemaVersion") != 2
                or state.get("authoring", {}).get("mode") not in {"single", "imported"}
                or _authoring_strategy(state) != WHOLE_DECK_STRATEGY
            ):
                raise WorkflowError("Initial HTML Candidate requires schema v2 whole-deck authoring")
            if any(state.get("approvals", {}).get(key) != "approved" for key in (
                "explanationPolicy", "storyOutline", "slidePlan",
            )):
                raise WorkflowError("Initial HTML Candidate requires approved planning")
            if _has_active_html_change(state):
                raise WorkflowError("Resolve the active HTML change before applying initial HTML")
            if canonical_html.exists() or canonical_registry.exists():
                raise PlanningRevisionConflict(
                    "Canonical HTMLまたはregistryがすでに存在します。既存のHTML Review経路を利用してください。"
                )
            if file_revision(deck_path) != expected_state_revision:
                raise PlanningRevisionConflict("deck.yamlが更新されています。HTML案を作り直してください。")
            if not hmac.compare_digest(
                planning_review_signature(root, state), expected_base_planning_signature,
            ):
                raise PlanningRevisionConflict("承認済みplanningが更新されています。HTML案を作り直してください。")
            if file_revision(source_html) != expected_candidate_html_revision:
                raise PlanningRevisionConflict("HTML Candidateが更新されています。最新の案を読み直してください。")
            if file_revision(source_registry) != expected_candidate_registry_revision:
                raise PlanningRevisionConflict("HTML Candidate registryが更新されています。最新の案を読み直してください。")
            if file_revision(marker) != expected_proposal_revision:
                raise PlanningRevisionConflict("HTML Candidate metadataが更新されています。最新の案を読み直してください。")
            for path, revision in {**input_paths, **dependency_paths, **evidence_paths}.items():
                if file_revision(path) != revision:
                    raise PlanningRevisionConflict("HTML Candidateの入力またはdependencyが更新されています。HTML案を作り直してください。")

            html_payload = source_html.read_bytes()
            registry_payload = source_registry.read_bytes()
            candidate_registry_value = _read_json(source_registry, label="initial HTML Candidate registry")

            def installed_evidence() -> HtmlDeckStructureEvidence:
                canonical_html.parent.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    prefix=".initial-html-", suffix=".preview.html",
                    dir=canonical_html.parent, delete=False,
                )
                temporary = Path(handle.name)
                try:
                    with handle:
                        handle.write(html_payload)
                    return _whole_deck_evidence(
                        root, state, html_path=temporary, registry=candidate_registry_value,
                    )
                finally:
                    temporary.unlink(missing_ok=True)

            evidence = installed_evidence()
            if evidence.review_digest != expected_candidate_review_digest:
                raise PlanningRevisionConflict("HTML Candidateのreview evidenceが更新されています。")
            if evidence.dependency_hashes != dict(candidate_dependency_revisions):
                raise PlanningRevisionConflict("HTML Candidateのdependency manifestが更新されています。")
            expected_slide_ids = [
                slide_id
                for section in state["sections"].values()
                for slide_id in section.get("slideIds", [])
            ]
            expected_sections = {
                slide_id: section_id
                for section_id, section in state["sections"].items()
                for slide_id in section.get("slideIds", [])
            }
            if list(evidence.ordered_slide_ids) != expected_slide_ids:
                raise WorkflowError("HTML Candidate slide order differs from approved planning")
            if evidence.slide_section_ids != expected_sections:
                raise WorkflowError("HTML Candidate section membership differs from approved planning")

            next_state = copy.deepcopy(state)
            next_state["authoring"]["htmlChange"] = None
            _set_whole_deck_html_review(
                root,
                next_state,
                evidence,
                source="completed-authoring",
                proposal_digest=proposal_digest,
                html_revision=expected_candidate_html_revision,
                registry_revision_value=expected_candidate_registry_revision,
            )
            validate_state(root, next_state)
            payloads = {
                canonical_html: html_payload,
                canonical_registry: registry_payload,
                deck_path: _state_payload(next_state),
                work_log: _work_log_payload(
                    root,
                    "Applied the reviewed AI initial HTML Candidate and opened whole-deck HTML review",
                ),
                marker: bytes(applied_proposal_payload),
            }
            store = ArtifactTransactionStore(
                root,
                (*payloads, source_html, source_registry, *input_paths, *dependency_paths, *evidence_paths),
                inherited_writer_lease=lease,
                fault_injector=fault_injector,
            )

            def validate_base() -> None:
                fresh = load_state(root)
                if file_revision(deck_path) != expected_state_revision:
                    raise PlanningRevisionConflict("deck.yaml changed while applying initial HTML")
                if not hmac.compare_digest(
                    planning_review_signature(root, fresh), expected_base_planning_signature,
                ):
                    raise PlanningRevisionConflict("approved planning changed while applying initial HTML")
                if canonical_html.exists() or canonical_registry.exists():
                    raise PlanningRevisionConflict("canonical HTML appeared while applying initial HTML")
                if file_revision(source_html) != expected_candidate_html_revision:
                    raise PlanningRevisionConflict("HTML Candidate changed while applying")
                if file_revision(source_registry) != expected_candidate_registry_revision:
                    raise PlanningRevisionConflict("HTML Candidate registry changed while applying")
                if file_revision(marker) != expected_proposal_revision:
                    raise PlanningRevisionConflict("HTML Candidate metadata changed while applying")
                for path, revision in {**input_paths, **dependency_paths, **evidence_paths}.items():
                    if file_revision(path) != revision:
                        raise PlanningRevisionConflict("HTML Candidate input changed while applying")
                current_evidence = installed_evidence()
                if current_evidence.review_digest != expected_candidate_review_digest:
                    raise PlanningRevisionConflict("HTML Candidate evidence changed while applying")

            def validate_committed() -> None:
                committed = load_state(root)
                if committed["workflow"]["stage"] != "html_review":
                    raise WorkflowError("Initial HTML apply did not open HTML review")
                if file_revision(canonical_html) != expected_candidate_html_revision:
                    raise WorkflowError("Installed canonical HTML differs from the reviewed candidate")
                if file_revision(canonical_registry) != expected_candidate_registry_revision:
                    raise WorkflowError("Installed canonical registry differs from the reviewed candidate")
                _require_current_html_review(root, committed)
                if marker.read_bytes() != applied_proposal_payload:
                    raise WorkflowError("Applied HTML Candidate status differs after commit")

            result = store.commit(
                payloads,
                operation="apply-ai-initial-html-candidate",
                validate_base=validate_base,
                validate_committed=validate_committed,
            )
            state.clear()
            state.update(next_state)
            return result
    except ArtifactLeaseConflict as exc:
        raise PlanningRevisionConflict(
            "HTML案の反映対象を別の処理が更新中です。完了後に最新の案を読み直してください。"
        ) from exc
    finally:
        lease.release()


def command_approve_html_deck(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "html_review")
    if _authoring_strategy(state) != WHOLE_DECK_STRATEGY:
        raise WorkflowError("approve-html-deck requires whole-deck authoring")
    if _has_active_html_change(state):
        raise WorkflowError("Resolve the active HTML change proposal before approving the whole deck")
    _require_current_post_apply_review(root, state)
    incomplete = [
        section_id for section_id, entry in state["sections"].items()
        if entry["status"] != "html_review" or entry.get("canonical") != "html"
    ]
    if incomplete:
        raise WorkflowError(
            "Every section must be in the current whole-deck HTML review: "
            + ", ".join(incomplete)
        )
    evidence = _require_current_html_review(root, state)
    next_state = copy.deepcopy(state)
    for section_id, section_digest in evidence.section_digests.items():
        slide_ids = [
            slide_id for slide_id in evidence.ordered_slide_ids
            if evidence.slide_section_ids[slide_id] == section_id
        ]
        next_state["sections"][section_id].update({
            "status": "approved", "canonical": "html",
            "slideIds": slide_ids, "approvalDigest": section_digest,
        })
    next_state["handoff"]["readyForCodex"] = True
    _transition(next_state, "ready_for_conversion", "ready", current=None)
    atomic_write_state(root, next_state)
    append_work_log(root, "Approved the complete HTML deck at one human checkpoint")


def _clean_proposal_text(value: str, *, field: str) -> str:
    cleaned = " ".join(str(value).split())
    if not cleaned:
        raise WorkflowError(f"{field} must not be blank")
    return cleaned


def _proposal_report_payload(proposal: dict[str, Any]) -> bytes:
    return (json.dumps(proposal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _proposal_impact_payload(
    impact: HtmlChangeImpact,
    evidence: HtmlDeckStructureEvidence,
) -> dict[str, Any]:
    payload = impact.as_dict()
    if payload["globalStyleChanged"]:
        # A shared-style edit changes every section digest by contract.
        payload["changedSectionIds"] = list(evidence.section_digests)
    return payload


def _proposal_paths(root: Path, state: dict[str, Any], proposal_id: str) -> tuple[Path, Path, Path]:
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    candidate_html = html_path.with_name(f".bento-html-change-{proposal_id}.candidate.html")
    candidate_registry = registry_path.with_name(f".bento-html-change-{proposal_id}.candidate.registry.json")
    proposal_path = root / "output" / "html-change-proposals" / f"{proposal_id}.json"
    return candidate_html, candidate_registry, proposal_path


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def command_propose_html_change(
    root: Path,
    state: dict[str, Any],
    *,
    candidate_html: Path,
    candidate_registry: Path | None,
    request: str,
    summary: str,
    impact_summary: str,
    requested_slide_ids: Iterable[str],
    related_slide_ids: Iterable[str],
    expected_base_html_revision: str | None = None,
    expected_base_registry_revision: str | None = None,
    expected_base_review_digest: str | None = None,
    expected_state_revision: str | None = None,
) -> dict[str, Any]:
    """Snapshot a candidate and calculate impact without mutating canonical HTML."""

    _require_stage(state, "html_review")
    if _authoring_strategy(state) != WHOLE_DECK_STRATEGY:
        raise WorkflowError("HTML change proposals require whole-deck authoring")
    if _has_unfinished_html_change(state):
        raise WorkflowError("Resolve the active HTML change proposal before creating another")
    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    state_path = root / STATE_RELATIVE
    expected_base_revisions = (
        ("canonical HTML", canonical_html, expected_base_html_revision),
        ("canonical registry", canonical_registry, expected_base_registry_revision),
        ("deck state", state_path, expected_state_revision),
    )
    for label, path, expected in expected_base_revisions:
        actual = file_revision(path)
        if expected is not None and actual != expected:
            raise WorkflowError(f"AI proposal base {label} changed before registration")
    base_evidence = _require_current_html_review(root, state)
    if (
        expected_base_review_digest is not None
        and base_evidence.review_digest != expected_base_review_digest
    ):
        raise WorkflowError("AI proposal HTML review changed before registration")
    source_candidate = _repo_path(root, str(candidate_html), field="html-change.candidateHtml")
    if not source_candidate.is_file():
        raise WorkflowError(f"HTML change candidate does not exist: {source_candidate}")
    if source_candidate == canonical_html:
        raise WorkflowError("Create an HTML change candidate instead of editing the canonical HTML directly")
    source_registry = (
        _repo_path(root, str(candidate_registry), field="html-change.candidateRegistry")
        if candidate_registry is not None else canonical_registry
    )
    if not source_registry.is_file():
        raise WorkflowError(f"HTML change candidate registry does not exist: {source_registry}")

    base_registry = _read_json(canonical_registry, label="canonical HTML registry")
    candidate_registry_value = _read_json(source_registry, label="candidate HTML registry")
    candidate_html_payload = source_candidate.read_bytes()
    candidate_registry_payload = source_registry.read_bytes()
    proposal_id = uuid.uuid4().hex[:12]
    snapshot_html, snapshot_registry, proposal_path = _proposal_paths(root, state, proposal_id)
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".html-change-{proposal_id}-", suffix=".candidate.html",
        dir=canonical_html.parent, delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(candidate_html_payload)
        impact = analyze_html_change(
            base_html=canonical_html,
            base_registry=base_registry,
            candidate_html=temporary_path,
            candidate_registry=candidate_registry_value,
            repository=root,
            requested_slide_ids=requested_slide_ids,
            related_slide_ids=related_slide_ids,
        )
        candidate_evidence = _whole_deck_evidence(
            root, state, html_path=temporary_path, registry=candidate_registry_value,
        )
    finally:
        temporary_path.unlink(missing_ok=True)

    proposal = {
        "format": HTML_CHANGE_FORMAT,
        "proposalId": proposal_id,
        "status": "proposed",
        "baseHtmlRevision": file_revision(canonical_html),
        "baseRegistryRevision": file_revision(canonical_registry),
        "baseReviewDigest": base_evidence.review_digest,
        "baseDependencyRevisions": dict(base_evidence.dependency_hashes),
        "candidateHtml": _relative(root, snapshot_html),
        "candidateRegistry": _relative(root, snapshot_registry),
        "candidateHtmlRevision": bytes_revision(candidate_html_payload),
        "candidateRegistryRevision": bytes_revision(candidate_registry_payload),
        "candidateReviewDigest": candidate_evidence.review_digest,
        "candidateDependencyRevisions": dict(candidate_evidence.dependency_hashes),
        "proposalPath": _relative(root, proposal_path),
        "request": _clean_proposal_text(request, field="request"),
        "summary": _clean_proposal_text(summary, field="summary"),
        "impactSummary": _clean_proposal_text(impact_summary, field="impact-summary"),
        **_proposal_impact_payload(impact, candidate_evidence),
        "proposalDigest": None,
        "approvedProposalDigest": None,
        "postApplyReview": None,
        "proposedAt": utc_now(),
        "approvedAt": None,
        "appliedAt": None,
        "cancelledAt": None,
    }
    proposal["proposalDigest"] = html_change_proposal_digest(proposal)
    next_state = copy.deepcopy(state)
    next_state["authoring"]["htmlChange"] = proposal
    validate_state(root, next_state)
    bound_state_revision = expected_state_revision or file_revision(state_path)
    payloads = {
        snapshot_html: candidate_html_payload,
        snapshot_registry: candidate_registry_payload,
        proposal_path: _proposal_report_payload(proposal),
        state_path: yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    dependency_paths = _review_dependency_paths(
        root,
        {**proposal["baseDependencyRevisions"], **proposal["candidateDependencyRevisions"]},
    )
    input_revisions = {
        canonical_html: proposal["baseHtmlRevision"],
        canonical_registry: proposal["baseRegistryRevision"],
        source_candidate: bytes_revision(candidate_html_payload),
        source_registry: bytes_revision(candidate_registry_payload),
    }

    def validate_base() -> None:
        for label, path, expected in expected_base_revisions:
            if expected is not None and file_revision(path) != expected:
                raise WorkflowError(f"AI proposal base {label} changed before registration")
        for path, expected in input_revisions.items():
            if file_revision(path) != expected:
                raise WorkflowError(f"HTML change input changed while creating the proposal: {path.name}")
        current_state = load_state(root)
        if file_revision(state_path) != bound_state_revision or current_state != state:
            raise WorkflowError("Deck state changed while creating the proposal")
        current_evidence = _require_current_html_review(root, current_state)
        if current_evidence.review_digest != proposal["baseReviewDigest"]:
            raise WorkflowError("Whole-deck HTML review changed while creating the proposal")
        if (
            expected_base_review_digest is not None
            and current_evidence.review_digest != expected_base_review_digest
        ):
            raise WorkflowError("AI proposal HTML review changed before registration")
        for relative, expected in {
            **proposal["baseDependencyRevisions"],
            **proposal["candidateDependencyRevisions"],
        }.items():
            if file_revision(_repo_path(root, relative, field="proposal dependency")) != expected:
                raise WorkflowError(f"HTML change dependency changed while creating the proposal: {relative}")

    ArtifactTransactionStore(
        root,
        (*payloads, *input_revisions, *dependency_paths),
    ).commit(
        payloads,
        operation="propose-whole-deck-html-change",
        validate_base=validate_base,
    )
    append_work_log(
        root,
        f"Proposed HTML change {proposal_id} ({proposal['scope']}); canonical HTML remains unchanged",
    )
    return proposal


def _verified_html_change(
    root: Path, state: dict[str, Any], *, required_status: str,
) -> tuple[dict[str, Any], Path, Path, Path]:
    proposal = _html_change(state)
    if not proposal or proposal.get("status") != required_status:
        raise WorkflowError(f"HTML change proposal must be {required_status!r}")
    if proposal.get("format") != HTML_CHANGE_FORMAT:
        raise WorkflowError(
            "This legacy HTML change proposal predates dependency-bound review; "
            "cancel it and create a fresh proposal"
        )
    current_digest = html_change_proposal_digest(proposal)
    if proposal.get("proposalDigest") != current_digest:
        raise WorkflowError("HTML change proposal explanation or impact changed after it was created")
    if required_status == "approved" and proposal.get("approvedProposalDigest") != current_digest:
        raise WorkflowError("HTML change approval is not bound to the current proposal digest")
    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    snapshot_html = _repo_path(root, proposal["candidateHtml"], field="authoring.htmlChange.candidateHtml")
    snapshot_registry = _repo_path(
        root, proposal["candidateRegistry"], field="authoring.htmlChange.candidateRegistry",
    )
    proposal_path = _repo_path(root, proposal["proposalPath"], field="authoring.htmlChange.proposalPath")
    checks = (
        (canonical_html, proposal["baseHtmlRevision"], "canonical HTML"),
        (canonical_registry, proposal["baseRegistryRevision"], "canonical registry"),
        (snapshot_html, proposal["candidateHtmlRevision"], "candidate HTML"),
        (snapshot_registry, proposal["candidateRegistryRevision"], "candidate registry"),
    )
    for path, expected, label in checks:
        if file_revision(path) != expected:
            raise WorkflowError(f"The {label} changed after the proposal; create a fresh proposal")
    base_evidence = _require_current_html_review(root, state)
    if base_evidence.review_digest != proposal["baseReviewDigest"]:
        raise WorkflowError("The canonical review evidence changed after the proposal")
    if base_evidence.dependency_hashes != proposal["baseDependencyRevisions"]:
        raise WorkflowError("The canonical dependency manifest changed after the proposal")
    for manifest_field in ("baseDependencyRevisions", "candidateDependencyRevisions"):
        for relative, expected in proposal[manifest_field].items():
            path = _repo_path(root, relative, field=f"authoring.htmlChange.{manifest_field}")
            if file_revision(path) != expected:
                raise WorkflowError(
                    f"The HTML change dependency changed after the proposal: {relative}"
                )
    base_registry = _read_json(canonical_registry, label="canonical HTML registry")
    candidate_registry = _read_json(snapshot_registry, label="candidate HTML registry")
    evidence = _whole_deck_evidence(root, state, html_path=snapshot_html, registry=candidate_registry)
    if evidence.review_digest != proposal["candidateReviewDigest"]:
        raise WorkflowError("The candidate review evidence changed after the proposal")
    if evidence.dependency_hashes != proposal["candidateDependencyRevisions"]:
        raise WorkflowError("The candidate dependency manifest changed after the proposal")
    recomputed = _proposal_impact_payload(
        analyze_html_change(
            base_html=canonical_html,
            base_registry=base_registry,
            candidate_html=snapshot_html,
            candidate_registry=candidate_registry,
            repository=root,
            requested_slide_ids=proposal["requestedSlideIds"],
            related_slide_ids=proposal["relatedSlideIds"],
        ),
        evidence,
    )
    changed_fields = [
        field for field, value in recomputed.items()
        if proposal.get(field) != value
    ]
    if changed_fields:
        raise WorkflowError(
            "HTML change impact no longer matches the reviewed proposal: "
            + ", ".join(changed_fields)
        )
    return proposal, snapshot_html, snapshot_registry, proposal_path


def _commit_proposal_state(
    root: Path, state: dict[str, Any], proposal: dict[str, Any], proposal_path: Path, *, operation: str,
) -> None:
    next_state = copy.deepcopy(state)
    next_state["authoring"]["htmlChange"] = proposal
    validate_state(root, next_state)
    state_path = root / STATE_RELATIVE
    payloads = {
        proposal_path: _proposal_report_payload(proposal),
        state_path: yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    ArtifactTransactionStore(root, tuple(payloads)).commit(payloads, operation=operation)


def command_approve_html_change(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "html_review")
    proposal, _, _, proposal_path = _verified_html_change(root, state, required_status="proposed")
    approved = copy.deepcopy(proposal)
    approved["status"] = "approved"
    approved["approvedProposalDigest"] = approved["proposalDigest"]
    approved["approvedAt"] = utc_now()
    _commit_proposal_state(
        root, state, approved, proposal_path, operation="approve-whole-deck-html-change",
    )
    append_work_log(root, f"Approved HTML change proposal {approved['proposalId']}")


def command_apply_html_change(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "html_review")
    proposal, snapshot_html, snapshot_registry, proposal_path = _verified_html_change(
        root, state, required_status="approved",
    )
    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    candidate_html_payload = snapshot_html.read_bytes()
    candidate_registry_payload = snapshot_registry.read_bytes()
    candidate_registry = _read_json(snapshot_registry, label="candidate HTML registry")
    evidence = _whole_deck_evidence(
        root, state, html_path=snapshot_html, registry=candidate_registry,
    )
    applied = copy.deepcopy(proposal)
    applied["status"] = "applied"
    applied["appliedAt"] = utc_now()
    review_root = root / "output" / "html-change-reviews" / applied["proposalId"]
    applied["postApplyReview"] = {
        "format": POST_APPLY_REVIEW_FORMAT,
        "status": "pending",
        "proposalDigest": applied["proposalDigest"],
        "htmlRevision": applied["candidateHtmlRevision"],
        "registryRevision": applied["candidateRegistryRevision"],
        # Removed slides remain part of the human-facing impact report but no
        # longer have a DOM node that can be rendered after apply.
        "affectedSlideIds": [
            slide_id for slide_id in applied["affectedSlideIds"]
            if slide_id not in set(applied["removedSlideIds"])
        ],
        "reportPath": _relative(root, review_root / "browser-report.json"),
        "reportRevision": None,
        "environmentPath": _relative(root, review_root / "browser-environment.json"),
        "environmentRevision": None,
        "browserEnvironmentDigest": None,
        "screenshots": {},
        "checkedAt": None,
    }
    next_state = copy.deepcopy(state)
    next_state["authoring"]["htmlChange"] = applied
    _set_whole_deck_html_review(
        root,
        next_state,
        evidence,
        source="applied-change",
        proposal_digest=applied["proposalDigest"],
        html_revision=applied["candidateHtmlRevision"],
        registry_revision_value=applied["candidateRegistryRevision"],
    )
    validate_state(root, next_state)
    state_path = root / STATE_RELATIVE
    base_state_revision = file_revision(state_path)
    payloads = {
        canonical_html: candidate_html_payload,
        canonical_registry: candidate_registry_payload,
        proposal_path: _proposal_report_payload(applied),
        state_path: yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    dependency_paths = _review_dependency_paths(
        root,
        {**applied["baseDependencyRevisions"], **applied["candidateDependencyRevisions"]},
    )
    transaction = ArtifactTransactionStore(
        root, (*payloads, snapshot_html, snapshot_registry, *dependency_paths),
    )

    def validate_base() -> None:
        if file_revision(state_path) != base_state_revision:
            raise WorkflowError("deck.yaml changed after the HTML change was prepared")
        current_state = load_state(root)
        current, current_html, current_registry, _ = _verified_html_change(
            root, current_state, required_status="approved",
        )
        if current["proposalDigest"] != applied["proposalDigest"]:
            raise WorkflowError("A different HTML change proposal was approved before apply")
        if bytes_revision(candidate_html_payload) != file_revision(current_html):
            raise WorkflowError("Prepared HTML payload differs from the approved candidate")
        if bytes_revision(candidate_registry_payload) != file_revision(current_registry):
            raise WorkflowError("Prepared registry payload differs from the approved candidate")

    def validate_committed() -> None:
        committed = load_state(root)
        validate_state(root, committed)
        current_proposal = _html_change(committed)
        if not current_proposal or current_proposal.get("status") != "applied":
            raise WorkflowError("Committed HTML change state is not applied")
        if file_revision(canonical_html) != current_proposal["candidateHtmlRevision"]:
            raise WorkflowError("Committed canonical HTML differs from the approved candidate")
        if file_revision(canonical_registry) != current_proposal["candidateRegistryRevision"]:
            raise WorkflowError("Committed canonical registry differs from the approved candidate")
        _require_current_html_review(root, committed)

    transaction.commit(
        payloads,
        operation="apply-approved-whole-deck-html-change",
        validate_base=validate_base,
        validate_committed=validate_committed,
    )
    append_work_log(
        root,
        f"Applied approved HTML change {applied['proposalId']} and returned the whole deck to review",
    )


def command_cancel_html_change(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "html_review")
    proposal = _html_change(state)
    if not proposal or proposal.get("status") not in ACTIVE_HTML_CHANGE_STATUSES:
        raise WorkflowError("There is no active HTML change proposal to cancel")
    # Cancellation never applies candidate bytes, so it remains available even
    # when either side became stale after the proposal was created.
    proposal_path = _repo_path(
        root, proposal["proposalPath"], field="authoring.htmlChange.proposalPath",
    )
    cancelled = copy.deepcopy(proposal)
    cancelled["status"] = "cancelled"
    cancelled["cancelledAt"] = utc_now()
    _commit_proposal_state(
        root, state, cancelled, proposal_path, operation="cancel-whole-deck-html-change",
    )
    append_work_log(root, f"Cancelled HTML change proposal {cancelled['proposalId']}; canonical HTML was unchanged")


def _require_current_post_apply_review(root: Path, state: dict[str, Any]) -> None:
    proposal = _html_change(state)
    if not proposal or proposal.get("status") != "applied":
        return
    _require_current_html_review(root, state)
    review = _post_apply_review(proposal)
    if not review or review.get("status") != "checked":
        raise WorkflowError("Affected slides need current post-apply browser review before whole-deck approval")
    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    if file_revision(canonical_html) != review["htmlRevision"]:
        raise WorkflowError("Post-apply browser review is stale for the current HTML")
    if file_revision(canonical_registry) != review["registryRevision"]:
        raise WorkflowError("Post-apply browser review is stale for the current registry")
    if review["proposalDigest"] != proposal["proposalDigest"]:
        raise WorkflowError("Post-apply browser review belongs to a different proposal")
    report_path = _repo_path(
        root, review["reportPath"], field="authoring.htmlChange.postApplyReview.reportPath",
    )
    environment_path = _repo_path(
        root, review["environmentPath"], field="authoring.htmlChange.postApplyReview.environmentPath",
    )
    if file_revision(report_path) != review["reportRevision"]:
        raise WorkflowError("Post-apply browser report revision is stale")
    if file_revision(environment_path) != review["environmentRevision"]:
        raise WorkflowError("Post-apply browser environment revision is stale")
    report = _read_json(report_path, label="post-apply browser report")
    environment = _read_json(environment_path, label="post-apply browser environment")
    if report.get("status") != "pass" or report.get("proposalDigest") != proposal["proposalDigest"]:
        raise WorkflowError("Post-apply browser report does not pass for the current proposal")
    if report.get("htmlRevision") != review["htmlRevision"] or report.get("registryRevision") != review["registryRevision"]:
        raise WorkflowError("Post-apply browser report artifact revisions are stale")
    if report.get("affectedSlideIds") != review["affectedSlideIds"]:
        raise WorkflowError("Post-apply browser report covers different slides")
    if environment.get("environmentDigest") != review["browserEnvironmentDigest"]:
        raise WorkflowError("Post-apply browser environment digest is stale")
    for slide_id, screenshot in review["screenshots"].items():
        path = _repo_path(
            root, screenshot["path"],
            field=f"authoring.htmlChange.postApplyReview.screenshots.{slide_id}.path",
        )
        if file_revision(path) != screenshot["revision"]:
            raise WorkflowError(f"Post-apply screenshot revision is stale: {slide_id}")


def command_check_html_change(
    root: Path,
    state: dict[str, Any],
    *,
    browser_executable: Path | None,
) -> None:
    """Create revision-bound browser evidence for an applied HTML change."""

    _require_stage(state, "html_review")
    proposal = _html_change(state)
    review = _post_apply_review(proposal)
    if not proposal or proposal.get("status") != "applied" or not review:
        raise WorkflowError("There is no applied HTML change awaiting browser review")
    if review.get("status") != "pending":
        _require_current_post_apply_review(root, state)
        return
    _require_current_html_review(root, state)
    canonical_html = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    canonical_registry = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    if file_revision(canonical_html) != review["htmlRevision"]:
        raise WorkflowError("Applied HTML changed before post-apply browser review")
    if file_revision(canonical_registry) != review["registryRevision"]:
        raise WorkflowError("Applied registry changed before post-apply browser review")

    with tempfile.TemporaryDirectory(prefix="bento-html-change-review-") as temporary:
        evidence = collect_html_change_browser_evidence(
            html_path=canonical_html,
            registry_path=canonical_registry,
            affected_slide_ids=review["affectedSlideIds"],
            screenshots_dir=Path(temporary) / "screenshots",
            browser_executable=browser_executable,
        )
        checked_at = utc_now()
        review_root = root / "output" / "html-change-reviews" / proposal["proposalId"]
        screenshot_payloads: dict[Path, bytes] = {}
        screenshot_state: dict[str, dict[str, str]] = {}
        screenshot_report: dict[str, dict[str, str]] = {}
        for index, slide_id in enumerate(review["affectedSlideIds"], start=1):
            payload = evidence.screenshots[slide_id].read_bytes()
            destination = review_root / "screenshots" / f"{index:02d}.png"
            relative = _relative(root, destination)
            revision = bytes_revision(payload)
            screenshot_payloads[destination] = payload
            screenshot_state[slide_id] = {"path": relative, "revision": revision}
            screenshot_report[slide_id] = {"path": relative, "revision": revision}

        environment_payload = (
            json.dumps(evidence.environment, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        report_value = {
            **evidence.report,
            "proposalId": proposal["proposalId"],
            "proposalDigest": proposal["proposalDigest"],
            "htmlRevision": review["htmlRevision"],
            "registryRevision": review["registryRevision"],
            "screenshots": screenshot_report,
            "browserEnvironmentDigest": evidence.environment["environmentDigest"],
            "checkedAt": checked_at,
        }
        report_payload = (
            json.dumps(report_value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        checked = copy.deepcopy(proposal)
        checked_review = checked["postApplyReview"]
        checked_review.update({
            "status": "checked",
            "reportRevision": bytes_revision(report_payload),
            "environmentRevision": bytes_revision(environment_payload),
            "browserEnvironmentDigest": evidence.environment["environmentDigest"],
            "screenshots": screenshot_state,
            "checkedAt": checked_at,
        })
        next_state = copy.deepcopy(state)
        next_state["authoring"]["htmlChange"] = checked
        validate_state(root, next_state)
        state_path = root / STATE_RELATIVE
        proposal_path = _repo_path(root, proposal["proposalPath"], field="authoring.htmlChange.proposalPath")
        report_path = _repo_path(root, review["reportPath"], field="postApplyReview.reportPath")
        environment_path = _repo_path(root, review["environmentPath"], field="postApplyReview.environmentPath")
        base_state_revision = file_revision(state_path)
        payloads = {
            **screenshot_payloads,
            report_path: report_payload,
            environment_path: environment_payload,
            proposal_path: _proposal_report_payload(checked),
            state_path: yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8"),
        }
        dependency_paths = _review_dependency_paths(
            root, _html_review_baseline(state)["dependencyRevisions"],
        )
        transaction = ArtifactTransactionStore(
            root, (*payloads, canonical_html, canonical_registry, *dependency_paths),
        )

        def validate_base() -> None:
            if file_revision(state_path) != base_state_revision:
                raise WorkflowError("deck.yaml changed during post-apply browser review")
            current = load_state(root)
            current_proposal = _html_change(current)
            current_review = _post_apply_review(current_proposal)
            if (
                not current_proposal
                or current_proposal.get("proposalDigest") != proposal["proposalDigest"]
                or not current_review
                or current_review.get("status") != "pending"
            ):
                raise WorkflowError("HTML change review state changed during browser inspection")
            if file_revision(canonical_html) != review["htmlRevision"]:
                raise WorkflowError("Canonical HTML changed during browser inspection")
            if file_revision(canonical_registry) != review["registryRevision"]:
                raise WorkflowError("Canonical registry changed during browser inspection")
            _require_current_html_review(root, current)

        def validate_committed() -> None:
            _require_current_post_apply_review(root, load_state(root))

        transaction.commit(
            payloads,
            operation="check-applied-whole-deck-html-change",
            validate_base=validate_base,
            validate_committed=validate_committed,
        )
    append_work_log(
        root,
        f"Browser-checked affected slides for applied HTML change {proposal['proposalId']}",
    )


def _section_digest(
    document: dict[str, Any], registry: dict[str, Any], slide_ids: Iterable[str],
) -> str:
    ordered_ids = [str(value) for value in slide_ids]
    wanted = set(ordered_ids)
    projection = [
        slide for slide in document.get("slides", [])
        if isinstance(slide, dict) and slide.get("id") in wanted
    ]
    if [slide.get("id") for slide in projection] != ordered_ids:
        raise WorkflowError("Authoring Bento does not contain the section slides in their registered order")

    normalized = normalize_registry(registry, unit_id=str(registry.get("unitId") or "deck"))
    try:
        dependencies = registry_dependency_closure(projection, normalized)
    except BentoConverterError as exc:
        raise WorkflowError(str(exc)) from exc
    registry_projection: dict[str, Any] = {}
    definition_collections = [
        collection for collection in dependencies if collection != "sources"
    ]
    # Keep the established digest shape: when a section references any typed
    # definition all typed collections are projected (including empty ones),
    # while a section with only direct source provenance projects sources only.
    if any(dependencies[collection] for collection in definition_collections):
        for collection in definition_collections:
            registry_projection[collection] = {
                identifier: copy.deepcopy(normalized.get(collection, {})[identifier])
                for identifier in sorted(dependencies[collection])
            }
    registry_projection["sources"] = {
        identifier: copy.deepcopy(normalized.get("sources", {})[identifier])
        for identifier in sorted(dependencies["sources"])
    }

    section_document = {"title": "", "slides": projection}
    section_text = visible_document_text(section_document)
    element_ids = {
        str(element["id"])
        for slide in projection
        for element in slide.get("elements", [])
        if isinstance(element, dict) and isinstance(element.get("id"), str)
    }
    protected = normalized.get("protected", {})
    registry_projection["protected"] = {
        "slideIds": [value for value in protected.get("slideIds", []) if value in wanted],
        "elementIds": [value for value in protected.get("elementIds", []) if value in element_ids],
        "requiredText": [value for value in protected.get("requiredText", []) if value in section_text],
    }
    return canonical_projection_hash({"slides": projection, "registry": registry_projection})


def _protected_output_hashes(root: Path, state: dict[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for field in ("generatedHtml", "generatedJson", "generatedRegistry", "finalHtml", "finalJson", "finalRegistry"):
        value = state["outputs"].get(field)
        path = _repo_path(root, value, field=f"outputs.{field}") if isinstance(value, str) else None
        result[field] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    return result


def _rolling_section(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    section_id = state["workflow"].get("currentSection")
    if not section_id or section_id not in state["sections"]:
        raise WorkflowError("No current section is selected")
    return section_id, state["sections"][section_id]


def command_approve_current(root: Path, state: dict[str, Any]) -> None:
    """Record only the approval that is explicitly awaiting the user now."""

    stage = state["workflow"]["stage"]
    if stage == "awaiting_plan_approval":
        command_approve_plan(root, state)
        return
    if stage == "html_review" and state["authoring"]["mode"] in {"single", "imported"}:
        if _authoring_strategy(state) == WHOLE_DECK_STRATEGY:
            proposal = _html_change(state)
            if proposal and proposal.get("status") == "proposed":
                command_approve_html_change(root, state)
            else:
                command_approve_html_deck(root, state)
            return
        section_id, entry = _rolling_section(state)
        if entry["status"] not in {"html_review", "review"}:
            raise WorkflowError(f"Current section is not awaiting HTML approval: {section_id}")
        evidence = load_single_section_evidence(root, state)
        if section_id not in evidence:
            raise WorkflowError(f"Current section is absent from HTML: {section_id}")
        current = evidence[section_id]
        if entry["slideIds"] and entry["slideIds"] != list(current.slide_ids):
            raise WorkflowError(f"Section slide membership changed during review: {section_id}")
        next_state = copy.deepcopy(state)
        current_entry = next_state["sections"][section_id]
        current_entry.update({
            "status": "bento_integration", "canonical": "html",
            "slideIds": list(current.slide_ids), "approvalDigest": current.digest,
        })
        _transition(next_state, "html_review", "ready", current=section_id)
        atomic_write_state(root, next_state)
        append_work_log(root, f"Approved section {section_id} HTML for Bento promotion")
        return
    if stage == "content_review":
        command_approve_content(root, state)
        return
    if stage == "bento_finalization":
        command_approve_final(root, state)
        return
    raise WorkflowError("There is no current user approval checkpoint")


def command_promote_current_section(
    root: Path, state: dict[str, Any], *, browser_executable: Path | None, browser_check: bool,
) -> None:
    """Convert and transactionally promote one approved HTML section into authoring."""

    _require_stage(state, "html_review")
    if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
        raise WorkflowError("Rolling section promotion requires schema v2 single/imported authoring")
    section_id, entry = _rolling_section(state)
    if entry["status"] != "bento_integration" or entry.get("canonical") != "html":
        raise WorkflowError("Approve the current section HTML before promoting it")
    evidence = load_single_section_evidence(root, state)
    if section_id not in evidence or entry["approvalDigest"] != evidence[section_id].digest:
        raise WorkflowError("Current section HTML changed after approval; review it again")
    before = _protected_output_hashes(root, state)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    source_registry = _read_json(registry_path, label="single HTML registry")
    base = root / "Bento_Slides.base.bento.html"
    if not base.is_file():
        raise WorkflowError("Bento runtime base is missing")
    candidate_html_handle = tempfile.NamedTemporaryFile(
        prefix=".section-promotion-", suffix=".preview.html", dir=html_path.parent, delete=False,
    )
    candidate_registry_handle = tempfile.NamedTemporaryFile(
        prefix=".section-promotion-", suffix=".registry.json", dir=html_path.parent, delete=False,
    )
    candidate_html_handle.close()
    candidate_registry_handle.close()
    candidate_html_path = Path(candidate_html_handle.name)
    candidate_registry_path = Path(candidate_registry_handle.name)
    try:
      with tempfile.TemporaryDirectory(prefix=".section-promotion-build-", dir=output_dir) as temporary:
        work = Path(temporary)
        candidate_html, candidate_registry, slide_ids = write_section_candidate(
            html_path, source_registry, section_id=section_id,
            output_html=candidate_html_path, output_registry=candidate_registry_path,
        )
        conversion = build_from_html(
            html_path=candidate_html, registry_path=candidate_registry,
            base_path=base, output_path=work / "section.bento.html",
            browser_executable=browser_executable, browser_check=browser_check,
        )
        converted_registry = _read_json(work / "diagnostics/merged-registry.json", label="promoted registry")
        outputs = state["outputs"]
        target = _repo_path(root, outputs["authoringHtml"], field="outputs.authoringHtml")
        target_registry = _repo_path(root, outputs["authoringRegistry"], field="outputs.authoringRegistry")
        next_state = copy.deepcopy(state)
        next_entry = next_state["sections"][section_id]
        next_entry.update({
            "status": "bento_authoring", "canonical": "bento", "slideIds": slide_ids,
            "bentoSlideIds": slide_ids,
            "acceptedAt": None,
        })
        next_state["approvals"]["bentoContent"] = _pending_content_approval()
        next_state["handoff"].update({
            "readyForCodex": False, "readyForBentoAuthoring": True,
            "readyForContentReview": False, "readyForFinalEditing": False,
        })
        _transition(next_state, "bento_authoring", "in_progress", current=section_id)
        if not target.exists():
            next_entry.update({
                "bentoDocumentRevision": document_revision(conversion.document),
                "bentoRegistryRevision": registry_revision(converted_registry),
                "bentoSectionDigest": _section_digest(
                    conversion.document, converted_registry, slide_ids,
                ),
            })
            validate_state(root, next_state)
            AuthoringArtifactStorage(
                source=conversion.html_path, source_registry=work / "diagnostics/merged-registry.json",
                target=target, target_registry=target_registry, repository=root,
                state_path=root / STATE_RELATIVE, initial_workflow_state=next_state,
            )
        else:
            storage = authoring_storage(root, state)
            storage.acquire_writer_lease()
            try:
                current_html, current_document, current_registry = storage.artifact_snapshot()
                current_ids = [str(slide.get("id")) for slide in current_document.get("slides", [])]
                installed_ids = list(entry.get("bentoSlideIds") or [])
                if not installed_ids and all(slide_id in current_ids for slide_id in entry["slideIds"]):
                    installed_ids = list(entry["slideIds"])
                if installed_ids:
                    operation = "replace-section"
                    anchor = None
                    targets = installed_ids
                else:
                    later_ids: list[str] = []
                    seen_current = False
                    for planned_id, planned in state["sections"].items():
                        if planned_id == section_id:
                            seen_current = True
                            continue
                        if seen_current and planned.get("canonical") == "bento":
                            later_ids.extend(planned.get("bentoSlideIds") or planned["slideIds"])
                    anchor = next((value for value in later_ids if value in current_ids), None)
                    operation = "insert-before" if anchor else "append"
                    targets = None
                merged_document, merged_registry, report = merge_segment(
                    current_document, current_registry, conversion.document, converted_registry,
                    operation=operation, anchor_slide_id=anchor, target_slide_ids=targets,
                )
                next_entry.update({
                    "bentoSlideIds": slide_ids,
                    "bentoDocumentRevision": document_revision(merged_document),
                    "bentoRegistryRevision": registry_revision(merged_registry),
                    "bentoSectionDigest": _section_digest(
                        merged_document, merged_registry, slide_ids,
                    ),
                })
                validate_state(root, next_state)
                status = storage.status()
                storage.save_serialized(
                    embed_bento_doc(current_html, merged_document),
                    base_document_revision=status["documentRevision"],
                    base_registry_revision=status["registryRevision"], registry=merged_registry,
                    replace_slide_ids=set(targets or ()), operation="promote-section",
                    report_details={"sectionId": section_id, **report},
                    report_path=output_dir / "section-promotion-report.json",
                    workflow_state=next_state,
                )
            finally:
                storage.release_writer_lease()
    finally:
        candidate_html_path.unlink(missing_ok=True)
        candidate_registry_path.unlink(missing_ok=True)
    if _protected_output_hashes(root, next_state) != before:
        raise WorkflowError("Section promotion changed generated or final artifacts")
    append_work_log(root, f"Promoted section {section_id} to Bento authoring without rebuilding unrelated sections")


def _assert_accepted_sections_current(
    state: dict[str, Any], document: dict[str, Any], registry: dict[str, Any],
) -> None:
    for section_id, entry in state["sections"].items():
        slide_ids = entry.get("bentoSlideIds") or entry["slideIds"]
        if entry["status"] == "accepted" and entry.get("bentoSectionDigest") != _section_digest(
            document, registry, slide_ids,
        ):
            raise WorkflowError(f"Accepted section changed without being reopened: {section_id}")


def _is_rolling_section_workflow(state: dict[str, Any]) -> bool:
    if state.get("schemaVersion") != 2 or state.get("authoring", {}).get("mode") not in {"single", "imported"}:
        return False
    return any(
        entry.get("bentoSlideIds")
        or entry.get("status") in {"bento_integration", "bento_authoring", "accepted"}
        for entry in state.get("sections", {}).values()
    )


def _require_rolling_sections_accepted(
    state: dict[str, Any], document: dict[str, Any], registry: dict[str, Any],
) -> None:
    """Enforce the rolling-workflow gate on one consistent artifact snapshot."""

    if not _is_rolling_section_workflow(state):
        return
    incomplete = [
        section_id for section_id, entry in state["sections"].items()
        if entry["status"] != "accepted"
    ]
    if incomplete:
        raise WorkflowError(
            "Every section must be accepted before whole-deck content review: "
            + ", ".join(incomplete)
        )
    _assert_accepted_sections_current(state, document, registry)


def command_finish_current_section(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "bento_authoring")
    section_id, entry = _rolling_section(state)
    if entry["status"] != "bento_authoring" or entry.get("canonical") != "bento":
        raise WorkflowError("Current section is not in Bento authoring")
    storage = authoring_storage(root, state)
    _, document, registry = storage.artifact_snapshot()
    _assert_accepted_sections_current(state, document, registry)
    next_state = copy.deepcopy(state)
    accepted = next_state["sections"][section_id]
    accepted.update({
        "status": "accepted", "canonical": "bento",
        "bentoSlideIds": list(accepted.get("bentoSlideIds") or accepted["slideIds"]),
        "bentoDocumentRevision": document_revision(document),
        "bentoRegistryRevision": registry_revision(registry),
        "bentoSectionDigest": _section_digest(
            document, registry, accepted.get("bentoSlideIds") or accepted["slideIds"],
        ),
        "acceptedAt": utc_now(),
    })
    remaining = [key for key, value in next_state["sections"].items() if value["status"] != "accepted"]
    if remaining:
        next_id = remaining[0]
        next_entry = next_state["sections"][next_id]
        if next_entry["status"] == "planned":
            next_entry.update({"status": "html_authoring", "canonical": "html"})
        _transition(next_state, "html_authoring", "in_progress", current=next_id)
    else:
        next_state["handoff"]["readyForContentReview"] = True
        _transition(next_state, "content_review", "awaiting_approval")
    atomic_write_state(root, next_state)
    append_work_log(root, f"Accepted Bento section {section_id}; it remains reopenable")


def command_reopen_current_section(root: Path, state: dict[str, Any], *, section_id: str | None, via: str) -> None:
    _require_stage(state, "html_authoring", "html_review", "bento_authoring", "content_review", "bento_finalization", "complete")
    selected = section_id or state["workflow"].get("currentSection")
    if not selected:
        selected = next((key for key, value in state["sections"].items() if value["status"] == "accepted"), None)
    if not selected or selected not in state["sections"]:
        raise WorkflowError("No section is available to reopen")
    entry = state["sections"][selected]
    if entry["status"] not in {"accepted", "bento_authoring", "bento_integration"}:
        raise WorkflowError(f"Section is not reopenable from status {entry['status']!r}")
    next_state = copy.deepcopy(state)
    reopened = next_state["sections"][selected]
    if not reopened.get("bentoSlideIds"):
        # Upgrade rolling states created before installed Bento membership was tracked separately.
        reopened["bentoSlideIds"] = list(reopened["slideIds"])
    reopened["acceptedAt"] = None
    next_state["approvals"]["bentoContent"] = _pending_content_approval()
    next_state["approvals"]["finalBento"] = _pending_final_approval()
    next_state["validation"].update({"finalStatus": "pending", "checkedAt": None})
    if via == "html":
        reopened.update({
            "status": "html_authoring", "canonical": "html", "approvalDigest": None,
            "bentoDocumentRevision": None, "bentoRegistryRevision": None, "bentoSectionDigest": None,
        })
        _transition(next_state, "html_authoring", "in_progress", current=selected)
    else:
        reopened.update({"status": "bento_authoring", "canonical": "bento"})
        _transition(next_state, "bento_authoring", "in_progress", current=selected)
    atomic_write_state(root, next_state)
    append_work_log(root, f"Reopened section {selected} via {via}; final artifacts were not changed")


def command_review_whole_deck(root: Path, state: dict[str, Any]) -> None:
    if any(entry["status"] != "accepted" for entry in state["sections"].values()):
        raise WorkflowError("Every section must be accepted before whole-deck content review")
    _require_stage(state, "bento_authoring", "content_review")
    storage = authoring_storage(root, state)
    _, document, registry = storage.artifact_snapshot()
    _require_rolling_sections_accepted(state, document, registry)
    state["handoff"]["readyForContentReview"] = True
    _transition(state, "content_review", "awaiting_approval")
    atomic_write_state(root, state)
    append_work_log(root, "Opened mandatory whole-deck content review")


def _select_chapter(state: dict[str, Any], requested: str | None) -> str:
    if requested:
        if requested not in state["chapters"]:
            raise WorkflowError(f"Chapter is not registered: {requested}")
        return requested
    current = state["workflow"].get("currentChapter")
    if current and state["chapters"][current]["status"] != "complete":
        return current
    for chapter_id, entry in state["chapters"].items():
        if entry["status"] != "complete":
            return chapter_id
    raise WorkflowError("All registered chapters are complete")


def command_begin_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    chapter_id = _select_chapter(state, requested)
    entry = state["chapters"][chapter_id]
    if entry["status"] not in {"planned", "authoring"}:
        raise WorkflowError(f"Chapter cannot enter authoring from status {entry['status']!r}: {chapter_id}")
    entry["status"] = "authoring"
    _transition(state, "html_authoring", "in_progress", current=chapter_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Began authoring {chapter_id}")


def command_complete_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    chapter_id = _select_chapter(state, requested)
    entry = state["chapters"][chapter_id]
    _load_chapter(root, chapter_id, entry)
    entry["status"] = "review"
    entry["visualApproval"] = "pending"
    _transition(state, "html_review", "awaiting_approval", current=chapter_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Validated {chapter_id} and requested visual approval")


def command_approve_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_review")
    chapter_id = requested or state["workflow"].get("currentChapter")
    if not chapter_id or chapter_id not in state["chapters"]:
        raise WorkflowError("No current chapter is available for visual approval")
    entry = state["chapters"][chapter_id]
    if entry["status"] != "review":
        raise WorkflowError(f"Chapter is not awaiting visual approval: {chapter_id}")
    _load_chapter(root, chapter_id, entry)
    entry["status"] = "complete"
    entry["visualApproval"] = "approved"
    remaining = [key for key, value in state["chapters"].items() if value["status"] != "complete"]
    if remaining:
        next_chapter = remaining[0]
        state["chapters"][next_chapter]["status"] = "authoring"
        _transition(state, "html_authoring", "in_progress", current=next_chapter)
    else:
        validate_chapters(root, state, require_complete=True)
        state["handoff"]["readyForCodex"] = True
        _transition(state, "ready_for_conversion", "ready")
    atomic_write_state(root, state)
    append_work_log(root, f"Approved visual composition for {chapter_id}")


def command_prepare_conversion(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "ready_for_conversion")
    if any(state["approvals"][key] != "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")):
        raise WorkflowError("Plan approvals are incomplete")
    validate_html_authoring(root, state, require_approved=True)
    if not state["handoff"]["readyForCodex"]:
        raise WorkflowError("Work-to-Codex handoff is not ready")
    _transition(state, "converting", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Validated conversion readiness and handed the deck to Codex")


def command_mark_converted(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "converting")
    bundle = validate_output_bundle(root, state, require_final=False)
    if state.get("schemaVersion") == 2:
        storage = authoring_storage(root, state)
        storage.status()
        state["handoff"]["readyForCodex"] = False
        state["handoff"]["readyForBentoAuthoring"] = True
        state["handoff"]["readyForContentReview"] = False
        state["handoff"]["readyForFinalEditing"] = False
        _transition(state, "bento_validation", "in_progress")
        atomic_write_state(root, state)
        append_work_log(root, "Validated generated output and initialized or retained Bento authoring artifacts")
        return
    outputs = state["outputs"]
    WorkEditorStorage(
        source=_repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml"),
        target=_repo_path(root, outputs["finalHtml"], field="outputs.finalHtml"),
        registry=bundle["registry"],
        reset_final=False,
        allow_content_edit=False,
    )
    # Before the first handoff, prove that an existing final is still a layout-only
    # descendant of generated. Persist generated as the immutable content baseline.
    validate_output_bundle(root, state, require_final=True, allow_missing_baseline=True)
    initialize_final_baseline(root, state, bundle["generatedDocument"])
    validate_output_bundle(root, state, require_final=True)
    _transition(state, "bento_validation", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Validated generated output and initialized or retained protected final output")


def command_begin_authoring(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento authoring is available only after deck schema v2 migration")
    _require_stage(state, "bento_validation")
    validate_output_bundle(root, state, require_final=False)
    storage = authoring_storage(root, state)
    storage.status()
    state["handoff"]["readyForCodex"] = False
    state["handoff"]["readyForBentoAuthoring"] = True
    state["handoff"]["readyForContentReview"] = False
    state["handoff"]["readyForFinalEditing"] = False
    _transition(state, "bento_authoring", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Handed validated generated Bento artifacts to Work for authoring")


def _pending_content_approval() -> dict[str, Any]:
    return {
        "status": "pending", "documentRevision": None, "registryRevision": None,
        "approvalDigest": None, "approvedAt": None,
    }


def _pending_final_approval() -> dict[str, Any]:
    return {
        "status": "pending", "documentRevision": None, "htmlRevision": None,
        "registryRevision": None, "runtimeFingerprint": None, "approvedAt": None,
    }


def _final_approval_status(approval: Any) -> str:
    return str(approval.get("status")) if isinstance(approval, dict) else str(approval)


def _final_approval_snapshot(bundle: dict[str, Any], *, approved_at: str | None = None) -> dict[str, Any]:
    html_revision = bundle.get("finalHtmlRevision")
    registry_revision_value = bundle.get("finalRegistryRevision")
    runtime = bundle.get("finalRuntimeFingerprint")
    if not all(isinstance(value, str) for value in (html_revision, registry_revision_value, runtime)):
        raise WorkflowError("Final artifact revisions are unavailable")
    return {
        "status": "approved",
        "documentRevision": document_revision(bundle["finalDocument"]),
        "htmlRevision": html_revision,
        "registryRevision": registry_revision_value,
        "runtimeFingerprint": runtime,
        "approvedAt": approved_at or utc_now(),
    }


def _final_artifact_store(root: Path, state: dict[str, Any]) -> ArtifactTransactionStore:
    outputs = state["outputs"]
    artifacts = [
        _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml"),
        _repo_path(root, outputs["finalJson"], field="outputs.finalJson"),
        root / STATE_RELATIVE,
    ]
    if state.get("schemaVersion") == 2 and outputs.get("finalRegistry"):
        artifacts.append(_repo_path(root, outputs["finalRegistry"], field="outputs.finalRegistry"))
    return ArtifactTransactionStore(root, artifacts)


def _commit_final_state(
    root: Path, store: ArtifactTransactionStore, before: bytes, state: dict[str, Any], *, operation: str,
) -> None:
    validate_state(root, state)
    state_path = root / STATE_RELATIVE
    payload = yaml.safe_dump(state, allow_unicode=True, sort_keys=False).encode("utf-8")

    def validate_base() -> None:
        if state_path.read_bytes() != before:
            raise WorkflowError("deck.yaml changed before the final workflow transition")

    def validate_committed() -> None:
        installed = yaml.safe_load(state_path.read_text(encoding="utf-8-sig"))
        validate_state(root, installed)

    store.commit(
        {state_path: payload}, operation=operation,
        validate_base=validate_base, validate_committed=validate_committed,
    )


def _current_authoring_status(
    root: Path, state: dict[str, Any], *, storage: AuthoringArtifactStorage | None = None,
) -> dict[str, Any]:
    status = (storage or authoring_storage(root, state)).status()
    approval = state["approvals"]["bentoContent"]
    if approval["status"] == "approved" and (
        approval["documentRevision"] != status["documentRevision"]
        or approval["registryRevision"] != status["registryRevision"]
    ):
        state["approvals"]["bentoContent"] = _pending_content_approval()
    return status


def _validated_content_review_status(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    storage = authoring_storage(root, state)
    storage.acquire_writer_lease()
    try:
        status = _current_authoring_status(root, state, storage=storage)
        _, document, registry = storage.content_review_snapshot()
        _require_rolling_sections_accepted(state, document, registry)
        return status
    finally:
        storage.release_writer_lease()


def command_begin_content_review(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento content review requires deck schema v2")
    _require_stage(state, "bento_authoring")
    _validated_content_review_status(root, state)
    state["handoff"]["readyForBentoAuthoring"] = False
    state["handoff"]["readyForContentReview"] = True
    state["handoff"]["readyForFinalEditing"] = False
    _transition(state, "content_review", "awaiting_approval")
    atomic_write_state(root, state)
    append_work_log(root, "Validated authoring artifacts and requested Bento content approval")


def command_reset_authoring_from_html(
    root: Path, state: dict[str, Any], *, confirmation: str,
    browser_executable: Path | None, browser_check: bool,
) -> None:
    if confirmation != "RESET-AUTHORING-FROM-HTML":
        raise WorkflowError(
            "Full authoring reset requires --confirm RESET-AUTHORING-FROM-HTML"
        )
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Full authoring reset requires deck schema v2")
    _require_stage(state, "bento_authoring")
    from bento_converter.html_pipeline import build_from_html

    outputs = state["outputs"]
    storage = authoring_storage(root, state)
    storage.acquire_writer_lease()
    final_paths = [
        _repo_path(root, outputs[field], field=f"outputs.{field}")
        for field in ("finalHtml", "finalJson", "finalRegistry") if outputs.get(field) is not None
    ]
    baseline = state["validation"].get("finalBaseline")
    if isinstance(baseline, dict):
        final_paths.extend([
            _repo_path(root, baseline["documentPath"], field="validation.finalBaseline.documentPath"),
            _repo_path(root, baseline["registryPath"], field="validation.finalBaseline.registryPath"),
        ])
    final_before = {path: path.read_bytes() if path.is_file() else None for path in final_paths}
    try:
        storage.create_revision_backup()
        with tempfile.TemporaryDirectory(prefix=".authoring-reset-", dir=root / "output") as temporary:
            build_root = Path(temporary)
            build_arguments: dict[str, Any] = {
                "base_path": storage.target,
                "output_path": build_root / "presentation.generated.bento.html",
                "browser_executable": browser_executable,
                "browser_check": browser_check,
            }
            if state["authoring"]["mode"] == "modular":
                build_arguments.update(html_dir=root / "chapters", registry_dir=root / "chapters")
            else:
                build_arguments.update(
                    html_path=_repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml"),
                    registry_path=_repo_path(root, state["authoring"]["registry"], field="authoring.registry"),
                )
            built = build_from_html(**build_arguments)
            built_html = built.html_path.read_bytes()
            built_document = built.document
            built_json = (serialize_bento_doc(built_document) + "\n").encode("utf-8")
            built_registry_path = build_root / "diagnostics/merged-registry.json"
            built_registry = normalize_registry(
                _read_json(built_registry_path, label="reset generated registry"), unit_id="deck",
            )
            validate_authoring_document(built_document, current=built_document, registry=built_registry)
            built_registry_payload = (
                json.dumps(built_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            ).encode("utf-8")
            generated_html = _repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml")
            generated_json = _repo_path(root, outputs["generatedJson"], field="outputs.generatedJson")
            generated_registry = _repo_path(root, outputs["generatedRegistry"], field="outputs.generatedRegistry")
            authoring_html = _repo_path(root, outputs["authoringHtml"], field="outputs.authoringHtml")
            authoring_json = _repo_path(root, outputs["authoringJson"], field="outputs.authoringJson")
            authoring_registry = _repo_path(root, outputs["authoringRegistry"], field="outputs.authoringRegistry")
            generated_root = generated_html.parent
            diagnostic_sources = {
                generated_root / "conversion-report.json": build_root / "conversion-report.json",
                generated_root / "diagnostics/computed-layout.json": build_root / "diagnostics/computed-layout.json",
                generated_root / "diagnostics/resource-scan.json": build_root / "diagnostics/resource-scan.json",
                generated_root / "diagnostics/browser-environment.json": build_root / "diagnostics/browser-environment.json",
            }
            browser_source = build_root / "diagnostics/browser-check.json"
            payloads: dict[Path, bytes] = {
                generated_html: built_html, generated_json: built_json,
                generated_registry: built_registry_payload,
                authoring_html: built_html, authoring_json: built_json,
                authoring_registry: built_registry_payload,
            }
            for destination, source in diagnostic_sources.items():
                payloads[destination] = source.read_bytes()
            payloads[generated_root / "diagnostics/browser-check.json"] = (
                browser_source.read_bytes() if browser_source.is_file()
                else b'{"skipped":true,"serialize_roundtrip":false}\n'
            )
            next_state = copy.deepcopy(state)
            next_state["approvals"]["bentoContent"] = _pending_content_approval()
            next_state["handoff"]["readyForBentoAuthoring"] = True
            next_state["handoff"]["readyForContentReview"] = False
            state_path = root / STATE_RELATIVE
            state_base = state_path.read_bytes()
            payloads[state_path] = yaml.safe_dump(
                next_state, allow_unicode=True, sort_keys=False,
            ).encode("utf-8")
            transaction = ArtifactTransactionStore(
                root, payloads, inherited_writer_lease=storage.transactions.writer_lease,
            )

            def validate_base() -> None:
                if state_path.read_bytes() != state_base:
                    raise WorkflowError("deck.yaml changed before full authoring reset")

            def validate_committed() -> None:
                installed_generated = extract_bento_doc(load_html(generated_html))
                installed_authoring = extract_bento_doc(load_html(authoring_html))
                if installed_generated != built_document or installed_authoring != built_document:
                    raise WorkflowError("Reset generated/authoring documents differ from the prepared conversion")
                if _load_sidecar(generated_json) != built_document or _load_sidecar(authoring_json) != built_document:
                    raise WorkflowError("Reset Bento JSON sidecars differ from their HTML documents")
                installed_registry = _read_json(authoring_registry, label="reset authoring registry")
                if registry_revision(installed_registry) != registry_revision(built_registry):
                    raise WorkflowError("Reset authoring registry differs from generated registry")
                validate_authoring_document(installed_authoring, current=installed_authoring, registry=installed_registry)
                validate_state(root, yaml.safe_load(state_path.read_text(encoding="utf-8-sig")))

            transaction.commit(
                payloads,
                operation="reset-authoring-from-html",
                target_document_revision=document_revision(built_document),
                target_registry_revision=registry_revision(built_registry),
                validate_base=validate_base, validate_committed=validate_committed,
                report_path=generated_root / "reset-authoring-report.json",
                report_payload={
                    "operation": "reset-authoring-from-html",
                    "documentRevision": document_revision(built_document),
                    "registryRevision": registry_revision(built_registry),
                    "backup": "created", "browserCheck": "pass" if browser_check else "skipped",
                    "finalArtifactsChanged": False,
                },
            )
            state.clear()
            state.update(next_state)
        final_after = {path: path.read_bytes() if path.is_file() else None for path in final_paths}
        if final_after != final_before:
            raise WorkflowError("Full authoring reset changed final artifacts")
        append_work_log(root, "Explicitly reset generated and authoring Bento from the HTML source")
    finally:
        storage.release_writer_lease()


def command_approve_content(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento content approval requires deck schema v2")
    _require_stage(state, "content_review")
    status = _validated_content_review_status(root, state)
    document_revision_value = status["documentRevision"]
    registry_revision_value = status["registryRevision"]
    state["approvals"]["bentoContent"] = {
        "status": "approved",
        "documentRevision": document_revision_value,
        "registryRevision": registry_revision_value,
        "approvalDigest": content_approval_digest(document_revision_value, registry_revision_value),
        "approvedAt": utc_now(),
    }
    state["handoff"]["readyForContentReview"] = True
    state["workflow"]["status"] = "ready"
    atomic_write_state(root, state)
    append_work_log(root, "Approved Bento authoring content at fixed document and registry revisions")


def _next_final_restart_archive(final_html: Path) -> tuple[Path, dict[str, Path]]:
    archive_root = final_html.parent / "revisions/final-restarts"
    existing = []
    if archive_root.is_dir():
        for path in archive_root.glob("restart-*"):
            match = re.fullmatch(r"restart-(\d{6})", path.name) if path.is_dir() else None
            if match:
                existing.append(int(match.group(1)))
    directory = archive_root / f"restart-{max(existing, default=0) + 1:06d}"
    return directory, {
        "finalHtml": directory / "final.bento.html",
        "finalJson": directory / "final.bento.json",
        "finalRegistry": directory / "final.registry.json",
        "baselineDocument": directory / "baseline.bento.json",
        "baselineRegistry": directory / "baseline.registry.json",
        "workflowState": directory / "deck.yaml",
        "manifest": directory / "manifest.json",
    }


def _final_restart_archive_payloads(
    root: Path, state: dict[str, Any], *, sources: dict[str, Path], destinations: dict[str, Path],
) -> tuple[dict[Path, bytes], dict[str, Any]]:
    source_payloads = {role: path.read_bytes() for role, path in sources.items()}
    archived_payloads = {
        destinations[role]: payload for role, payload in source_payloads.items()
    }
    final_html = source_payloads["finalHtml"].decode("utf-8-sig")
    final_document = extract_bento_doc(final_html)
    final_registry = json.loads(source_payloads["finalRegistry"].decode("utf-8-sig"))
    manifest = {
        "format": "bento/final-restart-archive/v1",
        "createdAt": utc_now(),
        "reason": "approved-authoring-content-revision",
        "previous": {
            "documentRevision": document_revision(final_document),
            "registryRevision": registry_revision(final_registry),
            "runtimeFingerprint": runtime_fingerprint(final_html),
            "baseline": copy.deepcopy(state["validation"].get("finalBaseline")),
        },
        "files": {
            role: {
                "source": sources[role].relative_to(root).as_posix(),
                "archive": destinations[role].relative_to(root).as_posix(),
                "revision": bytes_revision(payload),
            }
            for role, payload in source_payloads.items()
        },
    }
    manifest_payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    archived_payloads[destinations["manifest"]] = manifest_payload
    return archived_payloads, manifest


def _validate_final_restart_archive(root: Path, manifest_path: Path) -> None:
    manifest = _read_json(manifest_path, label="final restart archive manifest")
    if manifest.get("format") != "bento/final-restart-archive/v1":
        raise WorkflowError("Final restart archive manifest has an unsupported format")
    for role, entry in manifest.get("files", {}).items():
        if not isinstance(entry, dict):
            raise WorkflowError(f"Final restart archive entry is invalid: {role}")
        path = _repo_path(root, entry.get("archive"), field=f"finalRestartArchive.files.{role}.archive")
        if file_revision(path) != entry.get("revision"):
            raise WorkflowError(f"Final restart archive revision mismatch: {role}")


def _initialize_v2_finalization(
    root: Path, state: dict[str, Any], *, archive_existing: bool = False,
    require_archive: bool = False,
) -> None:
    recover_repository_transactions(root)
    outputs = state["outputs"]
    final_html = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
    final_json = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
    final_registry = _repo_path(root, outputs["finalRegistry"], field="outputs.finalRegistry")
    baseline_document = _final_baseline_path(root, state)
    baseline_registry = _final_registry_baseline_path(root, state)
    authoring_html_path = _repo_path(root, outputs["authoringHtml"], field="outputs.authoringHtml")
    authoring_json_path = _repo_path(root, outputs["authoringJson"], field="outputs.authoringJson")
    authoring_registry_path = _repo_path(root, outputs["authoringRegistry"], field="outputs.authoringRegistry")
    if not all(path.is_file() for path in (authoring_html_path, authoring_json_path, authoring_registry_path)):
        raise WorkflowError("Authoring artifacts must exist before final initialization")
    # Constructing the storage performs its own recovery under the authoring
    # artifact lease. Do that before taking the wider restart lease; once the
    # union lease is held, its read-only snapshot methods are protected from
    # concurrent writers without trying to acquire a conflicting second lease.
    storage = authoring_storage(root, state)
    archive_directory, archive_destinations = _next_final_restart_archive(final_html)
    state_path = root / STATE_RELATIVE
    lease = WriterLease(
        root,
        (
            authoring_html_path, authoring_json_path, authoring_registry_path, state_path,
            final_html, final_json, final_registry, baseline_document, baseline_registry,
            *archive_destinations.values(),
        ),
    )
    lease.acquire()
    try:
        authoring_html, authoring_document, authoring_registry = storage.content_review_snapshot()
        _require_rolling_sections_accepted(state, authoring_document, authoring_registry)
        document_revision_value = document_revision(authoring_document)
        registry_revision_value = registry_revision(authoring_registry)
        approval = state["approvals"]["bentoContent"]
        if (
            approval["status"] != "approved"
            or approval["documentRevision"] != document_revision_value
            or approval["registryRevision"] != registry_revision_value
            or approval["approvalDigest"] != content_approval_digest(document_revision_value, registry_revision_value)
        ):
            raise WorkflowError("Current authoring document and registry revisions do not have fresh content approval")
        storage.validate_serialized(
            authoring_html,
            base_document_revision=document_revision_value,
            base_registry_revision=registry_revision_value,
            registry=authoring_registry,
        )
        intended_document_payload = (serialize_bento_doc(authoring_document) + "\n").encode("utf-8")
        intended_registry_payload = (
            json.dumps(authoring_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        payloads = {
            final_html: authoring_html.encode("utf-8"),
            final_json: intended_document_payload,
            final_registry: intended_registry_payload,
            baseline_document: intended_document_payload,
            baseline_registry: intended_registry_payload,
        }
        existing = [path.is_file() for path in payloads]
        if any(existing) and not all(existing):
            raise WorkflowError("Final initialization artifacts are incomplete; recover or remove the incomplete set explicitly")
        archive_payloads: dict[Path, bytes] = {}
        archive_manifest: dict[str, Any] | None = None
        archive_sources = {
            "finalHtml": final_html,
            "finalJson": final_json,
            "finalRegistry": final_registry,
            "baselineDocument": baseline_document,
            "baselineRegistry": baseline_registry,
            "workflowState": state_path,
        }
        if all(existing):
            mismatched = [path for path, payload in payloads.items() if path.read_bytes() != payload]
            if mismatched:
                if not archive_existing:
                    raise WorkflowError(
                        "Existing final artifacts differ from approved authoring content; "
                        "use restart-finalization-from-authoring after explicit content approval"
                    )
                validate_output_bundle(root, state, require_final=True)
                archive_payloads, archive_manifest = _final_restart_archive_payloads(
                    root, state, sources=archive_sources, destinations=archive_destinations,
                )
            elif archive_existing and require_archive:
                raise WorkflowError(
                    "Existing final artifacts already match approved authoring content; use begin-finalization"
                )
        elif archive_existing and require_archive:
            raise WorkflowError("Final restart requires one complete existing final artifact set")

        next_state = copy.deepcopy(state)
        next_state["validation"]["finalBaseline"] = {
            "documentPath": baseline_document.relative_to(root).as_posix(),
            "documentRevision": document_revision_value,
            "registryPath": baseline_registry.relative_to(root).as_posix(),
            "registryRevision": registry_revision_value,
            "protectedContentFingerprint": protected_content_fingerprint(authoring_document),
        }
        next_state["approvals"]["finalBento"] = _pending_final_approval()
        next_state["validation"]["finalStatus"] = "pending"
        next_state["validation"]["checkedAt"] = None
        _transition(next_state, "bento_finalization", "in_progress")
        state_payload = yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8")
        base_state_payload = state_path.read_bytes()
        base_final_revisions = {
            path: file_revision(path) for path in archive_sources.values()
        }
        transaction_payloads = {**archive_payloads, **payloads, state_path: state_payload}
        transaction = ArtifactTransactionStore(
            root, transaction_payloads, inherited_writer_lease=lease,
        )

        def validate_base() -> None:
            _, current_document, current_registry = storage.artifact_snapshot()
            if (
                document_revision(current_document) != document_revision_value
                or registry_revision(current_registry) != registry_revision_value
            ):
                raise WorkflowError("Authoring revisions changed before final initialization")
            if state_path.read_bytes() != base_state_payload:
                raise WorkflowError("deck.yaml changed before final initialization")
            if archive_payloads and any(
                file_revision(path) != revision for path, revision in base_final_revisions.items()
            ):
                raise WorkflowError("Existing final artifacts changed before restart archival")

        def validate_committed() -> None:
            installed_state = yaml.safe_load(state_path.read_text(encoding="utf-8-sig"))
            validate_state(root, installed_state)
            validate_output_bundle(root, installed_state, require_final=True)
            if archive_payloads:
                _validate_final_restart_archive(root, archive_destinations["manifest"])

        operation = (
            "authoring-to-final-restart" if archive_payloads else "authoring-to-final-initialize"
        )
        transaction.commit(
            transaction_payloads,
            operation=operation,
            base_document_revision=document_revision_value,
            base_registry_revision=registry_revision_value,
            target_document_revision=document_revision_value,
            target_registry_revision=registry_revision_value,
            validate_base=validate_base,
            validate_committed=validate_committed,
            report_path=final_html.parent / (
                "finalization-restart-report.json"
                if archive_payloads else "finalization-initialization-report.json"
            ),
            report_payload={
                "operation": operation,
                "documentRevision": document_revision_value,
                "registryRevision": registry_revision_value,
                "approvalDigest": approval["approvalDigest"],
                "archive": (
                    archive_directory.relative_to(root).as_posix() if archive_payloads else None
                ),
                "previous": archive_manifest["previous"] if archive_manifest else None,
                "validation": "pass",
            },
        )
        state.clear()
        state.update(next_state)
    finally:
        lease.release()


def command_begin_finalization(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") == 2:
        _require_stage(state, "content_review")
        _initialize_v2_finalization(root, state)
        append_work_log(root, "Initialized frozen final artifacts from approved Bento authoring content")
        return
    _require_stage(state, "bento_validation")
    validate_output_bundle(root, state, require_final=True)
    state["handoff"]["readyForCodex"] = False
    state["handoff"]["readyForFinalEditing"] = True
    _transition(state, "bento_finalization", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Handed the validated final Bento artifact to Work for layout finalization")


def command_restart_finalization_from_authoring(
    root: Path, state: dict[str, Any], *, confirmation: str,
) -> None:
    if confirmation != "ARCHIVE-AND-RESTART-FINALIZATION":
        raise WorkflowError(
            "Final restart requires --confirm ARCHIVE-AND-RESTART-FINALIZATION"
        )
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Final restart from authoring requires deck schema v2")
    _require_stage(state, "content_review")
    _initialize_v2_finalization(
        root, state, archive_existing=True, require_archive=True,
    )
    append_work_log(
        root,
        "Archived the previous final artifact set and restarted finalization from approved authoring content",
    )


def command_approve_final(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "bento_finalization")
    if state.get("schemaVersion") == 1:
        validate_output_bundle(root, state, require_final=True)
        state["approvals"]["finalBento"] = "approved"
        state["validation"]["finalStatus"] = "pass"
        state["validation"]["checkedAt"] = utc_now()
        state["workflow"]["status"] = "ready"
        atomic_write_state(root, state)
        append_work_log(root, "Recorded final Bento approval after technical validation")
        return
    store = _final_artifact_store(root, state)
    store.acquire_writer_lease()
    try:
        store.recover()
        before = (root / STATE_RELATIVE).read_bytes()
        bundle = validate_output_bundle(root, state, require_final=True)
        next_state = copy.deepcopy(state)
        next_state["approvals"]["finalBento"] = _final_approval_snapshot(bundle)
        next_state["validation"]["finalStatus"] = "pass"
        next_state["validation"]["checkedAt"] = utc_now()
        next_state["workflow"]["status"] = "ready"
        _commit_final_state(root, store, before, next_state, operation="approve-final-revisions")
        state.clear()
        state.update(next_state)
    finally:
        store.release_writer_lease()
    append_work_log(root, "Recorded final Bento approval after technical validation")


def command_complete(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "bento_finalization")
    approval = state["approvals"]["finalBento"]
    if _final_approval_status(approval) != "approved":
        raise WorkflowError("Final Bento approval is still pending")
    if state.get("schemaVersion") == 1:
        validate_output_bundle(root, state, require_final=True)
        state["validation"]["finalStatus"] = "pass"
        state["validation"]["checkedAt"] = utc_now()
        state["handoff"]["readyForFinalEditing"] = False
        _transition(state, "complete", "complete")
        atomic_write_state(root, state)
        append_work_log(root, "Completed final Bento validation")
        return
    if not isinstance(approval, dict):
        raise WorkflowError("Legacy final approval is not revision-bound; run approve-final again")
    store = _final_artifact_store(root, state)
    store.acquire_writer_lease()
    try:
        store.recover()
        before = (root / STATE_RELATIVE).read_bytes()
        bundle = validate_output_bundle(root, state, require_final=True)
        current = _final_approval_snapshot(bundle, approved_at=approval["approvedAt"])
        revision_fields = (
            "documentRevision", "htmlRevision", "registryRevision", "runtimeFingerprint",
        )
        if any(approval.get(field) != current[field] for field in revision_fields):
            raise WorkflowError("Final Bento approval is stale; stop editing and run approve-final again")
        next_state = copy.deepcopy(state)
        next_state["validation"]["finalStatus"] = "pass"
        next_state["validation"]["checkedAt"] = utc_now()
        next_state["handoff"]["readyForFinalEditing"] = False
        _transition(next_state, "complete", "complete")
        _commit_final_state(root, store, before, next_state, operation="complete-final-revisions")
        state.clear()
        state.update(next_state)
    finally:
        store.release_writer_lease()
    append_work_log(root, "Completed final Bento validation")


def command_reopen_finalization(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("reopen-finalization requires deck schema v2")
    _require_stage(state, "bento_finalization", "complete")
    store = _final_artifact_store(root, state)
    store.acquire_writer_lease()
    try:
        store.recover()
        before = (root / STATE_RELATIVE).read_bytes()
        validate_output_bundle(root, state, require_final=True)
        next_state = copy.deepcopy(state)
        next_state["approvals"]["finalBento"] = _pending_final_approval()
        next_state["validation"]["finalStatus"] = "pending"
        next_state["validation"]["checkedAt"] = None
        next_state["handoff"]["readyForFinalEditing"] = True
        _transition(next_state, "bento_finalization", "in_progress")
        _commit_final_state(root, store, before, next_state, operation="reopen-finalization")
        state.clear()
        state.update(next_state)
    finally:
        store.release_writer_lease()
    append_work_log(root, "Reopened finalization and invalidated the previous final approval")


def command_block(root: Path, state: dict[str, Any], owner: str, reason: str) -> None:
    if not reason.strip():
        raise WorkflowError("A non-empty blocking reason is required")
    with planning_writer_guard_if_needed(root, state):
        workflow = state["workflow"]
        if workflow["stage"] in {"blocked", "complete"}:
            raise WorkflowError(f"Stage {workflow['stage']!r} cannot be blocked")
        workflow["blockedFrom"] = {
            "stage": workflow["stage"],
            "status": workflow["status"],
            "owner": workflow["owner"],
            "sourceOfTruth": workflow["sourceOfTruth"],
            "currentChapter": workflow["currentChapter"],
        }
        if state.get("schemaVersion") == 2:
            workflow["blockedFrom"]["currentSection"] = workflow["currentSection"]
        workflow.update({"stage": "blocked", "status": "blocked", "owner": owner, "blockingReason": reason})
        _normalize_handoff(state)
        atomic_write_state(root, state)
        append_work_log(root, f"Blocked ({owner}): {reason}")


def _validate_resume_target(root: Path, state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    stage = snapshot["stage"]
    if stage == "initialized":
        return
    if stage in {"planning", "awaiting_plan_approval"}:
        discover_source_candidates(root, state)
        if stage == "awaiting_plan_approval":
            validate_planning(root)
            units = state["sections"] if state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular" else state["chapters"]
            if not units:
                raise WorkflowError("Cannot resume plan approval without configured sections or chapters")
        return
    if any(state["approvals"][key] != "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")):
        raise WorkflowError(f"Cannot resume {stage!r} while plan approvals are incomplete")
    single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular"
    units = state["sections"] if single else state["chapters"]
    if not units:
        raise WorkflowError(f"Cannot resume {stage!r} without configured sections or chapters")
    if stage == "html_authoring":
        return
    if stage == "html_review":
        current = snapshot["currentSection"] if single else snapshot["currentChapter"]
        if not current:
            raise WorkflowError("Cannot resume HTML review without a current section or chapter")
        entry = units[current]
        if entry["status"] != "review":
            raise WorkflowError(f"Authoring unit is not awaiting review: {current}")
        if single:
            evidence = load_single_section_evidence(root, state)
            if current not in evidence or list(evidence[current].slide_ids) != entry["slideIds"]:
                raise WorkflowError(f"Section changed while workflow was blocked: {current}")
        else:
            _load_chapter(root, current, entry)
        return
    if stage in {"ready_for_conversion", "converting"}:
        validate_html_authoring(root, state, require_approved=True)
        if not state["handoff"]["readyForCodex"]:
            raise WorkflowError("Cannot resume conversion because the Work-to-Codex handoff is not ready")
        return
    if stage == "bento_validation":
        if state.get("schemaVersion") == 2:
            validate_output_bundle(root, state, require_final=False)
            authoring_storage(root, state).status()
        else:
            validate_output_bundle(root, state, require_final=True)
        return
    if stage in {"bento_authoring", "content_review"}:
        if state.get("schemaVersion") != 2:
            raise WorkflowError(f"Stage {stage!r} requires deck schema v2")
        authoring_storage(root, state).status()
        return
    if stage == "bento_finalization":
        validate_output_bundle(root, state, require_final=True)
        if not state["handoff"]["readyForFinalEditing"]:
            raise WorkflowError("Cannot resume finalization because the Codex-to-Work handoff is not ready")
        return
    raise WorkflowError(f"Unsupported resume target: {stage}")


def command_resume(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "blocked")
    snapshot = state["workflow"].get("blockedFrom")
    if not isinstance(snapshot, dict):
        raise WorkflowError("Blocked state has no resumable workflow snapshot")
    _validate_resume_target(root, state, snapshot)
    state["workflow"].update(snapshot)
    state["workflow"]["blockingReason"] = None
    state["workflow"]["blockedFrom"] = None
    _normalize_handoff(state)
    atomic_write_state(root, state)
    append_work_log(root, f"Resumed workflow at {snapshot['stage']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, help="Repository root (defaults to the checkout containing this module)")
    commands = result.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    route = commands.add_parser("route")
    route.add_argument("--json", action="store_true", dest="as_json")
    capture_request = commands.add_parser("capture-request")
    capture_request.add_argument("--text", required=True)
    write_planning = commands.add_parser("write-planning-artifact")
    write_planning.add_argument(
        "--artifact", required=True, choices=tuple(PLANNING_ARTIFACT_FILES),
    )
    planning_source = write_planning.add_mutually_exclusive_group(required=True)
    planning_source.add_argument("--from-file", type=Path)
    planning_source.add_argument("--text")
    commands.add_parser("validate")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--report", type=Path)
    set_project = commands.add_parser("set-project")
    set_project.add_argument("--kind", required=True)
    set_project.add_argument("--title", required=True)
    commands.add_parser("initialize")
    configure = commands.add_parser("configure-chapters")
    configure.add_argument("chapters", nargs="+")
    configure_sections = commands.add_parser("configure-sections")
    configure_sections.add_argument("sections", nargs="+")
    commands.add_parser("adopt-whole-deck")
    commands.add_parser("complete-html-deck")
    commands.add_parser("approve-html-deck")
    propose_html_change = commands.add_parser("propose-html-change")
    propose_html_change.add_argument("--candidate-html", required=True, type=Path)
    propose_html_change.add_argument("--candidate-registry", type=Path)
    propose_html_change.add_argument("--request", required=True)
    propose_html_change.add_argument("--summary", required=True)
    propose_html_change.add_argument("--impact-summary", required=True)
    propose_html_change.add_argument("--target-slide", action="append", required=True, dest="target_slides")
    propose_html_change.add_argument("--related-slide", action="append", default=[], dest="related_slides")
    commands.add_parser("approve-html-change")
    commands.add_parser("apply-html-change")
    check_html_change = commands.add_parser("check-html-change")
    check_html_change.add_argument("--browser-executable", type=Path)
    commands.add_parser("cancel-html-change")
    commands.add_parser("submit-plan")
    commands.add_parser("approve-plan")
    advance = commands.add_parser("advance")
    advance.add_argument("--browser-executable", type=Path)
    advance.add_argument("--skip-browser-check", action="store_true")
    commands.add_parser("approve-current")
    promote = commands.add_parser("promote-current-section")
    promote.add_argument("--browser-executable", type=Path)
    promote.add_argument("--skip-browser-check", action="store_true")
    promote_explicit = commands.add_parser("promote-section")
    promote_explicit.add_argument("--section", required=True)
    promote_explicit.add_argument("--browser-executable", type=Path)
    promote_explicit.add_argument("--skip-browser-check", action="store_true")
    commands.add_parser("edit-current")
    commands.add_parser("finish-current-section")
    commands.add_parser("review-whole-deck")
    reopen_current = commands.add_parser("reopen-current-section")
    reopen_current.add_argument("--section")
    reopen_current.add_argument("--via", choices=("bento", "html"), default="bento")
    for name in ("begin-chapter", "complete-chapter", "approve-chapter"):
        child = commands.add_parser(name)
        child.add_argument("--chapter")
    for name in ("begin-section", "complete-section", "approve-section"):
        child = commands.add_parser(name)
        child.add_argument("--section")
    unlock = commands.add_parser("unlock-section")
    unlock.add_argument("--section", required=True)
    commands.add_parser("prepare-conversion")
    commands.add_parser("mark-converted")
    commands.add_parser("begin-authoring")
    commands.add_parser("begin-content-review")
    commands.add_parser("approve-content")
    reset_authoring = commands.add_parser("reset-authoring-from-html")
    reset_authoring.add_argument("--confirm", required=True)
    reset_authoring.add_argument("--browser-executable", type=Path)
    reset_authoring.add_argument("--skip-browser-check", action="store_true")
    commands.add_parser("begin-finalization")
    restart_finalization = commands.add_parser("restart-finalization-from-authoring")
    restart_finalization.add_argument("--confirm", required=True)
    commands.add_parser("approve-final")
    commands.add_parser("complete")
    commands.add_parser("reopen-finalization")
    discover = commands.add_parser("discover-sources")
    discover.add_argument("--json", action="store_true", dest="as_json")
    blocked = commands.add_parser("block")
    blocked.add_argument("--owner", choices=("work", "codex"), required=True)
    blocked.add_argument("--reason", required=True)
    commands.add_parser("resume")
    current_url = commands.add_parser("set-current-url")
    current_url.add_argument("--url", required=True)
    commands.add_parser("clear-current-url")
    return result


def run(args: argparse.Namespace) -> int:
    root = repository_root(args.root)
    recover_repository_transactions(root)
    state = load_state(root)
    command = args.command
    if command == "status":
        command_status(root, state, as_json=args.as_json)
    elif command == "route":
        command_route(state, as_json=args.as_json)
    elif command == "capture-request":
        command_capture_request(root, state, text=args.text)
    elif command == "write-planning-artifact":
        if args.from_file is not None:
            try:
                payload = args.from_file.read_bytes()
            except OSError as exc:
                raise WorkflowError(f"Cannot read planning artifact input: {exc}") from exc
        else:
            payload = args.text.encode("utf-8")
        command_write_planning_artifacts(root, state, {args.artifact: payload})
    elif command == "validate":
        validate_current_stage(root, state)
        print(f"deck.yaml + {state['workflow']['stage']} artifacts: PASS")
    elif command == "migrate":
        report_path = _repo_path(root, str(args.report), field="migration.report") if args.report else None
        command_migrate(root, state, dry_run=args.dry_run, report_path=report_path)
    elif command == "set-project":
        command_set_project(root, state, kind=args.kind, title=args.title)
    elif command == "discover-sources":
        selected, candidates = discover_source_candidates(root, state)
        payload = {"primarySource": selected.relative_to(root).as_posix() if selected else None, "candidates": [path.relative_to(root).as_posix() for path in candidates]}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else "\n".join(payload["candidates"]))
    elif command == "initialize":
        command_initialize(root, state)
    elif command == "configure-chapters":
        command_configure_chapters(root, state, args.chapters)
    elif command == "configure-sections":
        command_configure_sections(root, state, args.sections)
    elif command == "adopt-whole-deck":
        command_adopt_whole_deck(root, state)
    elif command == "complete-html-deck":
        command_complete_html_deck(root, state)
    elif command == "approve-html-deck":
        command_approve_html_deck(root, state)
    elif command == "propose-html-change":
        proposal = command_propose_html_change(
            root, state,
            candidate_html=args.candidate_html,
            candidate_registry=args.candidate_registry,
            request=args.request,
            summary=args.summary,
            impact_summary=args.impact_summary,
            requested_slide_ids=args.target_slides,
            related_slide_ids=args.related_slides,
        )
        print(json.dumps(proposal, ensure_ascii=False, indent=2))
    elif command == "approve-html-change":
        command_approve_html_change(root, state)
    elif command == "apply-html-change":
        command_apply_html_change(root, state)
    elif command == "check-html-change":
        command_check_html_change(
            root, state, browser_executable=args.browser_executable,
        )
    elif command == "cancel-html-change":
        command_cancel_html_change(root, state)
    elif command == "submit-plan":
        command_submit_plan(root, state)
    elif command == "approve-plan":
        command_approve_plan(root, state)
    elif command == "advance":
        command_advance(
            root, state, browser_executable=args.browser_executable,
            browser_check=not args.skip_browser_check,
        )
    elif command == "approve-current":
        command_approve_current(root, state)
    elif command in {"promote-current-section", "promote-section"}:
        if command == "promote-section" and args.section != state["workflow"].get("currentSection"):
            raise WorkflowError("promote-section may target only the current reviewed section")
        command_promote_current_section(
            root, state, browser_executable=args.browser_executable,
            browser_check=not args.skip_browser_check,
        )
    elif command == "edit-current":
        route_value = workspace_route(state)
        if route_value not in {"html-preview", "authoring-editor", "final-editor"}:
            raise WorkflowError("The current stage has no editable workspace")
        print(route_value)
    elif command == "finish-current-section":
        command_finish_current_section(root, state)
    elif command == "review-whole-deck":
        command_review_whole_deck(root, state)
    elif command == "reopen-current-section":
        command_reopen_current_section(root, state, section_id=args.section, via=args.via)
    elif command == "begin-chapter":
        command_begin_chapter(root, state, args.chapter)
    elif command == "complete-chapter":
        command_complete_chapter(root, state, args.chapter)
    elif command == "approve-chapter":
        command_approve_chapter(root, state, args.chapter)
    elif command == "begin-section":
        command_begin_section(root, state, args.section)
    elif command == "complete-section":
        command_complete_section(root, state, args.section)
    elif command == "approve-section":
        command_approve_section(root, state, args.section)
    elif command == "unlock-section":
        command_unlock_section(root, state, args.section)
    elif command == "prepare-conversion":
        command_prepare_conversion(root, state)
    elif command == "mark-converted":
        command_mark_converted(root, state)
    elif command == "begin-authoring":
        command_begin_authoring(root, state)
    elif command == "begin-content-review":
        command_begin_content_review(root, state)
    elif command == "approve-content":
        command_approve_content(root, state)
    elif command == "reset-authoring-from-html":
        command_reset_authoring_from_html(
            root, state, confirmation=args.confirm,
            browser_executable=args.browser_executable,
            browser_check=not args.skip_browser_check,
        )
    elif command == "begin-finalization":
        command_begin_finalization(root, state)
    elif command == "restart-finalization-from-authoring":
        command_restart_finalization_from_authoring(
            root, state, confirmation=args.confirm,
        )
    elif command == "approve-final":
        command_approve_final(root, state)
    elif command == "complete":
        command_complete(root, state)
    elif command == "reopen-finalization":
        command_reopen_finalization(root, state)
    elif command == "block":
        command_block(root, state, args.owner, args.reason)
    elif command == "resume":
        command_resume(root, state)
    elif command == "set-current-url":
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/", args.url):
            raise WorkflowError("preview.currentUrl must be a 127.0.0.1 HTTP URL")
        port = int(args.url.rsplit(":", 1)[1].rstrip("/"))
        if port < 1 or port > 65535:
            raise WorkflowError("preview.currentUrl contains an invalid port")
        with planning_writer_guard_if_needed(root, state):
            state["preview"]["currentUrl"] = args.url
            atomic_write_state(root, state)
    elif command == "clear-current-url":
        with planning_writer_guard_if_needed(root, state):
            state["preview"]["currentUrl"] = None
            atomic_write_state(root, state)
    else:  # pragma: no cover
        raise WorkflowError(f"Unknown command: {command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (WorkflowError, BentoConverterError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
