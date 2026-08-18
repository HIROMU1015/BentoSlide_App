from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.backend.models.view_models import (
    HtmlGenerationCandidateView,
    HtmlGenerationSlide,
    HtmlGenerationStatusResponse,
    SlideItem,
    SlidesResponse,
)
from app.backend.services.ai_job_coordinator import RepositoryAiJobCoordinator
from app.backend.services.ai_proposal_service import (
    AdapterAvailability,
    CodexSdkAdapter,
    ProposalAdapter,
    _authority_text,
    _html_metadata,
    _unsupported_visible_tokens,
)
from bento_converter.artifact_transaction import (
    ArtifactTransactionStore,
    bytes_revision,
    file_revision,
)
from bento_converter.errors import BentoConverterError
from bento_converter.html_change_review import (
    HtmlChangeBrowserEvidence,
    collect_html_change_browser_evidence,
)
from bento_converter.planning_proposal import (
    PLANNING_ARTIFACT_FILENAMES,
    PLANNING_ARTIFACT_NAMES,
    PlanningCandidate,
    validate_planning_candidate,
)
from bento_converter.registry_document import validate_registry
from bento_converter.section_approval import (
    HtmlDeckStructureEvidence,
    compute_html_deck_structure_evidence,
)
from scripts.deck_workflow import (
    PLANNING_ARTIFACT_FILES,
    WorkflowError,
    _repo_path,
    command_apply_initial_html_candidate,
    load_state,
    planning_review_signature,
)


LOGGER = logging.getLogger(__name__)
JOB_FORMAT = "bento/html-generation-job/v1"
RESULT_FORMAT = "bento/html-initial-generation-result/v1"
CANDIDATE_FORMAT = "bento/html-initial-generation-candidate/v1"
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 256 * 1024
ALLOWED_SOURCE_ROLES = {"primary", "evidence", "supplementary"}
RELEVANT_SPECIFICATIONS = (
    "docs/html-first-authoring-contract.md",
    "docs/source-of-truth-policy.md",
    "docs/visual-workflow.md",
    "workflow/WORKFLOW.md",
)
STALE_MESSAGE = "承認済み構成または一次資料が変更されました。最新の内容からHTML案を作り直してください。"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_payload(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _screenshot_name(slide_id: str) -> str:
    return hashlib.sha256(slide_id.encode("utf-8")).hexdigest()[:24] + ".png"


class HtmlGenerationAgentSlide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    sectionId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=1, max_length=300)


class HtmlGenerationAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = RESULT_FORMAT
    summary: str = Field(min_length=1, max_length=1500)
    slides: list[HtmlGenerationAgentSlide] = Field(min_length=1, max_length=500)
    visualsSummary: str = Field(min_length=1, max_length=1500)
    provenanceSummary: str = Field(min_length=1, max_length=1500)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    factualChanges: list[str] = Field(default_factory=list, max_length=100)
    sourceReferences: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_values(self) -> "HtmlGenerationAgentResult":
        slide_ids = [slide.id for slide in self.slides]
        if len(slide_ids) != len(set(slide_ids)):
            raise ValueError("slides must contain unique IDs")
        if len(self.sourceReferences) != len(set(self.sourceReferences)):
            raise ValueError("sourceReferences must be unique")
        return self


class StoredHtmlGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = CANDIDATE_FORMAT
    generationId: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["proposed", "applied", "cancelled"]
    basePlanningSignature: str
    baseContextSignature: str
    baseStateRevision: str
    inputRevisions: dict[str, str | None]
    candidateHtmlRevision: str
    candidateRegistryRevision: str
    candidateReviewDigest: str
    candidateDependencyRevisions: dict[str, str]
    candidatePreviewDependencyRevisions: dict[str, str]
    browserReportRevision: str
    browserEnvironmentRevision: str
    browserEnvironmentDigest: str
    browserScreenshotRevisions: dict[str, str]
    candidateDigest: str
    instruction: str
    summary: str
    visualsSummary: str
    provenanceSummary: str
    warnings: list[str]
    sourceReferences: list[str]
    slides: list[HtmlGenerationAgentSlide]
    createdAt: str
    appliedAt: str | None = None
    cancelledAt: str | None = None


class _StaleHtmlGenerationInputs(WorkflowError):
    pass


class _CandidateMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_slide: str | None = None
        self.figures: dict[str, set[str]] = {}
        self.images: dict[str, list[str]] = {}
        self.element_ids: list[str] = []
        self.urls: list[str] = []
        self.has_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "script":
            self.has_script = True
        if values.get("data-slide-id"):
            self.current_slide = values["data-slide-id"]
        if values.get("data-bento-id"):
            self.element_ids.append(str(values["data-bento-id"]))
        if self.current_slide and values.get("data-figure-id"):
            self.figures.setdefault(self.current_slide, set()).add(str(values["data-figure-id"]))
        if self.current_slide and tag.casefold() in {"img", "image"}:
            source = values.get("src") or values.get("href") or values.get("xlink:href") or ""
            self.images.setdefault(self.current_slide, []).append(source)
        for key in ("src", "href", "poster", "data-src"):
            value = values.get(key)
            if value:
                self.urls.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "section" and self.current_slide:
            self.current_slide = None


StateLoader = Callable[[Path], dict[str, Any]]
ApplyCommand = Callable[..., dict[str, Any]]
BrowserValidator = Callable[..., HtmlChangeBrowserEvidence]


