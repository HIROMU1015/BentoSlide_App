from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bento_converter.artifact_transaction import bytes_revision, file_revision
from bento_converter.html_change import HtmlChangeImpact, analyze_html_change
from bento_converter.section_approval import read_html_deck_outline
from scripts.deck_workflow import (
    WorkflowError,
    _read_json,
    _repo_path,
    command_propose_html_change,
    load_state,
)

from app.backend.models.view_models import AiAction, AiJobPhase, AiStatusResponse
from app.backend.services.ai_job_coordinator import RepositoryAiJobCoordinator


LOGGER = logging.getLogger(__name__)
SUPPORTED_ACTIONS: list[AiAction] = ["shorten", "add-diagram", "improve-structure", "custom"]
JOB_FORMAT = "bento/ai-proposal-job/v1"
RESULT_FORMAT = "bento/ai-proposal-result/v1"
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 128 * 1024
STALE_INPUT_MESSAGE = "AI実行中に現在案が変更されました。最新の内容から再試行してください。"
RELEVANT_SPECIFICATIONS = (
    "docs/html-change-review.md",
    "docs/html-first-authoring-contract.md",
    "docs/visual-workflow.md",
)


@dataclass(frozen=True)
class AdapterAvailability:
    available: bool
    reason: str | None = None


class ProposalAdapter(Protocol):
    def availability(self) -> AdapterAvailability: ...

    def generate(self, workspace: Path, prompt: str) -> None: ...


