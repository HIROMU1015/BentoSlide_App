from __future__ import annotations

import hmac
import mimetypes
import secrets
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from bento_converter.section_approval import HtmlDeckOutline, read_html_deck_outline
from scripts.deck_workflow import (
    WorkflowError,
    command_apply_html_change,
    command_approve_html_change,
    command_approve_html_deck,
    command_check_html_change,
    load_state,
)

from app.backend.models.view_models import (
    ActionResponse,
    HtmlProposal,
    HtmlReviewResponse,
    ReviewSlide,
)
from app.backend.services.workflow_service import WorkflowService, verified_local_session_url


ACTIVE_CANDIDATE_STATUSES = {"proposed", "approved"}
VISIBLE_PROPOSAL_STATUSES = {"proposed", "approved", "applied"}


def _proposal(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("authoring", {}).get("htmlChange")
    if isinstance(value, dict) and value.get("status") in VISIBLE_PROPOSAL_STATUSES:
        return value
    return None


def _signature(state: dict[str, Any]) -> tuple[str, ...]:
    proposal = _proposal(state)
    if proposal:
        post_apply = proposal.get("postApplyReview") if isinstance(proposal.get("postApplyReview"), dict) else {}
        return (
            str(state["workflow"]["stage"]),
            str(proposal.get("proposalId") or ""),
            str(proposal.get("proposalDigest") or ""),
            str(proposal.get("status") or ""),
            str(post_apply.get("status") or ""),
        )
    review = state.get("authoring", {}).get("htmlReview")
    if not isinstance(review, dict):
        review = {}
    return (
        str(state["workflow"]["stage"]),
        str(review.get("htmlRevision") or ""),
        str(review.get("registryRevision") or ""),
        "no-proposal",
    )


def _can_approve_deck(state: dict[str, Any], proposal: dict[str, Any] | None) -> bool:
    if state["workflow"]["stage"] != "html_review":
        return False
    if proposal is None:
        return True
    if proposal.get("status") != "applied":
        return False
    post_apply = proposal.get("postApplyReview")
    return isinstance(post_apply, dict) and post_apply.get("status") == "checked"


def _outline_positions(outline: HtmlDeckOutline) -> dict[str, int]:
    return {slide_id: index for index, slide_id in enumerate(outline.ordered_slide_ids, start=1)}


class HtmlReviewService:
    """Expose review data while keeping revisions and proposal digests server-side."""

    def __init__(self, repository: str | Path, workflow: WorkflowService):
        self.repository = Path(repository).resolve()
        self.workflow = workflow
        self._action_lock = threading.Lock()
        self._token = ""
        self._token_signature: tuple[str, ...] | None = None

    def _action_token(self, state: dict[str, Any]) -> str:
        signature = _signature(state)
        if self._token_signature != signature:
            self._token_signature = signature
            self._token = secrets.token_urlsafe(32)
        return self._token

    def _require_token(self, state: dict[str, Any], supplied: str) -> None:
        expected = self._action_token(state)
        if not hmac.compare_digest(expected, supplied):
            raise WorkflowError("The review action is stale. Reload the latest proposal before continuing")

    def _candidate_path(self, state: dict[str, Any]) -> Path | None:
        proposal = _proposal(state)
        if not proposal or proposal.get("status") not in ACTIVE_CANDIDATE_STATUSES:
            return None
        relative = proposal.get("candidateHtml")
        if not isinstance(relative, str) or not relative:
            return None
        path = (self.repository / relative).resolve()
        path.relative_to(self.repository)
        return path if path.is_file() else None

    def _review_slides(
        self,
        proposal: dict[str, Any],
        current_outline: HtmlDeckOutline,
        candidate_outline: HtmlDeckOutline | None,
    ) -> list[ReviewSlide]:
        current_positions = _outline_positions(current_outline)
        candidate_positions = _outline_positions(candidate_outline) if candidate_outline else {}
        titles = dict(current_outline.slide_titles)
        if candidate_outline:
            titles.update(candidate_outline.slide_titles)
        titles.update({str(key): str(value) for key, value in (proposal.get("slideTitles") or {}).items()})
        requested = set(proposal.get("requestedSlideIds") or [])
        related = set(proposal.get("relatedSlideIds") or [])
        changed = set(proposal.get("changedSlideIds") or [])
        added = set(proposal.get("addedSlideIds") or [])
        removed = set(proposal.get("removedSlideIds") or [])
        result: list[ReviewSlide] = []
        for slide_id in proposal.get("affectedSlideIds") or []:
            if slide_id in added:
                impact = "added"
            elif slide_id in removed:
                impact = "removed"
            elif slide_id in changed:
                impact = "changed"
            elif slide_id in related:
                impact = "related"
            elif slide_id in requested:
                impact = "requested"
            else:
                impact = "review"
            result.append(ReviewSlide(
                id=slide_id,
                title=titles.get(slide_id, slide_id),
                number=candidate_positions.get(slide_id) or current_positions.get(slide_id),
                impact=impact,
            ))
        return result

    def review(self) -> HtmlReviewResponse:
        state = self.workflow.state()
        current_path = self.workflow.html_source(state)
        current_outline = read_html_deck_outline(current_path)
        proposal = _proposal(state)
        candidate_path = self._candidate_path(state)
        candidate_outline = read_html_deck_outline(candidate_path) if candidate_path else None
        proposal_view = None
        if proposal:
            scope = "structural-global" if proposal.get("scope") == "global" else str(proposal.get("scope") or "local")
            post_apply = proposal.get("postApplyReview") if isinstance(proposal.get("postApplyReview"), dict) else None
            proposal_view = HtmlProposal(
                status=str(proposal["status"]),
                scope=scope,
                summary=str(proposal.get("summary") or ""),
                impactSummary=str(proposal.get("impactSummary") or ""),
                affectedSlides=self._review_slides(proposal, current_outline, candidate_outline),
                postApplyReviewStatus=post_apply.get("status") if post_apply else None,
            )
        preview_port = int(state.get("preview", {}).get("htmlPort") or 4173)
        preview_url = verified_local_session_url(
            self.repository,
            filename="html-preview-session.json",
            expected_format="bento/html-preview-session/v1",
            expected_port=preview_port,
        )
        return HtmlReviewResponse(
            currentHtmlUrl="/api/html/view/current/",
            candidateHtmlUrl="/api/html/view/candidate/" if candidate_path else None,
            fullPreviewUrl=preview_url,
            proposal=proposal_view,
            actionToken=self._action_token(state),
            canApply=bool(proposal and proposal.get("status") in ACTIVE_CANDIDATE_STATUSES),
            canApproveDeck=_can_approve_deck(state, proposal),
        )

    def apply_and_check(self, *, action_token: str, reviewed_slide_ids: list[str]) -> ActionResponse:
        with self._action_lock:
            state = load_state(self.repository)
            self._require_token(state, action_token)
            proposal = _proposal(state)
            if not proposal:
                raise WorkflowError("There is no HTML change proposal to apply")
            if proposal.get("status") in ACTIVE_CANDIDATE_STATUSES:
                affected = list(proposal.get("affectedSlideIds") or [])
                if reviewed_slide_ids != affected or len(set(reviewed_slide_ids)) != len(reviewed_slide_ids):
                    raise WorkflowError("Every affected slide must be marked reviewed before applying the proposal")
            if proposal.get("status") == "proposed":
                command_approve_html_change(self.repository, state)
                state = load_state(self.repository)
                proposal = _proposal(state)
            if proposal and proposal.get("status") == "approved":
                command_apply_html_change(self.repository, state)
                state = load_state(self.repository)
                proposal = _proposal(state)
            if not proposal or proposal.get("status") != "applied":
                raise WorkflowError("The HTML change proposal did not reach the applied state")
            post_apply = proposal.get("postApplyReview")
            if isinstance(post_apply, dict) and post_apply.get("status") == "pending":
                command_check_html_change(self.repository, state, browser_executable=None)
            return ActionResponse(status="checked", review=self.review())

    def approve_deck(self, *, action_token: str) -> ActionResponse:
        with self._action_lock:
            state = load_state(self.repository)
            if state["workflow"]["stage"] == "ready_for_conversion":
                return ActionResponse(status="already-approved", review=None)
            self._require_token(state, action_token)
            command_approve_html_deck(self.repository, state)
            return ActionResponse(status="approved", review=None)

    def resolve_view_resource(self, view: str, resource_path: str) -> tuple[Path, str]:
        state = self.workflow.state()
        if view == "current":
            source = self.workflow.html_source(state)
        elif view == "candidate":
            source = self._candidate_path(state)
            if source is None:
                raise FileNotFoundError("There is no active candidate HTML")
        else:
            raise FileNotFoundError("Unknown HTML view")
        if not resource_path:
            path = source
        else:
            pure = PurePosixPath(resource_path)
            if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
                raise FileNotFoundError("Unsafe HTML resource path")
            path = source.parent.joinpath(*pure.parts).resolve()
            path.relative_to(source.parent.resolve())
        if not path.is_file():
            raise FileNotFoundError(f"HTML resource does not exist: {resource_path}")
        return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