class HtmlGenerationService:
    """Generate and review the first whole-deck HTML pair from approved planning."""

    def __init__(
        self,
        repository: str | Path,
        *,
        adapter: ProposalAdapter | None = None,
        state_loader: StateLoader = load_state,
        apply_command: ApplyCommand = command_apply_initial_html_candidate,
        browser_validator: BrowserValidator = collect_html_change_browser_evidence,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.adapter = adapter or CodexSdkAdapter()
        self._state_loader = state_loader
        self._apply_command = apply_command
        self._browser_validator = browser_validator
        self._run_root = self.repository / ".bento-ai" / "runs"
        self._lock = threading.RLock()
        self._token_lock = threading.Lock()
        self._token_signature = ""
        self._token = ""
        self._thread: threading.Thread | None = None
        self._availability: AdapterAvailability | None = None
        self._status = HtmlGenerationStatusResponse(
            available=False,
            reason="AI利用可否を確認していません。",
            allowedStage=False,
            status="idle",
            message="AI HTML生成の利用可否を確認しています。",
        )
        self._recover()

    def _state(self) -> dict[str, Any]:
        return self._state_loader(self.repository)

    def _canonical_paths(self, state: dict[str, Any]) -> tuple[Path, Path]:
        return (
            _repo_path(self.repository, state["authoring"]["entryHtml"], field="authoring.entryHtml"),
            _repo_path(self.repository, state["authoring"]["registry"], field="authoring.registry"),
        )

    def _state_is_supported(self, state: dict[str, Any]) -> bool:
        try:
            html_path, registry_path = self._canonical_paths(state)
        except BaseException:
            return False
        approvals = state.get("approvals", {})
        sections = state.get("sections")
        return (
            state.get("schemaVersion") == 2
            and state.get("workflow", {}).get("stage") == "html_authoring"
            and state.get("authoring", {}).get("mode") in {"single", "imported"}
            and state.get("authoring", {}).get("strategy") == "whole_deck"
            and all(approvals.get(key) == "approved" for key in (
                "explanationPolicy", "storyOutline", "slidePlan",
            ))
            and isinstance(sections, dict)
            and bool(sections)
            and all(isinstance(value, dict) and value.get("slideIds") for value in sections.values())
            and not html_path.exists()
            and not registry_path.exists()
            and state.get("authoring", {}).get("htmlChange") is None
        )

    def _availability_status(self) -> AdapterAvailability:
        if self._availability is None:
            self._availability = self.adapter.availability()
        return self._availability

    def _job_path(self, generation_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", generation_id):
            raise WorkflowError("HTML Generation ID is invalid")
        return self._run_root / generation_id

    def _candidate_marker_path(self, generation_id: str) -> Path:
        return self._job_path(generation_id) / "html-generation.json"

    def _active_markers(self) -> list[Path]:
        if not self._run_root.is_dir():
            return []
        result: list[Path] = []
        for path in self._run_root.glob("*/html-generation.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("format") == CANDIDATE_FORMAT and value.get("status") == "proposed":
                result.append(path)
        return sorted(result, key=lambda path: path.stat().st_mtime_ns, reverse=True)

    def _active_id(self) -> str | None:
        markers = self._active_markers()
        if len(markers) > 1:
            raise WorkflowError("複数の初期HTML Candidateが残っています。安全のため操作を停止しました。")
        return markers[0].parent.name if markers else None

    def _recover(self) -> None:
        if self._run_root.is_dir():
            for marker in sorted(self._run_root.glob("*/html-generation-job.json"), reverse=True):
                try:
                    value = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and value.get("format") == JOB_FORMAT and value.get("status") == "running":
                    value.update(status="failed", phase="failed")
                    marker.write_bytes(_json_payload(value))
                    self._status = self._failed(
                        "前回のAI HTML生成が中断されました。再試行できます。", retryable=True,
                    )
                    break
        try:
            generation_id = self._active_id()
        except WorkflowError as exc:
            self._status = self._failed(str(exc), retryable=False)
            return
        if generation_id:
            self._status = HtmlGenerationStatusResponse(
                available=False,
                reason="AI利用可否を確認していません。",
                allowedStage=False,
                status="succeeded",
                phase="ready",
                message="生成されたHTML案を確認できます。現在のHTMLはまだ作成していません。",
                hasCandidate=True,
                generationId=generation_id,
            )

    def _failed(self, message: str, *, retryable: bool) -> HtmlGenerationStatusResponse:
        return HtmlGenerationStatusResponse(
            available=True,
            reason=None,
            allowedStage=False,
            status="failed",
            phase="failed",
            message=message,
            error=message,
            retryable=retryable,
        )

    def status(self) -> HtmlGenerationStatusResponse:
        availability = self._availability_status()
        with self._lock:
            current = self._status.model_copy(deep=True)
            thread_running = self._thread is not None and self._thread.is_alive()
        current.available = availability.available
        current.reason = availability.reason
        current.candidate = None
        if thread_running and current.status == "running":
            # Candidate metadata and browser evidence are installed by one
            # transaction.  On Windows an eager poll must not observe the
            # proposed marker while a screenshot is still being replaced.
            current.hasCandidate = False
            current.generationId = None
            current.retryable = False
            return current
        try:
            active_id = self._active_id()
            supported = self._state_is_supported(self._state())
        except BaseException:
            active_id = None
            supported = False
        current.hasCandidate = active_id is not None
        current.generationId = active_id
        current.allowedStage = supported and active_id is None
        current.candidate = None
        if active_id is not None:
            try:
                current.candidate = self.candidate(active_id)
                current.status = "succeeded"
                current.phase = "ready"
                current.message = "生成されたHTML案を確認できます。現在のHTMLはまだ作成していません。"
                current.retryable = False
            except WorkflowError as exc:
                return self._failed(str(exc), retryable=False)
        elif current.status == "idle":
            current.message = (
                "承認済み構成からHTML案を生成できます。"
                if current.available and supported
                else availability.reason or "初期HTML生成は承認済み構成のHTML制作段階でのみ利用できます。"
            )
        elif current.status == "failed":
            current.retryable = current.retryable and current.allowedStage and current.available
        return current

    def start(self, *, instruction: str = "") -> HtmlGenerationStatusResponse:
        availability = self._availability_status()
        if not availability.available:
            raise WorkflowError(availability.reason or "AI HTML生成を利用できません")
        cleaned = instruction.strip()
        if len(cleaned) > 2000 or any(ord(character) < 32 and character not in "\t\r\n" for character in cleaned):
            raise WorkflowError("補助指示が長すぎるか、利用できない制御文字を含んでいます")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WorkflowError("AI HTML生成はすでに実行中です")
            state = self._state()
            if not self._state_is_supported(state):
                raise WorkflowError("初期HTML生成は承認済み構成のHTML制作段階で、HTML未生成の場合だけ利用できます")
            if self._active_id() is not None:
                raise WorkflowError("現在のHTML案を採用または破棄してから再生成してください")
            if not RepositoryAiJobCoordinator.claim(self.repository):
                raise WorkflowError("AI候補生成はすでに実行中です")
            generation_id = uuid.uuid4().hex
            workspace = self._job_path(generation_id)
            self._marker(workspace, status="running", phase="preparing")
            self._status = HtmlGenerationStatusResponse(
                available=True,
                reason=None,
                allowedStage=True,
                status="running",
                phase="preparing",
                message="承認済み構成と一次資料を安全に準備しています。",
            )
            thread = threading.Thread(
                target=self._run,
                args=(workspace, generation_id, cleaned),
                name=f"bentoslide-html-generation-{generation_id[:8]}",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                RepositoryAiJobCoordinator.release(self.repository)
                self._status = self._failed("AI HTML生成を開始できませんでした。", retryable=True)
                raise
        return self.status()

    def _marker(self, workspace: Path, *, status: str, phase: str) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "html-generation-job.json").write_bytes(_json_payload({
            "format": JOB_FORMAT,
            "status": status,
            "phase": phase,
        }))

    def _update(self, workspace: Path, phase: str, message: str) -> None:
        self._marker(workspace, status="running", phase=phase)
        with self._lock:
            self._status = self._status.model_copy(update={"phase": phase, "message": message})

    def _run(self, workspace: Path, generation_id: str, instruction: str) -> None:
        try:
            self._update(workspace, "preparing", "承認済み構成と一次資料を安全に準備しています。")
            context = self._prepare_workspace(workspace, instruction)
            self._update(workspace, "generating", "承認済み構成からスライド全体のHTML案を生成しています。")
            self.adapter.generate(workspace, self._prompt())
            self._require_current_inputs(context)
            self._update(workspace, "validating", "HTML、registry、構成、出典を検証しています。")
            result, evidence, candidate_registry = self._validate_outputs(workspace, context)
            self._require_current_inputs(context)
            self._update(workspace, "browser-checking", "ブラウザで全スライドの表示を確認しています。")
            browser = self._validate_browser(workspace, evidence)
            self._require_current_inputs(context)
            self._update(workspace, "registering-candidate", "確認用のHTML Candidateを登録しています。")
            self._register_candidate(
                workspace=workspace,
                generation_id=generation_id,
                instruction=instruction,
                context=context,
                result=result,
                evidence=evidence,
                candidate_registry=candidate_registry,
                browser=browser,
            )
            with self._lock:
                self._status = HtmlGenerationStatusResponse(
                    available=True,
                    reason=None,
                    allowedStage=False,
                    status="succeeded",
                    phase="ready",
                    message="生成されたHTML案を確認できます。現在のHTMLはまだ作成していません。",
                    hasCandidate=True,
                    generationId=generation_id,
                )
        except BaseException as exc:
            LOGGER.exception("Initial HTML generation failed")
            if isinstance(exc, _StaleHtmlGenerationInputs):
                message = STALE_MESSAGE
            elif isinstance(exc, (WorkflowError, BentoConverterError)):
                message = str(exc)
            else:
                message = "HTML案を安全に生成できませんでした。内容を確認して再試行してください。"
            try:
                self._marker(workspace, status="failed", phase="failed")
            except OSError:
                LOGGER.exception("Unable to persist failed HTML generation marker")
            with self._lock:
                self._status = self._failed(message, retryable=self._state_is_supported_safe())
        finally:
            RepositoryAiJobCoordinator.release(self.repository)

    def _state_is_supported_safe(self) -> bool:
        try:
            return self._state_is_supported(self._state()) and self._active_id() is None
        except BaseException:
            return False

    def _planning_snapshot(self, state: dict[str, Any]) -> PlanningCandidate:
        artifacts: dict[str, bytes] = {}
        for name, relative in PLANNING_ARTIFACT_FILES.items():
            path = (self.repository / relative).resolve()
            if not path.is_file():
                raise WorkflowError("初期HTML生成には承認済みの4つのplanning文書が必要です")
            artifacts[name] = path.read_bytes()
        sections = [
            {"id": str(section_id), "title": str(value.get("title") or section_id), "slideIds": list(value.get("slideIds") or [])}
            for section_id, value in state["sections"].items()
        ]
        try:
            return validate_planning_candidate(artifacts, sections)
        except BentoConverterError as exc:
            raise WorkflowError("承認済みplanning snapshotが初期HTML生成の厳密な形式を満たしていません") from exc

    def _source_entries(self, state: dict[str, Any]) -> tuple[list[dict[str, str]], dict[Path, str | None]]:
        manifest = _repo_path(self.repository, state["sources"]["manifest"], field="sources.manifest")
        try:
            value = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise WorkflowError("Source manifestを読み取れません") from exc
        items = value.get("items", []) if isinstance(value, dict) else []
        entries: list[dict[str, str]] = []
        revisions: dict[Path, str | None] = {manifest: file_revision(manifest)}
        used_ids: set[str] = set()
        used_paths: set[Path] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("role") not in ALLOWED_SOURCE_ROLES:
                continue
            source_id = str(item.get("id") or "")
            relative = item.get("path")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", source_id) or not isinstance(relative, str):
                raise WorkflowError("Source manifest entry is invalid")
            source = _repo_path(self.repository, relative, field="sources.items.path")
            if not source.is_file():
                raise WorkflowError(f"許可されたsourceが見つかりません: {relative}")
            if source_id in used_ids or source in used_paths:
                continue
            entry = {
                "id": source_id,
                "path": source.relative_to(self.repository).as_posix(),
                "type": str(item.get("type") or "application/octet-stream"),
                "role": str(item["role"]),
            }
            entries.append(entry)
            revisions[source] = file_revision(source)
            used_ids.add(source_id)
            used_paths.add(source)
        for index, relative in enumerate(state.get("project", {}).get("supplementarySources") or [], start=1):
            source = _repo_path(self.repository, relative, field="project.supplementarySources")
            if source in used_paths or not source.is_file():
                continue
            source_id = f"project-supplementary-{index}"
            entries.append({
                "id": source_id,
                "path": source.relative_to(self.repository).as_posix(),
                "type": "application/octet-stream",
                "role": "supplementary",
            })
            revisions[source] = file_revision(source)
            used_ids.add(source_id)
            used_paths.add(source)
        if not any(entry["role"] == "primary" for entry in entries):
            raise WorkflowError("初期HTML生成に利用できるprimary sourceがありません")
        return entries, revisions

    def _context_signature(
        self,
        state: dict[str, Any],
        revisions: dict[Path, str | None] | None = None,
    ) -> str:
        if revisions is None:
            _, revisions = self._source_entries(state)
            request = _repo_path(
                self.repository, state["project"]["request"], field="project.request",
            )
            revisions[request] = file_revision(request)
            for relative in RELEVANT_SPECIFICATIONS:
                specification = _repo_path(
                    self.repository, relative, field="HTML generation specification",
                )
                revisions[specification] = file_revision(specification)
        records = [
            {"path": path.relative_to(self.repository).as_posix(), "revision": revision}
            for path, revision in sorted(revisions.items(), key=lambda item: item[0].as_posix().casefold())
        ]
        value = {
            "format": "bento/html-generation-context/v1",
            "planningSignature": planning_review_signature(self.repository, state),
            "inputs": records,
        }
        return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _prepare_workspace(self, workspace: Path, instruction: str) -> dict[str, Any]:
        state = self._state()
        if not self._state_is_supported(state):
            raise WorkflowError("現在の状態では初期HTMLを生成できません")
        planning = self._planning_snapshot(state)
        inputs = workspace / "inputs"
        planning_dir = inputs / "planning"
        planning_dir.mkdir(parents=True, exist_ok=False)
        for name in PLANNING_ARTIFACT_NAMES:
            (planning_dir / PLANNING_ARTIFACT_FILENAMES[name]).write_bytes(planning.artifacts[name])
        request_path = _repo_path(self.repository, state["project"]["request"], field="project.request")
        shutil.copyfile(request_path, inputs / "REQUEST.md")
        entries, source_revisions = self._source_entries(state)
        source_revisions[request_path] = file_revision(request_path)
        sources_dir = inputs / "sources"
        sources_dir.mkdir()
        for entry in entries:
            source = _repo_path(self.repository, entry["path"], field="source snapshot")
            target = sources_dir / entry["id"] / source.name
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
        (inputs / "source-manifest.json").write_bytes(_json_payload({"items": entries}))
        (inputs / "project.json").write_bytes(_json_payload({
            "kind": state["project"]["kind"],
            "title": state["project"]["title"],
        }))
        (inputs / "sections.json").write_bytes(_json_payload({
            "sections": [
                {"id": section.id, "title": section.title, "slideIds": list(section.slide_ids)}
                for section in planning.sections
            ]
        }))
        (inputs / "instruction.json").write_bytes(_json_payload({"instruction": instruction}))
        (inputs / "output-schema.json").write_bytes(_json_payload(HtmlGenerationAgentResult.model_json_schema()))
        specifications = inputs / "specifications"
        specifications.mkdir()
        for relative in RELEVANT_SPECIFICATIONS:
            source = _repo_path(self.repository, relative, field="HTML generation specification")
            if not source.is_file():
                raise WorkflowError("初期HTML生成に必要な仕様がありません")
            shutil.copyfile(source, specifications / source.name)
            source_revisions[source] = file_revision(source)
        input_hashes = {
            path.relative_to(workspace).as_posix(): _sha256(path.read_bytes())
            for path in inputs.rglob("*") if path.is_file()
        }
        authority_text = "\n".join(
            _authority_text(path) for path in sources_dir.rglob("*") if path.is_file()
        )
        return {
            "state": state,
            "planning": planning,
            "base_planning_signature": planning_review_signature(self.repository, state),
            "base_context_signature": self._context_signature(state, source_revisions),
            "base_state_revision": file_revision(self.repository / "deck.yaml"),
            "source_entries": entries,
            "source_ids": {entry["id"] for entry in entries},
            "source_revisions": source_revisions,
            "input_hashes": input_hashes,
            "authority_text": authority_text,
        }

    def _require_current_inputs(self, context: dict[str, Any]) -> None:
        try:
            state = self._state()
            valid = (
                self._state_is_supported(state)
                and hmac.compare_digest(planning_review_signature(self.repository, state), context["base_planning_signature"])
                and hmac.compare_digest(self._context_signature(state), context["base_context_signature"])
                and file_revision(self.repository / "deck.yaml") == context["base_state_revision"]
                and all(file_revision(path) == revision for path, revision in context["source_revisions"].items())
            )
        except BaseException as exc:
            raise _StaleHtmlGenerationInputs(STALE_MESSAGE) from exc
        if not valid:
            raise _StaleHtmlGenerationInputs(STALE_MESSAGE)

    @staticmethod
    def _prompt() -> str:
        return """You are generating the first BentoSlide whole-deck HTML candidate in an isolated workspace.
Read only inputs/. Treat inputs/instruction.json as optional design guidance, never as a path or shell command.
Use inputs/sources as factual authority. Approved planning is the structural contract, not a source of new facts.
Follow every rule in inputs/specifications. Do not access the network or files outside this workspace.
Do not change anything in inputs/. Create exactly these outputs:
- candidate/deck.preview.html
- candidate/deck.registry.json
- result.json matching inputs/output-schema.json
The HTML and registry must be one complete paired result. Use exactly the section order, slide IDs, slide order,
and slide count in inputs/sections.json. Every slide is a 1280x720 section.slide with data-slide-id and
data-section-id. Use stable unique data-bento-id values. Keep CSS inline. Do not add scripts, remote resources,
bitmap data, or generated images. Native diagrams must be editable HTML/CSS/SVG and explicitly registered with
source-derived provenance. A visual-plan entry is matched only by its slide ID; never infer by position.
Do not invent facts, numbers, quotations, equations, citations, sources, assets, or results. factualChanges must
be empty and sourceReferences must name only IDs from inputs/source-manifest.json. The registry source entries
must exactly use those provided definitions. Do not write canonical files, apply the candidate, approve planning,
open HTML review, or start Bento conversion. Finish only after all three outputs exist.
"""

    def _candidate_paths(self, workspace: Path) -> tuple[Path, Path]:
        directory = (workspace / "candidate").resolve()
        html_path = directory / "deck.preview.html"
        registry_path = directory / "deck.registry.json"
        for path, maximum in ((html_path, MAX_HTML_BYTES), (registry_path, MAX_REGISTRY_BYTES)):
            if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
                raise WorkflowError("AI HTML Candidateが安全な作業領域にありません")
            if path.stat().st_size > maximum:
                raise WorkflowError("AI HTML Candidateが許容サイズを超えています")
        return html_path, registry_path

    def _load_result(self, workspace: Path) -> HtmlGenerationAgentResult:
        path = workspace / "result.json"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != workspace.resolve():
            raise WorkflowError("AI HTML resultが安全な作業領域にありません")
        payload = path.read_bytes()
        if len(payload) > MAX_RESULT_BYTES:
            raise WorkflowError("AI HTML resultが許容サイズを超えています")
        try:
            result = HtmlGenerationAgentResult.model_validate_json(payload)
        except ValidationError as exc:
            raise WorkflowError("AI HTML resultが必要なschemaを満たしていません") from exc
        if result.format != RESULT_FORMAT:
            raise WorkflowError("AI HTML result formatが一致しません")
        return result

    @staticmethod
    def _planned_order(planning: PlanningCandidate) -> tuple[list[str], dict[str, str]]:
        ordered = [slide.id for slide in planning.slides]
        sections = {slide.id: slide.section_id for slide in planning.slides}
        return ordered, sections

    def _validate_registry_sources(
        self, registry: dict[str, Any], entries: list[dict[str, str]], result: HtmlGenerationAgentResult,
    ) -> None:
        allowed = {entry["id"]: entry for entry in entries}
        sources = registry.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise WorkflowError("HTML Candidate registryにsource定義がありません")
        if not set(sources) <= set(allowed):
            raise WorkflowError("HTML Candidate registryに許可されていないsourceがあります")
        for source_id, definition in sources.items():
            if definition != {key: allowed[source_id][key] for key in ("path", "type", "role")}:
                raise WorkflowError("HTML Candidate registryのsource定義がmanifestと一致しません")
        if not set(result.sourceReferences) <= set(allowed) or not set(result.sourceReferences) <= set(sources):
            raise WorkflowError("AI HTML resultのsource referenceがregistryと一致しません")

    def _validate_visuals(
        self,
        html_payload: bytes,
        registry: dict[str, Any],
        planning: PlanningCandidate,
        warnings: list[str],
    ) -> None:
        parser = _CandidateMarkupParser()
        try:
            parser.feed(html_payload.decode("utf-8"))
            parser.close()
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkflowError("HTML CandidateをUTF-8 HTMLとして解析できません") from exc
        if parser.has_script:
            raise WorkflowError("HTML Candidateにscriptを追加できません")
        if len(parser.element_ids) != len(set(parser.element_ids)):
            raise WorkflowError("HTML Candidateのdata-bento-idはdeck全体でuniqueである必要があります")
        for raw_url in parser.urls:
            split = urlsplit(raw_url.strip())
            if split.scheme or split.netloc or raw_url.strip().startswith("//"):
                raise WorkflowError("HTML Candidateにexternal/network resourceを追加できません")
            if raw_url.strip().casefold().startswith(("data:image", "javascript:")):
                raise WorkflowError("HTML Candidateにbitmap dataまたは実行可能URLを追加できません")
        visual_by_id = {str(entry["id"]): entry for entry in planning.visual_plan["slides"]}
        figures = registry.get("figures", {})
        assets = registry.get("assets", {})
        for slide in planning.slides:
            entry = visual_by_id[slide.id]["visual"]
            visual_type = entry.get("type")
            slide_figures = parser.figures.get(slide.id, set())
            slide_images = parser.images.get(slide.id, [])
            if visual_type == "none" and (slide_figures or slide_images):
                raise WorkflowError(f"Visual planがnoneのスライドに図を追加できません: {slide.id}")
            if visual_type == "generated-image":
                if slide_figures or slide_images:
                    raise WorkflowError("このフェーズでは画像生成を行えません")
                if not any("画像" in warning or "image" in warning.casefold() for warning in warnings):
                    raise WorkflowError("未対応のgenerated-imageはwarningとして明示してください")
            if visual_type == "native-diagram" and entry.get("recommended") and not slide_figures:
                raise WorkflowError(f"native-diagramがexplicit figure IDへ結び付いていません: {slide.id}")
            for figure_id in slide_figures:
                definition = figures.get(figure_id)
                if not isinstance(definition, dict):
                    raise WorkflowError(f"HTML Candidateが未登録figureを参照しています: {figure_id}")
                origin = definition.get("origin")
                kind = origin.get("kind") if isinstance(origin, dict) else None
                if visual_type == "native-diagram" and kind != "source-derived":
                    raise WorkflowError("native-diagramはsource-derived provenanceが必要です")
                if visual_type == "source-figure":
                    if kind != "source-original" or not isinstance(definition.get("assetId"), str):
                        raise WorkflowError("source-figureは登録済みsource-original assetが必要です")
                    if definition["assetId"] not in assets:
                        raise WorkflowError("source-figureが未登録assetを参照しています")

    def _copy_candidate_dependencies(
        self, candidate_dir: Path, canonical_html: Path, revisions: dict[str, str],
    ) -> None:
        for relative in revisions:
            source = _repo_path(self.repository, relative, field="HTML Candidate dependency")
            try:
                local = source.relative_to(canonical_html.parent)
            except ValueError as exc:
                raise WorkflowError("HTML Candidate dependency must remain under the canonical HTML directory") from exc
            target = (candidate_dir / local).resolve()
            try:
                target.relative_to(candidate_dir.resolve())
            except ValueError as exc:
                raise WorkflowError("HTML Candidate dependency escapes the isolated workspace") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _validate_outputs(
        self, workspace: Path, context: dict[str, Any],
    ) -> tuple[HtmlGenerationAgentResult, HtmlDeckStructureEvidence, dict[str, Any]]:
        for relative, expected in context["input_hashes"].items():
            path = workspace / relative
            if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != expected:
                raise WorkflowError("AIが読み取り専用のHTML生成入力を変更しました")
        result = self._load_result(workspace)
        if result.factualChanges:
            raise WorkflowError("初期HTML生成では新しい事実を追加できません")
        html_path, registry_path = self._candidate_paths(workspace)
        html_payload = html_path.read_bytes()
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowError("HTML Candidate registryを読み取れません") from exc
        if not isinstance(registry, dict):
            raise WorkflowError("HTML Candidate registry rootはobjectである必要があります")
        try:
            validate_registry(registry, allow_v1=False)
        except BentoConverterError as exc:
            raise WorkflowError(str(exc)) from exc
        self._validate_registry_sources(registry, context["source_entries"], result)
        planning: PlanningCandidate = context["planning"]
        expected_ids, expected_sections = self._planned_order(planning)
        result_ids = [slide.id for slide in result.slides]
        if result_ids != expected_ids:
            raise WorkflowError("AI HTML resultのslide ID・順序が承認済みPlanningと一致しません")
        if {slide.id: slide.sectionId for slide in result.slides} != expected_sections:
            raise WorkflowError("AI HTML resultのsection対応が承認済みPlanningと一致しません")
        candidate_meta = _html_metadata(html_payload)
        planning_text = "\n".join(
            [planning.texts["explanation-policy"], planning.texts["story-outline"], planning.texts["slide-plan"]]
        )
        visible_text = " ".join(candidate_meta.visible_parts)
        if _unsupported_visible_tokens(visible_text, planning_text + "\n" + context["authority_text"]):
            raise WorkflowError("HTML Candidateに承認済みPlanningまたはsourceで確認できない記述があります")
        authority_numbers = set(re.findall(
            r"(?<![\w.-])\d+(?:[.,]\d+)*(?:%|[A-Za-z]+)?",
            planning_text + "\n" + context["authority_text"],
        ))
        candidate_numbers = set(re.findall(
            r"(?<![\w.-])\d+(?:[.,]\d+)*(?:%|[A-Za-z]+)?", visible_text,
        ))
        if not candidate_numbers <= authority_numbers:
            raise WorkflowError("HTML Candidateに根拠のない新しい数値があります")
        self._validate_visuals(html_payload, registry, planning, result.warnings)
        canonical_html, _ = self._canonical_paths(context["state"])
        canonical_html.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            prefix=".html-generation-", suffix=".preview.html", dir=canonical_html.parent, delete=False,
        )
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                temporary.write(html_payload)
            evidence = compute_html_deck_structure_evidence(
                temporary_path, registry, repository=self.repository,
            )
        finally:
            temporary_path.unlink(missing_ok=True)
        if list(evidence.ordered_slide_ids) != expected_ids:
            raise WorkflowError("HTML Candidateのslide ID・順序が承認済みPlanningと一致しません")
        if evidence.slide_section_ids != expected_sections:
            raise WorkflowError("HTML Candidateのsection対応が承認済みPlanningと一致しません")
        if list(evidence.section_digests) != [section.id for section in planning.sections]:
            raise WorkflowError("HTML Candidateのsection順序が承認済みPlanningと一致しません")
        self._copy_candidate_dependencies(html_path.parent, canonical_html, evidence.dependency_hashes)
        return result, evidence, registry

    def _validate_browser(
        self, workspace: Path, evidence: HtmlDeckStructureEvidence,
    ) -> HtmlChangeBrowserEvidence:
        html_path, registry_path = self._candidate_paths(workspace)
        browser = self._browser_validator(
            html_path=html_path,
            registry_path=registry_path,
            affected_slide_ids=evidence.ordered_slide_ids,
            screenshots_dir=workspace / "browser" / "screenshots",
            browser_executable=None,
        )
        if browser.report.get("status") != "pass":
            raise WorkflowError("HTML Candidateのbrowser validationが成功しませんでした")
        if browser.report.get("affectedSlideIds") != list(evidence.ordered_slide_ids):
            raise WorkflowError("HTML Candidateのbrowser evidenceが全スライドを対象にしていません")
        for check in browser.report.get("checks", []):
            size = check.get("sourceSize") if isinstance(check, dict) else None
            if not isinstance(size, dict) or float(size.get("w") or 0) != 1280 or float(size.get("h") or 0) != 720:
                raise WorkflowError("HTML Candidateの各スライドは1280x720である必要があります")
        return browser

    @staticmethod
    def _candidate_digest(metadata: dict[str, Any]) -> str:
        fields = {
            key: metadata[key]
            for key in (
                "format", "generationId", "basePlanningSignature", "baseContextSignature",
                "baseStateRevision", "inputRevisions", "candidateHtmlRevision",
                "candidateRegistryRevision", "candidateReviewDigest", "candidateDependencyRevisions",
                "candidatePreviewDependencyRevisions",
                "browserReportRevision", "browserEnvironmentRevision", "browserEnvironmentDigest",
                "browserScreenshotRevisions", "instruction", "summary", "visualsSummary",
                "provenanceSummary", "warnings", "sourceReferences", "slides", "createdAt",
            )
        }
        return _sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _register_candidate(
        self,
        *,
        workspace: Path,
        generation_id: str,
        instruction: str,
        context: dict[str, Any],
        result: HtmlGenerationAgentResult,
        evidence: HtmlDeckStructureEvidence,
        candidate_registry: dict[str, Any],
        browser: HtmlChangeBrowserEvidence,
    ) -> None:
        del candidate_registry
        html_path, registry_path = self._candidate_paths(workspace)
        browser_root = workspace / "browser"
        report_path = browser_root / "report.json"
        environment_path = browser_root / "environment.json"
        report_payload = _json_payload(browser.report)
        environment_payload = _json_payload(browser.environment)
        screenshot_payloads = {slide_id: path.read_bytes() for slide_id, path in browser.screenshots.items()}
        screenshot_revisions = {slide_id: bytes_revision(payload) for slide_id, payload in screenshot_payloads.items()}
        canonical_html, _ = self._canonical_paths(context["state"])
        preview_dependency_revisions: dict[str, str] = {}
        for relative, revision in evidence.dependency_hashes.items():
            source = _repo_path(self.repository, relative, field="HTML Candidate dependency")
            local = source.relative_to(canonical_html.parent).as_posix()
            preview = html_path.parent.joinpath(*PurePosixPath(local).parts)
            if file_revision(preview) != revision:
                raise WorkflowError("HTML Candidate preview dependencyが生成時のrevisionと一致しません")
            preview_dependency_revisions[local] = revision
        metadata: dict[str, Any] = {
            "format": CANDIDATE_FORMAT,
            "generationId": generation_id,
            "status": "proposed",
            "basePlanningSignature": context["base_planning_signature"],
            "baseContextSignature": context["base_context_signature"],
            "baseStateRevision": context["base_state_revision"],
            "inputRevisions": {
                path.relative_to(self.repository).as_posix(): revision
                for path, revision in context["source_revisions"].items()
            },
            "candidateHtmlRevision": file_revision(html_path),
            "candidateRegistryRevision": file_revision(registry_path),
            "candidateReviewDigest": evidence.review_digest,
            "candidateDependencyRevisions": dict(evidence.dependency_hashes),
            "candidatePreviewDependencyRevisions": preview_dependency_revisions,
            "browserReportRevision": bytes_revision(report_payload),
            "browserEnvironmentRevision": bytes_revision(environment_payload),
            "browserEnvironmentDigest": str(browser.environment.get("environmentDigest") or ""),
            "browserScreenshotRevisions": screenshot_revisions,
            "candidateDigest": "",
            "instruction": instruction,
            "summary": result.summary,
            "visualsSummary": result.visualsSummary,
            "provenanceSummary": result.provenanceSummary,
            "warnings": list(result.warnings),
            "sourceReferences": list(result.sourceReferences),
            "slides": [slide.model_dump() for slide in result.slides],
            "createdAt": _utc_now(),
            "appliedAt": None,
            "cancelledAt": None,
        }
        metadata["candidateDigest"] = self._candidate_digest(metadata)
        marker_path = self._candidate_marker_path(generation_id)
        job_path = workspace / "html-generation-job.json"
        payloads: dict[Path, bytes] = {
            marker_path: _json_payload(metadata),
            job_path: _json_payload({"format": JOB_FORMAT, "status": "succeeded", "phase": "ready"}),
            report_path: report_payload,
            environment_path: environment_payload,
        }
        for slide_id, payload in screenshot_payloads.items():
            payloads[browser_root / "screenshots" / _screenshot_name(slide_id)] = payload
        self._require_current_inputs(context)
        ArtifactTransactionStore(self.repository, tuple(payloads)).commit(
            payloads,
            operation="register-ai-initial-html-candidate",
            validate_base=lambda: self._require_current_inputs(context),
        )

    def _load_stored(self, generation_id: str) -> tuple[StoredHtmlGeneration, Path]:
        path = self._candidate_marker_path(generation_id)
        if path.is_symlink() or not path.is_file() or path.resolve().parent != path.parent.resolve():
            raise WorkflowError("初期HTML Candidateが見つかりません")
        try:
            stored = StoredHtmlGeneration.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise WorkflowError("初期HTML Candidate metadataが不正です") from exc
        if stored.format != CANDIDATE_FORMAT or stored.generationId != generation_id:
            raise WorkflowError("初期HTML Candidate metadataが要求と一致しません")
        return stored, path

    def _candidate_files(self, generation_id: str) -> tuple[Path, Path]:
        directory = self._job_path(generation_id) / "candidate"
        return directory / "deck.preview.html", directory / "deck.registry.json"

    def _preview_dependency_path(self, generation_id: str, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise WorkflowError("HTML Candidate preview dependency pathが不正です")
        candidate_dir = (self._job_path(generation_id) / "candidate").resolve()
        path = candidate_dir.joinpath(*pure.parts).resolve()
        try:
            path.relative_to(candidate_dir)
        except ValueError as exc:
            raise WorkflowError("HTML Candidate preview dependencyが作業領域外を参照しています") from exc
        return path

    def _validate_stored(self, stored: StoredHtmlGeneration, marker: Path) -> tuple[Path, Path]:
        if stored.status != "proposed":
            raise WorkflowError("初期HTML Candidateはすでに解決済みです")
        html_path, registry_path = self._candidate_files(stored.generationId)
        if file_revision(html_path) != stored.candidateHtmlRevision or file_revision(registry_path) != stored.candidateRegistryRevision:
            raise WorkflowError("初期HTML Candidateが登録後に変更されています")
        if not hmac.compare_digest(self._candidate_digest(stored.model_dump()), stored.candidateDigest):
            raise WorkflowError("初期HTML Candidateの説明またはbindingが変更されています")
        for relative, revision in stored.candidateDependencyRevisions.items():
            if file_revision(_repo_path(self.repository, relative, field="HTML Candidate dependency")) != revision:
                raise WorkflowError("初期HTML Candidateのdependencyが変更されています")
        for relative, revision in stored.candidatePreviewDependencyRevisions.items():
            if file_revision(self._preview_dependency_path(stored.generationId, relative)) != revision:
                raise WorkflowError("初期HTML Candidateのpreview dependencyが変更されています")
        browser_root = marker.parent / "browser"
        if file_revision(browser_root / "report.json") != stored.browserReportRevision:
            raise WorkflowError("初期HTML Candidateのbrowser reportが変更されています")
        if file_revision(browser_root / "environment.json") != stored.browserEnvironmentRevision:
            raise WorkflowError("初期HTML Candidateのbrowser environmentが変更されています")
        for slide_id, revision in stored.browserScreenshotRevisions.items():
            if file_revision(browser_root / "screenshots" / _screenshot_name(slide_id)) != revision:
                raise WorkflowError("初期HTML Candidateのbrowser screenshotが変更されています")
        return html_path, registry_path

    def _action_signature(self, stored: StoredHtmlGeneration, marker: Path) -> str:
        try:
            current_context = self._context_signature(self._state())
        except BaseException:
            current_context = "unavailable"
        return "\0".join((
            stored.generationId,
            stored.status,
            stored.candidateDigest,
            file_revision(marker) or "missing",
            current_context,
        ))

    def _action_token(self, stored: StoredHtmlGeneration, marker: Path) -> str:
        signature = self._action_signature(stored, marker)
        with self._token_lock:
            if signature != self._token_signature:
                self._token_signature = signature
                self._token = secrets.token_urlsafe(32)
            return self._token

    def candidate(self, generation_id: str | None = None) -> HtmlGenerationCandidateView:
        selected = generation_id or self._active_id()
        if selected is None:
            raise WorkflowError("確認できる初期HTML Candidateはありません")
        stored, marker = self._load_stored(selected)
        self._validate_stored(stored, marker)
        state = self._state()
        section_titles = {
            str(section_id): str(value.get("title") or section_id)
            for section_id, value in state.get("sections", {}).items()
        }
        slides = [
            HtmlGenerationSlide(
                id=slide.id,
                title=slide.title,
                number=index,
                sectionId=slide.sectionId,
                sectionTitle=section_titles.get(slide.sectionId, slide.sectionId),
            )
            for index, slide in enumerate(stored.slides, start=1)
        ]
        return HtmlGenerationCandidateView(
            id=stored.generationId,
            status="proposed",
            summary=stored.summary,
            generatedSlideCount=len(slides),
            sectionCount=len({slide.sectionId for slide in stored.slides}),
            visualsSummary=stored.visualsSummary,
            provenanceSummary=stored.provenanceSummary,
            warnings=list(stored.warnings),
            slides=slides,
            candidateHtmlUrl="/api/html/view/candidate/",
            actionToken=self._action_token(stored, marker),
        )

    def has_active_candidate(self) -> bool:
        return self._active_id() is not None

    def slides(self) -> SlidesResponse:
        candidate = self.candidate()
        return SlidesResponse(
            view="candidate",
            slides=[
                SlideItem(
                    id=slide.id,
                    title=slide.title,
                    number=slide.number,
                    sectionTitle=slide.sectionTitle,
                )
                for slide in candidate.slides
            ],
        )

    def resolve_candidate_resource(self, resource_path: str) -> tuple[Path, str]:
        import mimetypes

        candidate = self.candidate()
        source, _ = self._candidate_files(candidate.id)
        if not resource_path:
            path = source
        else:
            pure = PurePosixPath(resource_path)
            if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
                raise FileNotFoundError("Unsafe HTML Candidate resource path")
            path = source.parent.joinpath(*pure.parts).resolve()
            path.relative_to(source.parent.resolve())
        if not path.is_file():
            raise FileNotFoundError("HTML Candidate resource was not found")
        return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def _require_action_token(self, supplied: str, stored: StoredHtmlGeneration, marker: Path) -> None:
        if not hmac.compare_digest(self._action_token(stored, marker), supplied):
            raise WorkflowError("初期HTML Candidateが更新されています。最新の案を読み直してください。")

    def _require_current_stored_inputs(self, stored: StoredHtmlGeneration) -> dict[str, Any]:
        try:
            state = self._state()
            revisions = {
                _repo_path(self.repository, relative, field="HTML generation input"): revision
                for relative, revision in stored.inputRevisions.items()
            }
            valid = (
                self._state_is_supported(state)
                and hmac.compare_digest(planning_review_signature(self.repository, state), stored.basePlanningSignature)
                and hmac.compare_digest(self._context_signature(state), stored.baseContextSignature)
                and file_revision(self.repository / "deck.yaml") == stored.baseStateRevision
                and all(file_revision(path) == revision for path, revision in revisions.items())
            )
        except BaseException as exc:
            raise _StaleHtmlGenerationInputs(STALE_MESSAGE) from exc
        if not valid:
            raise _StaleHtmlGenerationInputs(STALE_MESSAGE)
        return state

    def apply(self, *, generation_id: str, action_token: str) -> HtmlGenerationStatusResponse:
        with self._lock:
            stored, marker = self._load_stored(generation_id)
            html_path, registry_path = self._validate_stored(stored, marker)
            self._require_action_token(action_token, stored, marker)
            state = self._require_current_stored_inputs(stored)
            applied = stored.model_copy(update={"status": "applied", "appliedAt": _utc_now()})
            browser_root = marker.parent / "browser"
            evidence_revisions = {
                (browser_root / "report.json").relative_to(self.repository).as_posix(): stored.browserReportRevision,
                (browser_root / "environment.json").relative_to(self.repository).as_posix(): stored.browserEnvironmentRevision,
                **{
                    self._preview_dependency_path(stored.generationId, relative).relative_to(self.repository).as_posix(): revision
                    for relative, revision in stored.candidatePreviewDependencyRevisions.items()
                },
                **{
                    (browser_root / "screenshots" / _screenshot_name(slide_id)).relative_to(self.repository).as_posix(): revision
                    for slide_id, revision in stored.browserScreenshotRevisions.items()
                },
            }
            self._apply_command(
                self.repository,
                state,
                candidate_html=html_path,
                candidate_registry=registry_path,
                expected_base_planning_signature=stored.basePlanningSignature,
                expected_state_revision=stored.baseStateRevision,
                expected_input_revisions=stored.inputRevisions,
                expected_candidate_html_revision=stored.candidateHtmlRevision,
                expected_candidate_registry_revision=stored.candidateRegistryRevision,
                expected_candidate_review_digest=stored.candidateReviewDigest,
                candidate_dependency_revisions=stored.candidateDependencyRevisions,
                expected_evidence_revisions=evidence_revisions,
                proposal_path=marker,
                expected_proposal_revision=file_revision(marker) or "missing",
                applied_proposal_payload=_json_payload(applied.model_dump()),
                proposal_digest=stored.candidateDigest,
            )
            self._status = HtmlGenerationStatusResponse(
                available=True,
                reason=None,
                allowedStage=False,
                status="succeeded",
                phase="ready",
                message="HTML案を採用し、HTML全体の確認へ進みました。",
            )
        return self.status()

    def cancel(self, *, generation_id: str, action_token: str) -> HtmlGenerationStatusResponse:
        with self._lock:
            stored, marker = self._load_stored(generation_id)
            self._validate_stored(stored, marker)
            self._require_action_token(action_token, stored, marker)
            cancelled = stored.model_copy(update={"status": "cancelled", "cancelledAt": _utc_now()})
            expected_revision = file_revision(marker)
            ArtifactTransactionStore(self.repository, (marker,)).commit(
                {marker: _json_payload(cancelled.model_dump())},
                operation="cancel-ai-initial-html-candidate",
                validate_base=lambda: self._require_marker_revision(marker, expected_revision),
            )
            self._status = HtmlGenerationStatusResponse(
                available=True,
                reason=None,
                allowedStage=self._state_is_supported_safe(),
                status="idle",
                message="HTML Candidateを破棄しました。canonical HTMLは変更していません。",
            )
        return self.status()

    @staticmethod
    def _require_marker_revision(marker: Path, expected_revision: str | None) -> None:
        if file_revision(marker) != expected_revision:
            raise WorkflowError("初期HTML Candidate metadataが更新されています")