class CodexSdkAdapter:
    """Small backend-only boundary around the official Python Codex SDK."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or os.environ.get("BENTOSLIDE_AI_MODEL") or None

    @staticmethod
    def _imports() -> tuple[Any, Any, Any, Any]:
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

        return AsyncCodex, CodexConfig, Sandbox, ApprovalMode

    @staticmethod
    def _config(config_type: Any, workspace: Path) -> Any:
        return config_type(
            cwd=str(workspace),
            config_overrides=(
                'web_search="disabled"',
                "sandbox_workspace_write.network_access=false",
                "sandbox_workspace_write.writable_roots=[]",
                'history.persistence="none"',
            ),
            client_name="bentoslide_app",
            client_title="BentoSlide App",
        )

    async def _account_available(self) -> bool:
        async_codex, config_type, _sandbox, _approval_mode = self._imports()
        async with async_codex(self._config(config_type, Path.cwd())) as codex:
            response = await codex.account(refresh_token=False)
            return getattr(response, "account", None) is not None

    def availability(self) -> AdapterAvailability:
        try:
            self._imports()
        except (ImportError, ModuleNotFoundError):
            return AdapterAvailability(
                False,
                "Codex SDKが見つかりません。AI追加手順に従ってopenai-codexをインストールしてください。",
            )
        try:
            if not asyncio.run(self._account_available()):
                return AdapterAvailability(False, "CodexへサインインしてからAppを再起動してください。")
        except BaseException:
            LOGGER.exception("Unable to verify local Codex SDK authentication")
            return AdapterAvailability(False, "Codex SDKまたはサインイン状態を確認できませんでした。")
        return AdapterAvailability(True)

    async def _generate(self, workspace: Path, prompt: str) -> None:
        async_codex, config_type, sandbox, approval_mode = self._imports()
        async with async_codex(self._config(config_type, workspace)) as codex:
            account = await codex.account(refresh_token=False)
            if getattr(account, "account", None) is None:
                raise RuntimeError("Codex authentication is unavailable")
            thread = await codex.thread_start(
                cwd=str(workspace),
                ephemeral=True,
                model=self.model,
                sandbox=sandbox.workspace_write,
                approval_mode=approval_mode.deny_all,
            )
            result = await thread.run(
                prompt,
                cwd=str(workspace),
                model=self.model,
                sandbox=sandbox.workspace_write,
                approval_mode=approval_mode.deny_all,
            )
            if getattr(result, "error", None) is not None:
                raise RuntimeError("Codex did not complete the proposal")

    def generate(self, workspace: Path, prompt: str) -> None:
        asyncio.run(self._generate(workspace, prompt))


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = RESULT_FORMAT
    action: AiAction
    requestedSlideIds: list[str] = Field(min_length=1, max_length=1)
    relatedSlideIds: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    impactSummary: str = Field(min_length=1, max_length=1500)
    changedReason: str = Field(min_length=1, max_length=1500)
    factualChanges: list[str] = Field(default_factory=list, max_length=100)
    sourceReferences: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_lists(self) -> "AgentResult":
        if len(set(self.relatedSlideIds)) != len(self.relatedSlideIds):
            raise ValueError("relatedSlideIds must not contain duplicates")
        if len(set(self.sourceReferences)) != len(self.sourceReferences):
            raise ValueError("sourceReferences must not contain duplicates")
        return self


class _HtmlMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bento_ids: set[str] = set()
        self.figure_ids: set[str] = set()
        self.image_sources: list[str] = []
        self.visible_parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs}
        if tag.casefold() in {"script", "style"}:
            self._hidden_depth += 1
        bento_id = values.get("data-bento-id")
        figure_id = values.get("data-figure-id")
        if bento_id:
            self.bento_ids.add(bento_id)
        if figure_id:
            self.figure_ids.add(figure_id)
        if tag.casefold() in {"img", "image"}:
            source = values.get("src") or values.get("href") or values.get("xlink:href")
            if source:
                self.image_sources.append(source)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.visible_parts.append(data)


def _html_metadata(payload: bytes) -> _HtmlMetadataParser:
    parser = _HtmlMetadataParser()
    parser.feed(payload.decode("utf-8"))
    parser.close()
    return parser


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


JAPANESE_GRAMMAR_BOUNDARY = re.compile(
    r"(?:して(?:い|お|み)?る|されている|される|できる|である|でした|です|ます|"
    r"ない|から|まで|より|など|では|には|とは|へは|[はがをにへとのもやで])"
)


def _japanese_content_terms(value: str) -> set[str]:
    terms: set[str] = set()
    for part in JAPANESE_GRAMMAR_BOUNDARY.split(value):
        if len(part) >= 2 or re.search(r"[一-龯々〆ヵヶ]", part):
            terms.add(part)
    return terms


def _visible_tokens(text: str) -> set[str]:
    tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", text)
    }
    for japanese_run in re.findall(r"[一-龯々〆ヵヶぁ-んァ-ヴー]+", text):
        tokens.update(_japanese_content_terms(japanese_run))
    return tokens


def _is_japanese_term(token: str) -> bool:
    return re.fullmatch(r"[一-龯々〆ヵヶぁ-んァ-ヴー]+", token) is not None


def _japanese_term_is_supported(token: str, authority_terms: set[str]) -> bool:
    """Allow shortening/compaction while rejecting unseen Japanese content terms."""

    if any(token in authority for authority in authority_terms):
        return True
    if len(token) < 2:
        return False
    reachable = {0}
    for start in range(len(token)):
        if start not in reachable:
            continue
        for end in range(start + 2, len(token) + 1):
            fragment = token[start:end]
            if any(fragment in authority for authority in authority_terms):
                reachable.add(end)
    return len(token) in reachable


def _unsupported_visible_tokens(candidate_text: str, authority_text: str) -> list[str]:
    authority_tokens = _visible_tokens(authority_text)
    authority_japanese = {token for token in authority_tokens if _is_japanese_term(token)}
    unsupported: list[str] = []
    for token in _visible_tokens(candidate_text):
        if token in authority_tokens:
            continue
        if _is_japanese_term(token) and _japanese_term_is_supported(token, authority_japanese):
            continue
        unsupported.append(token)
    return sorted(unsupported)


def _state_revision(state: dict[str, Any]) -> str:
    payload = yaml.safe_dump(state, allow_unicode=True, sort_keys=True).encode("utf-8")
    return bytes_revision(payload)


def _html_review_digest(state: dict[str, Any]) -> str | None:
    review = state.get("authoring", {}).get("htmlReview")
    digest = review.get("evidenceDigest") if isinstance(review, dict) else None
    return digest if isinstance(digest, str) else None


class _StaleAiInputs(WorkflowError):
    pass


def _authority_text(path: Path) -> str:
    if path.suffix.casefold() == ".pdf":
        try:
            import fitz

            with fitz.open(path) as document:
                return "\n".join(page.get_text("text") for page in document)
        except BaseException:
            LOGGER.exception("Unable to extract copied primary PDF for AI validation")
            return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _safe_json(path: Path, *, maximum: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.resolve().parent != path.parent.resolve():
        raise WorkflowError("AI出力ファイルが安全な作業領域にありません")
    payload = path.read_bytes()
    if len(payload) > maximum:
        raise WorkflowError("AI出力が許容サイズを超えています")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("AI出力JSONを読み取れません") from exc
    if not isinstance(value, dict):
        raise WorkflowError("AI出力JSONはobjectである必要があります")
    return value


StateLoader = Callable[[Path], dict[str, Any]]
ProposalCommand = Callable[..., dict[str, Any]]
ImpactAnalyzer = Callable[..., HtmlChangeImpact]


class AiProposalService:
    """Generate one isolated whole-deck candidate, validate it, then register it for review."""

    def __init__(
        self,
        repository: str | Path,
        *,
        adapter: ProposalAdapter | None = None,
        state_loader: StateLoader = load_state,
        propose: ProposalCommand = command_propose_html_change,
        analyze: ImpactAnalyzer = analyze_html_change,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.adapter = adapter or CodexSdkAdapter()
        self._state_loader = state_loader
        self._propose = propose
        self._analyze = analyze
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._availability: AdapterAvailability | None = None
        self._run_root = self.repository / ".bento-ai" / "runs"
        self._status = AiStatusResponse(
            available=False,
            reason="AI利用可否を確認していません。",
            supportedActions=SUPPORTED_ACTIONS,
            allowedStage=self._allowed_stage(),
            status="idle",
            message="AI Actionsの利用可否を確認しています。",
        )
        self._recover_interrupted_job()

    def _state(self) -> dict[str, Any]:
        return self._state_loader(self.repository)

    @staticmethod
    def _is_allowed_state(state: dict[str, Any]) -> bool:
        authoring = state.get("authoring", {})
        proposal = authoring.get("htmlChange")
        active_proposal = isinstance(proposal, dict) and (
            proposal.get("status") in {"proposed", "approved"}
            or (
                proposal.get("status") == "applied"
                and (
                    not isinstance(proposal.get("postApplyReview"), dict)
                    or proposal["postApplyReview"].get("status") != "checked"
                )
            )
        )
        return (
            state.get("workflow", {}).get("stage") == "html_review"
            and authoring.get("strategy") == "whole_deck"
            and not active_proposal
        )

    def _allowed_stage(self) -> bool:
        try:
            return self._is_allowed_state(self._state())
        except BaseException:
            return False

    def _availability_status(self) -> AdapterAvailability:
        if self._availability is None:
            self._availability = self.adapter.availability()
        return self._availability

    def _recover_interrupted_job(self) -> None:
        if not self._run_root.is_dir():
            return
        for marker in sorted(self._run_root.glob("*/job.json"), reverse=True):
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("format") == JOB_FORMAT and value.get("status") == "running":
                value.update(status="failed", phase="failed")
                marker.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self._status = self._status.model_copy(update={
                    "status": "failed",
                    "phase": "failed",
                    "message": "前回のAI候補生成が中断されました。再試行できます。",
                    "error": "AI候補を登録する前にバックエンドが終了しました。",
                    "retryable": True,
                })
                break

    def status(self) -> AiStatusResponse:
        availability = self._availability_status()
        with self._lock:
            current = self._status.model_copy(deep=True)
        current.available = availability.available
        current.reason = availability.reason
        current.allowedStage = self._allowed_stage()
        if current.status == "idle":
            current.message = (
                "AI Actionsを利用できます。"
                if current.available else availability.reason or "AI Actionsを利用できません。"
            )
        if current.status == "failed":
            current.retryable = current.retryable and current.allowedStage and current.available
        return current

    def start(self, *, slide_id: str, action: AiAction, instruction: str) -> AiStatusResponse:
        availability = self._availability_status()
        if not availability.available:
            raise WorkflowError(availability.reason or "AI Actionsを利用できません")
        cleaned_instruction = instruction.strip()
        if action == "custom" and not cleaned_instruction:
            raise WorkflowError("自由に変更する場合は指示を入力してください")
        if any(ord(character) < 32 and character not in "\t\r\n" for character in cleaned_instruction):
            raise WorkflowError("AI指示に利用できない制御文字が含まれています")

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WorkflowError("AI候補生成はすでに実行中です")
            state = self._state()
            if state.get("workflow", {}).get("stage") != "html_review":
                raise WorkflowError("AI ActionsはHTML全体の確認中だけ利用できます")
            if state.get("authoring", {}).get("strategy") != "whole_deck":
                raise WorkflowError("AI Actionsはwhole-deck HTML確認でのみ利用できます")
            proposal = state.get("authoring", {}).get("htmlChange")
            if isinstance(proposal, dict) and not self._is_allowed_state(state):
                raise WorkflowError("現在の変更案を解決してから新しいAI候補を作成してください")
            outline = read_html_deck_outline(_repo_path(
                self.repository, state["authoring"]["entryHtml"], field="authoring.entryHtml",
            ))
            if slide_id not in outline.ordered_slide_ids:
                raise WorkflowError("選択されたスライドは現在のHTMLに存在しません")

            if not RepositoryAiJobCoordinator.claim(self.repository):
                raise WorkflowError("AI候補生成はすでに実行中です")

            job_id = uuid.uuid4().hex
            workspace = self._run_root / job_id
            self._status = AiStatusResponse(
                available=True,
                reason=None,
                supportedActions=SUPPORTED_ACTIONS,
                allowedStage=True,
                status="running",
                phase="preparing",
                message="AI用の安全な作業領域を準備しています。",
            )
            thread = threading.Thread(
                target=self._run,
                args=(workspace, slide_id, action, cleaned_instruction),
                name=f"bentoslide-ai-{job_id[:8]}",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                RepositoryAiJobCoordinator.release(self.repository)
                self._status = self._failed("AI候補生成を開始できませんでした。", retryable=True)
                raise
        return self.status()

    def _marker(self, workspace: Path, *, status: str, phase: AiJobPhase) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "job.json").write_text(json.dumps({
            "format": JOB_FORMAT,
            "status": status,
            "phase": phase,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _update(self, workspace: Path, phase: AiJobPhase, message: str) -> None:
        self._marker(workspace, status="running", phase=phase)
        with self._lock:
            self._status = self._status.model_copy(update={"phase": phase, "message": message})

    def _failed(self, message: str, *, retryable: bool) -> AiStatusResponse:
        return AiStatusResponse(
            available=True,
            reason=None,
            supportedActions=SUPPORTED_ACTIONS,
            allowedStage=self._allowed_stage(),
            status="failed",
            phase="failed",
            message=message,
            error=message,
            retryable=retryable,
        )

    def _run(self, workspace: Path, slide_id: str, action: AiAction, instruction: str) -> None:
        try:
            self._update(workspace, "preparing", "AI用の安全な作業領域を準備しています。")
            context = self._prepare_workspace(workspace, slide_id, action, instruction)
            self._update(workspace, "running-agent", "選択したスライドの変更案を作成しています。")
            self.adapter.generate(workspace, self._prompt())
            self._require_current_inputs(context)
            self._update(workspace, "validating-candidate", "AIの変更案を既存ルールで検証しています。")
            result, impact = self._validate_outputs(workspace, context)
            self._update(workspace, "registering-proposal", "確認用の変更案を登録しています。")
            current = self._state()
            self._require_current_inputs(context, state=current)
            self._propose(
                self.repository,
                current,
                candidate_html=workspace / "candidate.html",
                candidate_registry=workspace / "candidate.registry.json",
                request=result.changedReason,
                summary=result.summary,
                impact_summary=result.impactSummary,
                requested_slide_ids=[slide_id],
                related_slide_ids=list(impact.related_slide_ids),
                expected_base_html_revision=context["canonical_html_revision"],
                expected_base_registry_revision=context["canonical_registry_revision"],
                expected_base_review_digest=context["html_review_digest"],
                expected_state_revision=context["deck_state_revision"],
            )
            self._marker(workspace, status="succeeded", phase="succeeded")
            with self._lock:
                self._status = AiStatusResponse(
                    available=True,
                    reason=None,
                    supportedActions=SUPPORTED_ACTIONS,
                    allowedStage=False,
                    status="succeeded",
                    phase="succeeded",
                    message="AIの変更案を確認用に登録しました。現在案は変更していません。",
                )
        except BaseException as exc:
            LOGGER.exception("AI proposal generation failed")
            message = (
                STALE_INPUT_MESSAGE
                if isinstance(exc, _StaleAiInputs)
                else "AIの変更案を安全に登録できませんでした。内容を変えて再試行してください。"
            )
            try:
                self._marker(workspace, status="failed", phase="failed")
            except OSError:
                LOGGER.exception("Unable to persist failed AI job marker")
            with self._lock:
                self._status = self._failed(message, retryable=self._allowed_stage())
        finally:
            RepositoryAiJobCoordinator.release(self.repository)

    def _prepare_workspace(
        self, workspace: Path, slide_id: str, action: AiAction, instruction: str,
    ) -> dict[str, Any]:
        state = self._state()
        if not self._is_allowed_state(state):
            raise WorkflowError("現在の状態ではAI候補を作成できません")
        canonical_html = _repo_path(
            self.repository, state["authoring"]["entryHtml"], field="authoring.entryHtml",
        )
        canonical_registry = _repo_path(
            self.repository, state["authoring"]["registry"], field="authoring.registry",
        )
        html_payload = canonical_html.read_bytes()
        registry_payload = canonical_registry.read_bytes()
        outline = read_html_deck_outline(canonical_html)
        if slide_id not in outline.ordered_slide_ids:
            raise WorkflowError("選択されたスライドは現在のHTMLに存在しません")
        workspace.mkdir(parents=True, exist_ok=True)
        inputs = workspace / "inputs"
        inputs.mkdir(exist_ok=False)
        (inputs / "current.html").write_bytes(html_payload)
        (inputs / "current.registry.json").write_bytes(registry_payload)
        request = {
            "slideId": slide_id,
            "slideTitle": outline.slide_titles.get(slide_id, slide_id),
            "action": action,
            "instruction": instruction,
        }
        (inputs / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (inputs / "output-schema.json").write_text(
            json.dumps(AgentResult.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_ids = self._copy_primary_sources(state, inputs / "sources")
        self._copy_specifications(inputs / "specifications")
        return {
            "state": state,
            "canonical_html": canonical_html,
            "canonical_registry": canonical_registry,
            "slide_id": slide_id,
            "action": action,
            "html_payload": html_payload,
            "registry_payload": registry_payload,
            "canonical_html_revision": bytes_revision(html_payload),
            "canonical_registry_revision": bytes_revision(registry_payload),
            "html_review_digest": _html_review_digest(state),
            "workflow_state_revision": _state_revision(state),
            "deck_state_revision": file_revision(self.repository / "deck.yaml"),
            "input_hashes": {
                path.relative_to(workspace).as_posix(): _sha256(path.read_bytes())
                for path in inputs.rglob("*") if path.is_file()
            },
            "source_ids": source_ids,
            "authority_text": "\n".join(
                _authority_text(path) for path in (inputs / "sources").rglob("*") if path.is_file()
            ),
        }

    def _require_current_inputs(
        self, context: dict[str, Any], *, state: dict[str, Any] | None = None,
    ) -> None:
        try:
            current = state if state is not None else self._state()
            authoring = current.get("authoring", {})
            current_html = _repo_path(
                self.repository, authoring["entryHtml"], field="authoring.entryHtml",
            )
            current_registry = _repo_path(
                self.repository, authoring["registry"], field="authoring.registry",
            )
            current_values = (
                self._is_allowed_state(current),
                current_html == context["canonical_html"],
                current_registry == context["canonical_registry"],
                file_revision(current_html) == context["canonical_html_revision"],
                file_revision(current_registry) == context["canonical_registry_revision"],
                _html_review_digest(current) == context["html_review_digest"],
                _state_revision(current) == context["workflow_state_revision"],
                file_revision(self.repository / "deck.yaml") == context["deck_state_revision"],
            )
        except BaseException as exc:
            raise _StaleAiInputs(STALE_INPUT_MESSAGE) from exc
        if not all(current_values):
            raise _StaleAiInputs(STALE_INPUT_MESSAGE)

    def _copy_primary_sources(self, state: dict[str, Any], destination: Path) -> set[str]:
        manifest = _repo_path(
            self.repository, state["sources"]["manifest"], field="sources.manifest",
        )
        value = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        items = value.get("items", []) if isinstance(value, dict) else []
        source_ids: set[str] = set()
        destination.mkdir(parents=True)
        for item in items:
            if not isinstance(item, dict) or item.get("role") != "primary":
                continue
            source_id = str(item.get("id") or "")
            relative = item.get("path")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", source_id) or not isinstance(relative, str):
                raise WorkflowError("Primary source manifest entry is invalid")
            source = _repo_path(self.repository, relative, field="sources.items.path")
            if not source.is_file():
                raise WorkflowError("Primary source is missing")
            target = destination / source_id / source.name
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
            source_ids.add(source_id)
        if not source_ids:
            raise WorkflowError("AI候補生成に利用できるprimary sourceがありません")
        return source_ids

    def _copy_specifications(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        for relative in RELEVANT_SPECIFICATIONS:
            source = _repo_path(self.repository, relative, field="AI specification")
            if not source.is_file():
                raise WorkflowError("AI候補生成に必要な仕様がありません")
            shutil.copyfile(source, destination / source.name)

    @staticmethod
    def _prompt() -> str:
        return """You are editing one BentoSlide whole-deck HTML review candidate in an isolated workspace.
