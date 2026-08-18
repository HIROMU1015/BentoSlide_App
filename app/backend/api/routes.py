from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.backend.models.view_models import (
    ActionResponse,
    AiProposalRequest,
    AiStatusResponse,
    ApplyHtmlRequest,
    ApproveHtmlDeckRequest,
    BentoIntegrationResponse,
    ConfirmedLifecycleRequest,
    ConversionStatusResponse,
    HtmlReviewResponse,
    LifecycleStatusResponse,
    PlanningAiProposalRequest,
    PlanningAiStatusResponse,
    PlanningProposalActionRequest,
    PlanningProposalView,
    ProjectResponse,
    SlidesResponse,
    StateResponse,
    StartConversionRequest,
    StoryboardActionRequest,
    StoryboardResponse,
)
from app.backend.services.bento_service import BentoService
from app.backend.services.ai_proposal_service import AiProposalService
from app.backend.services.bento_lifecycle_service import BentoLifecycleService
from app.backend.services.conversion_service import ConversionService
from app.backend.services.html_review_service import HtmlReviewService
from app.backend.services.planning_ai_proposal_service import PlanningAiProposalService
from app.backend.services.workflow_service import WorkflowService
from app.backend.services.storyboard_service import StoryboardService


def create_api_router(
    *, repository: Path, workflow: WorkflowService, html_review: HtmlReviewService, bento: BentoService,
    conversion: ConversionService,
    lifecycle: BentoLifecycleService,
    ai: AiProposalService,
    planning_ai: PlanningAiProposalService,
    storyboard: StoryboardService,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, str]:
        return {
            "format": "bento/application-api-health/v1",
            "repository": str(repository),
            "status": "ok",
        }

    @router.get("/project", response_model=ProjectResponse)
    def project() -> ProjectResponse:
        return workflow.project()

    @router.get("/state", response_model=StateResponse)
    def state() -> StateResponse:
        return workflow.state_view()

    @router.get("/storyboard", response_model=StoryboardResponse)
    def storyboard_view(view: Literal["current", "candidate"] = "current") -> StoryboardResponse:
        if view == "candidate":
            candidate, proposal = planning_ai.candidate()
            return storyboard.view(view=view, candidate=candidate, proposal=proposal)
        return storyboard.view(view=view, proposal=planning_ai.active_proposal())

    @router.post("/storyboard/initialize", response_model=StoryboardResponse)
    def initialize_storyboard(request: StoryboardActionRequest) -> StoryboardResponse:
        return storyboard.initialize(action_token=request.actionToken)

    @router.post("/storyboard/submit", response_model=StoryboardResponse)
    def submit_storyboard(request: StoryboardActionRequest) -> StoryboardResponse:
        if planning_ai.has_active_proposal():
            raise WorkflowError("Planning Candidateを反映または破棄してから構成案を提出してください。")
        return storyboard.submit(action_token=request.actionToken)

    @router.post("/storyboard/approve", response_model=StoryboardResponse)
    def approve_storyboard(request: StoryboardActionRequest) -> StoryboardResponse:
        return storyboard.approve(action_token=request.actionToken)

    @router.get("/slides", response_model=SlidesResponse)
    def slides(view: Literal["current", "candidate"] = "current") -> SlidesResponse:
        try:
            return workflow.slides(view=view)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc

    @router.get("/html/review", response_model=HtmlReviewResponse)
    def review() -> HtmlReviewResponse:
        try:
            return html_review.review()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="HTML preview is not available yet") from exc

    @router.post("/html/review/apply", response_model=ActionResponse)
    def apply_change(request: ApplyHtmlRequest) -> ActionResponse:
        return html_review.apply_and_check(
            action_token=request.actionToken,
            reviewed_slide_ids=request.reviewedSlideIds,
        )

    @router.post("/html/review/approve-deck", response_model=ActionResponse)
    def approve_deck(request: ApproveHtmlDeckRequest) -> ActionResponse:
        return html_review.approve_deck(action_token=request.actionToken)

    @router.get("/bento", response_model=BentoIntegrationResponse)
    def bento_integration() -> BentoIntegrationResponse:
        return bento.integration()

    @router.post("/convert", response_model=ConversionStatusResponse, status_code=HTTPStatus.ACCEPTED)
    def start_conversion(_request: StartConversionRequest) -> ConversionStatusResponse:
        return conversion.start()

    @router.get("/convert/status", response_model=ConversionStatusResponse)
    def conversion_status() -> ConversionStatusResponse:
        return conversion.status()

    @router.get("/bento/lifecycle/status", response_model=LifecycleStatusResponse)
    def lifecycle_status() -> LifecycleStatusResponse:
        return lifecycle.status()

    @router.get("/ai/status", response_model=AiStatusResponse)
    def ai_status() -> AiStatusResponse:
        return ai.status()

    @router.post("/ai/proposals", response_model=AiStatusResponse, status_code=HTTPStatus.ACCEPTED)
    def create_ai_proposal(request: AiProposalRequest) -> AiStatusResponse:
        return ai.start(
            slide_id=request.slideId,
            action=request.action,
            instruction=request.instruction,
        )

    @router.get("/ai/planning/status", response_model=PlanningAiStatusResponse)
    def planning_ai_status() -> PlanningAiStatusResponse:
        return planning_ai.status()

    @router.post(
        "/ai/planning/proposals",
        response_model=PlanningAiStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def create_planning_ai_proposal(request: PlanningAiProposalRequest) -> PlanningAiStatusResponse:
        return planning_ai.start(instruction=request.instruction)

    @router.get(
        "/ai/planning/proposals/{proposal_id}", response_model=PlanningProposalView,
    )
    def planning_ai_proposal(proposal_id: str) -> PlanningProposalView:
        return planning_ai.proposal(proposal_id)

    @router.post(
        "/ai/planning/proposals/{proposal_id}/apply", response_model=StoryboardResponse,
    )
    def apply_planning_ai_proposal(
        proposal_id: str, request: PlanningProposalActionRequest,
    ) -> StoryboardResponse:
        planning_ai.apply(proposal_id=proposal_id, action_token=request.actionToken)
        return storyboard.view()

    @router.post(
        "/ai/planning/proposals/{proposal_id}/cancel", response_model=StoryboardResponse,
    )
    def cancel_planning_ai_proposal(
        proposal_id: str, request: PlanningProposalActionRequest,
    ) -> StoryboardResponse:
        planning_ai.cancel(proposal_id=proposal_id, action_token=request.actionToken)
        return storyboard.view()

    @router.post(
        "/bento/content/review", response_model=LifecycleStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def begin_content_review(_request: ConfirmedLifecycleRequest) -> LifecycleStatusResponse:
        return lifecycle.start("content-review")

    @router.post(
        "/bento/content/approve", response_model=LifecycleStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def approve_content(_request: ConfirmedLifecycleRequest) -> LifecycleStatusResponse:
        return lifecycle.start("content-approve")

    @router.post(
        "/bento/final/approve", response_model=LifecycleStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def approve_final(_request: ConfirmedLifecycleRequest) -> LifecycleStatusResponse:
        return lifecycle.start("final-approve")

    @router.post(
        "/bento/final/reopen", response_model=LifecycleStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def reopen_final(_request: ConfirmedLifecycleRequest) -> LifecycleStatusResponse:
        return lifecycle.start("final-reopen")

    @router.post(
        "/bento/final/open", response_model=LifecycleStatusResponse,
        status_code=HTTPStatus.ACCEPTED,
    )
    def open_final(_request: ConfirmedLifecycleRequest) -> LifecycleStatusResponse:
        return lifecycle.start("final-open")

    safe_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'self' data: blob:; script-src 'none'; connect-src 'none'; "
            "style-src 'self' 'unsafe-inline' data:; img-src 'self' data: blob:; "
            "font-src 'self' data:; media-src 'self' data: blob:; frame-ancestors 'self'"
        ),
    }

    def html_file(view: str, resource_path: str) -> FileResponse:
        try:
            path, media_type = html_review.resolve_view_resource(view, resource_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="HTML view resource was not found") from exc
        return FileResponse(path, media_type=media_type, headers=safe_headers)

    @router.get("/html/view/{view}/")
    def html_view(view: Literal["current", "candidate"]) -> FileResponse:
        return html_file(view, "")

    @router.get("/html/view/{view}/{resource_path:path}")
    def html_resource(view: Literal["current", "candidate"], resource_path: str) -> FileResponse:
        return html_file(view, resource_path)

    return router
