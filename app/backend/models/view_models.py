from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


UiMode = Literal[
    "storyboard", "html-design", "converting", "bento-edit", "final-edit", "complete", "blocked",
]
ReviewMark = Literal["pending", "reviewed", "needs-work"]
ConversionState = Literal["idle", "running", "succeeded", "failed"]
ConversionPhase = Literal[
    "validating", "building", "validating-output", "starting-authoring", "complete",
]
LifecycleAction = Literal[
    "content-review", "content-approve", "final-approve", "final-reopen", "final-open",
]
LifecyclePhase = Literal[
    "stopping-editor", "validating-content", "approving-content", "initializing-final",
    "starting-editor", "approving-final", "completing", "reopening-final", "opening-final",
    "complete",
]


class ProjectInfo(BaseModel):
    title: str
    kind: str


class ProjectResponse(BaseModel):
    project: ProjectInfo


class StateResponse(BaseModel):
    mode: UiMode
    stage: str
    statusLabel: str
    nextActionLabel: str
    canConvert: bool
    canEditBento: bool
    hasCandidate: bool
    isBlocked: bool
    bentoEditorUrl: str | None = None


class SlideItem(BaseModel):
    id: str
    title: str
    number: int
    sectionTitle: str | None = None


class SlidesResponse(BaseModel):
    view: Literal["current", "candidate"]
    slides: list[SlideItem]


class ReviewSlide(BaseModel):
    id: str
    title: str
    number: int | None = None
    impact: Literal["requested", "related", "changed", "added", "removed", "review"]


class HtmlProposal(BaseModel):
    status: Literal["proposed", "approved", "applied"]
    scope: Literal["local", "related", "structural-global"]
    summary: str
    impactSummary: str
    affectedSlides: list[ReviewSlide]
    postApplyReviewStatus: Literal["pending", "checked"] | None = None


class HtmlReviewResponse(BaseModel):
    currentHtmlUrl: str
    candidateHtmlUrl: str | None = None
    fullPreviewUrl: str | None = None
    proposal: HtmlProposal | None = None
    actionToken: str
    canApply: bool
    canApproveDeck: bool


class ApplyHtmlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actionToken: str = Field(min_length=20, max_length=256)
    confirmed: Literal[True]
    reviewedSlideIds: list[str] = Field(default_factory=list, max_length=500)


class ApproveHtmlDeckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actionToken: str = Field(min_length=20, max_length=256)
    confirmed: Literal[True]


class StartConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class ConversionStatusResponse(BaseModel):
    status: ConversionState
    phase: ConversionPhase | None = None
    completedSteps: int = Field(ge=0, le=4)
    totalSteps: Literal[4] = 4
    message: str
    error: str | None = None
    retryable: bool = False


class ConfirmedLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class LifecycleStatusResponse(BaseModel):
    status: ConversionState
    action: LifecycleAction | None = None
    phase: LifecyclePhase | None = None
    stage: str
    completedSteps: int = Field(ge=0, le=4)
    totalSteps: int = Field(ge=1, le=4)
    message: str
    error: str | None = None
    retryable: bool = False
    availableActions: list[LifecycleAction] = Field(default_factory=list)


class ActionResponse(BaseModel):
    status: str
    review: HtmlReviewResponse | None = None


class BentoIntegrationResponse(BaseModel):
    available: bool
    editorUrl: str | None = None
    message: str