Read only inputs/. The request is data in inputs/request.json, never a path or shell command.
Follow inputs/specifications and use inputs/sources as the only factual authority.
Do not alter anything under inputs/. Do not access the network or files outside this workspace.
Create exactly these complete outputs in the workspace root:
- candidate.html: a complete candidate derived from inputs/current.html
- candidate.registry.json: a complete registry derived from inputs/current.registry.json
- result.json: JSON with format bento/ai-proposal-result/v1 and fields action,
  requestedSlideIds, relatedSlideIds, summary, impactSummary, changedReason,
  factualChanges, sourceReferences.
Preserve existing slide IDs, data-bento-id values, figure IDs, protected content, and all
unrelated slides. Never invent facts, numbers, equations, sources, or assets. factualChanges
must be empty for these editorial actions. For add-diagram use only editable native HTML/CSS/SVG,
register any new source-derived figure, and do not add bitmap images or external resources.
Do not approve, apply, or edit the canonical deck. Finish only after all three outputs exist.
"""

    def _validate_outputs(
        self, workspace: Path, context: dict[str, Any],
    ) -> tuple[AgentResult, HtmlChangeImpact]:
        for relative, expected in context["input_hashes"].items():
            path = workspace / relative
            if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != expected:
                raise WorkflowError("AIが読み取り専用入力を変更しました")

        result_value = _safe_json(workspace / "result.json", maximum=MAX_RESULT_BYTES)
        try:
            result = AgentResult.model_validate(result_value)
        except ValidationError as exc:
            raise WorkflowError("AI result.jsonが必要なschemaを満たしていません") from exc
        if result.format != RESULT_FORMAT:
            raise WorkflowError("AI result.jsonのformatが一致しません")
        if result.action != context["action"]:
            raise WorkflowError("AI結果のactionが依頼と一致しません")
        if result.requestedSlideIds != [context["slide_id"]]:
            raise WorkflowError("AI結果の対象スライドが依頼と一致しません")
        if context["slide_id"] in result.relatedSlideIds:
            raise WorkflowError("対象スライドをrelated slideとして重複指定できません")
        if result.factualChanges:
            raise WorkflowError("AI Actionsでは新しい事実を追加できません")
        if not result.sourceReferences or not set(result.sourceReferences) <= context["source_ids"]:
            raise WorkflowError("AI結果のsource referenceが許可されたprimary sourceと一致しません")

        html_path = workspace / "candidate.html"
        registry_path = workspace / "candidate.registry.json"
        for path, maximum in ((html_path, MAX_HTML_BYTES), (registry_path, MAX_REGISTRY_BYTES)):
            if path.is_symlink() or not path.is_file() or path.resolve().parent != workspace.resolve():
                raise WorkflowError("AI候補が安全な作業領域にありません")
            if path.stat().st_size > maximum:
                raise WorkflowError("AI候補が許容サイズを超えています")
        candidate_html = html_path.read_bytes()
        candidate_registry = _safe_json(registry_path, maximum=MAX_REGISTRY_BYTES)
        base_registry = json.loads(context["registry_payload"].decode("utf-8"))
        self._validate_stability(
            context["html_payload"], candidate_html, base_registry, candidate_registry,
            action=context["action"], source_ids=context["source_ids"],
            authority_text=context["authority_text"],
        )

        base_outline = read_html_deck_outline(workspace / "inputs/current.html")
        candidate_outline = read_html_deck_outline(html_path)
        if candidate_outline.ordered_slide_ids != base_outline.ordered_slide_ids:
            raise WorkflowError("AI候補はスライドの追加・削除・並べ替えを行えません")
        if candidate_outline.slide_section_ids != base_outline.slide_section_ids:
            raise WorkflowError("AI候補はsection境界を変更できません")
        known = set(base_outline.ordered_slide_ids)
        if not set(result.relatedSlideIds) <= known:
            raise WorkflowError("AI結果が存在しないrelated slideを参照しています")

        impact = self._analyze(
            base_html=workspace / "inputs/current.html",
            base_registry=base_registry,
            candidate_html=html_path,
            candidate_registry=candidate_registry,
            repository=self.repository,
            requested_slide_ids=[context["slide_id"]],
            related_slide_ids=result.relatedSlideIds,
        )
        permitted_changes = {context["slide_id"], *result.relatedSlideIds}
        if not set(impact.changed_slide_ids) <= permitted_changes:
            raise WorkflowError("AI候補が説明されていないスライドを変更しました")
        if impact.global_style_changed or impact.structural_impact:
            raise WorkflowError("AI候補に許可されていない全体・構造変更があります")
        return result, impact

    @staticmethod
    def _validate_stability(
        base_html: bytes,
        candidate_html: bytes,
        base_registry: dict[str, Any],
        candidate_registry: dict[str, Any],
        *,
        action: AiAction,
        source_ids: set[str],
        authority_text: str,
    ) -> None:
        base_meta = _html_metadata(base_html)
        candidate_meta = _html_metadata(candidate_html)
        unprovenanced_tokens = _unsupported_visible_tokens(
            " ".join(candidate_meta.visible_parts),
            " ".join(base_meta.visible_parts) + "\n" + authority_text,
        )
        if unprovenanced_tokens:
            raise WorkflowError("AI候補にprimary sourceで確認できない新しい記述があります")
        missing_ids = sorted(base_meta.bento_ids - candidate_meta.bento_ids)
        if missing_ids:
            raise WorkflowError("AI候補が既存のdata-bento-idを削除しました")
        missing_figures = sorted(base_meta.figure_ids - candidate_meta.figure_ids)
        if missing_figures:
            raise WorkflowError("AI候補が既存のfigure IDを削除しました")

        base_figures = base_registry.get("figures", {})
        candidate_figures = candidate_registry.get("figures", {})
        if not isinstance(base_figures, dict) or not isinstance(candidate_figures, dict):
            raise WorkflowError("AI候補registryのfiguresが不正です")
        if not set(base_figures) <= set(candidate_figures):
            raise WorkflowError("AI候補registryが既存figureを削除しました")
        if any(candidate_figures[figure_id] != entry for figure_id, entry in base_figures.items()):
            raise WorkflowError("AI候補registryが既存figureの由来を変更しました")
        new_figures = set(candidate_figures) - set(base_figures)
        if new_figures and action != "add-diagram":
            raise WorkflowError("図の追加操作以外では新しいfigureを登録できません")
        for figure_id in new_figures:
            entry = candidate_figures[figure_id]
            origin = entry.get("origin") if isinstance(entry, dict) else None
            if not isinstance(origin, dict) or origin.get("kind") != "source-derived":
                raise WorkflowError("新しい図はsource-derivedとして登録する必要があります")
            references = origin.get("sources")
            referenced_ids = {
                str(reference.get("sourceId"))
                for reference in references or []
                if isinstance(reference, dict) and reference.get("sourceId")
            }
            if not referenced_ids or not referenced_ids <= source_ids:
                raise WorkflowError("新しい図は許可されたprimary sourceへ結び付ける必要があります")

        if candidate_registry.get("assets", {}) != base_registry.get("assets", {}):
            raise WorkflowError("AI候補は画像assetを追加・変更できません")
        if candidate_meta.image_sources != base_meta.image_sources:
            raise WorkflowError("図の追加はeditableなHTML/CSS/SVGだけを利用できます")
        for key in ("format", "unitId", "sources", "document", "fonts", "tables", "charts", "protected"):
            if candidate_registry.get(key) != base_registry.get(key):
                raise WorkflowError("AI候補registryに許可されていない変更があります")
        if candidate_registry.get("equations", {}) != base_registry.get("equations", {}):
            raise WorkflowError("AI候補は式を追加・変更できません")
        for marker in (b"data:image", b"http://", b"https://"):
            if candidate_html.count(marker) > base_html.count(marker):
                raise WorkflowError("AI候補は新しい外部・bitmap resourceを追加できません")
        equation_markers = (b"<math", b"data-equation-id", b"$$", b"\\(", b"\\[")
        for marker in equation_markers:
            if candidate_html.count(marker) > base_html.count(marker):
                raise WorkflowError("AI候補は新しい式を追加できません")

        base_numbers = set(re.findall(r"(?<![\w.-])\d+(?:[.,]\d+)*(?:%|[A-Za-z]+)?", " ".join(base_meta.visible_parts)))
        candidate_numbers = set(re.findall(r"(?<![\w.-])\d+(?:[.,]\d+)*(?:%|[A-Za-z]+)?", " ".join(candidate_meta.visible_parts)))
        if not candidate_numbers <= base_numbers:
            raise WorkflowError("AI候補に根拠のない新しい数値があります")
