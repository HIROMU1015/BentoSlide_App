from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bento_converter.errors import BentoConverterError
from scripts.deck_workflow import WorkflowError, recover_repository_transactions, repository_root

from app.backend.api.routes import create_api_router
from app.backend.services.bento_service import BentoService
from app.backend.services.bento_lifecycle_service import BentoLifecycleService
from app.backend.services.ai_proposal_service import AiProposalService
from app.backend.services.conversion_service import ConversionService
from app.backend.services.html_review_service import HtmlReviewService
from app.backend.services.html_generation_service import HtmlGenerationService
from app.backend.services.planning_ai_proposal_service import PlanningAiProposalService
from app.backend.services.workflow_service import WorkflowService
from app.backend.services.storyboard_service import StoryboardService


def create_app(
    repository: str | Path | None = None,
    *,
    frontend_dist: str | Path | None = None,
    conversion_service: ConversionService | None = None,
    lifecycle_service: BentoLifecycleService | None = None,
    ai_service: AiProposalService | None = None,
    planning_ai_service: PlanningAiProposalService | None = None,
    html_generation_service: HtmlGenerationService | None = None,
    storyboard_service: StoryboardService | None = None,
) -> FastAPI:
    root = repository_root(repository or Path(__file__).resolve().parents[2])
    recover_repository_transactions(root)
    workflow = WorkflowService(root)
    html_review = HtmlReviewService(root, workflow)
    bento = BentoService(workflow)
    conversion = conversion_service or ConversionService(root)
    lifecycle = lifecycle_service or BentoLifecycleService(root)
    ai = ai_service or AiProposalService(root)
    planning_ai = planning_ai_service or PlanningAiProposalService(root)
    html_generation = html_generation_service or HtmlGenerationService(root)
    storyboard = storyboard_service or StoryboardService(root)

    application = FastAPI(title="BentoSlide Application API", version="0.1.0")
    application.state.repository = root
    application.state.workflow_service = workflow
    application.state.html_review_service = html_review
    application.state.bento_service = bento
    application.state.conversion_service = conversion
    application.state.lifecycle_service = lifecycle
    application.state.ai_service = ai
    application.state.planning_ai_service = planning_ai
    application.state.html_generation_service = html_generation
    application.state.storyboard_service = storyboard

    @application.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, exc: WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @application.exception_handler(BentoConverterError)
    async def converter_error(_request: Request, exc: BentoConverterError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @application.exception_handler(OSError)
    async def operating_system_error(_request: Request, exc: OSError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    application.include_router(create_api_router(
        repository=root,
        workflow=workflow,
        html_review=html_review,
        bento=bento,
        conversion=conversion,
        lifecycle=lifecycle,
        ai=ai,
        planning_ai=planning_ai,
        html_generation=html_generation,
        storyboard=storyboard,
    ))

    dist = Path(frontend_dist).resolve() if frontend_dist else root / "app/frontend/dist"
    if dist.is_dir() and (dist / "index.html").is_file():
        application.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @application.get("/", response_class=PlainTextResponse)
        def frontend_not_built() -> str:
            return "BentoSlide frontend is not built. Run npm install and npm run build in app/frontend."

    return application


app = create_app()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the localhost-only BentoSlide App backend")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=4180)
    result.add_argument("--stdout-log", type=Path)
    result.add_argument("--stderr-log", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("BentoSlide App must bind exactly to 127.0.0.1")
    if args.port < 1 or args.port > 65535:
        raise SystemExit(f"Invalid BentoSlide App port: {args.port}")
    with contextlib.ExitStack() as stack:
        if args.stdout_log is not None:
            args.stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout = stack.enter_context(args.stdout_log.open("a", encoding="utf-8", buffering=1))
            stack.enter_context(contextlib.redirect_stdout(stdout))
        if args.stderr_log is not None:
            args.stderr_log.parent.mkdir(parents=True, exist_ok=True)
            stderr = stack.enter_context(args.stderr_log.open("a", encoding="utf-8", buffering=1))
            stack.enter_context(contextlib.redirect_stderr(stderr))
        application = create_app(args.root)
        uvicorn.run(application, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
