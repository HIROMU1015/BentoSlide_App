from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.backend.models.view_models import (
    ActionResponse,
    ApplyHtmlRequest,
    ApproveHtmlDeckRequest,
    BentoIntegrationResponse,
    HtmlReviewResponse,
    ProjectResponse,
    SlidesResponse,
    StateResponse,
)
from app.backend.services.bento_service import BentoService
from app.backend.services.html_review_service import HtmlReviewService
from app.backend.services.workflow_service import WorkflowService


def create_api_router(
    *, repository: Path, workflow: WorkflowService, html_review: HtmlReviewService, bento: BentoService,
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

    @router.get("/slides", response_model=SlidesResponse)
    def slides(view: Literal["current", "candidate"] = "current") -> SlidesResponse:
        try:
            return workflow.slides(view=view)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc

    @router.get("/html/review", response_model=HtmlReviewResponse)
    def review() -> HtmlReviewResponse:
        return html_review.review()

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
