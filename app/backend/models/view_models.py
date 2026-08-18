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
AiAction = Literal["shorten", "add-diagram", "improve-structure", "custom"]
AiJobPhase = Literal[
    "preparing", "running-agent", "validating-candidate", "registering-proposal",
    "succeeded", "failed",
]
PlanningAiJobPhase = Literal[
    "preparing", "running-agent", "validating-candidate", "registering-proposal",
    "succeeded", "failed",
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
    htmlAvailable: bool
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


class AiProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    slideId: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    action: AiAction
    instruction: str = Field(default="", max_length=2000)


class AiStatusResponse(BaseModel):
    available: bool
    reason: str | None = None
    supportedActions: list[AiAction]
    allowedStage: bool
    status: ConversionState
    phase: AiJobPhase | None = None
    message: str
    error: str | None = None
    retryable: bool = False


class PlanningAiProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    instruction: str = Field(min_length=1, max_length=2000)


class PlanningAiStatusResponse(BaseModel):
    available: bool
    reason: str | None = None
    allowedStage: bool
    status: ConversionState
    phase: PlanningAiJobPhase | None = None
    message: str
    error: str | None = None
    retryable: bool = False
    hasProposal: bool = False
    proposalId: str | None = None


class PlanningSlideImpact(BaseModel):
    id: str
    title: str
    change: Literal["changed", "added", "removed", "moved"]
    previousNumber: int | None = None
    number: int | None = None


class PlanningSectionImpact(BaseModel):
    id: str
    title: str
    change: Literal["changed", "added", "removed"]


class PlanningImpact(BaseModel):
    slides: list[PlanningSlideImpact] = Field(default_factory=list)
    sections: list[PlanningSectionImpact] = Field(default_factory=list)
    explanationPolicyChanged: bool
    storyOutlineChanged: bool
    slidePlanChanged: bool
    visualChanges: int = Field(ge=0)


class PlanningProposalView(BaseModel):
    id: str
    status: Literal["proposed"]
    summary: str
    impactSummary: str
    impact: PlanningImpact
    actionToken: str = Field(min_length=20, max_length=256)


class PlanningProposalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    actionToken: str = Field(min_length=20, max_length=256)


class StoryboardDocumentSection(BaseModel):
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)


class StoryboardDocument(BaseModel):
    title: str
    sections: list[StoryboardDocumentSection] = Field(default_factory=list)


class StoryboardVisual(BaseModel):
    recommended: bool
    type: str
    intent: str | None = None
    purpose: str | None = None


class StoryboardSlide(BaseModel):
    id: str
    number: int
    title: str
    points: list[str] = Field(default_factory=list)
    sectionId: str
    sectionTitle: str
    visual: StoryboardVisual | None = None


class StoryboardSection(BaseModel):
    id: str
    title: str
    slides: list[StoryboardSlide] = Field(default_factory=list)


class StoryboardResponse(BaseModel):
    view: Literal["current", "candidate"] = "current"
    proposal: PlanningProposalView | None = None
    stage: str
    request: StoryboardDocument
    explanationPolicy: StoryboardDocument
    storyOutline: StoryboardDocument
    slidePlan: StoryboardDocument
    sections: list[StoryboardSection] = Field(default_factory=list)
    canInitialize: bool
    canSubmit: bool
    canApprove: bool
    nextActionLabel: str
    actionToken: str = Field(min_length=20, max_length=256)


class StoryboardActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actionToken: str = Field(min_length=20, max_length=256)
    confirmed: Literal[True]


class ActionResponse(BaseModel):
    status: str
    review: HtmlReviewResponse | None = None


class BentoIntegrationResponse(BaseModel):
    available: bool
    editorUrl: str | None = None
    message: str
