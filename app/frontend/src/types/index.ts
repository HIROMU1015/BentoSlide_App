export type UiMode =
  | 'storyboard'
  | 'html-design'
  | 'converting'
  | 'bento-edit'
  | 'final-edit'
  | 'complete'
  | 'blocked'

export type ProjectResponse = {
  project: { title: string; kind: string }
}

export type AppState = {
  mode: UiMode
  stage: string
  statusLabel: string
  nextActionLabel: string
  htmlAvailable: boolean
  canConvert: boolean
  canEditBento: boolean
  hasCandidate: boolean
  isBlocked: boolean
  bentoEditorUrl: string | null
}

export type BentoIntegration = {
  available: boolean
  editorUrl: string | null
  message: string
}

export type ConversionPhase =
  | 'validating'
  | 'building'
  | 'validating-output'
  | 'starting-authoring'
  | 'complete'

export type ConversionStatus = {
  status: 'idle' | 'running' | 'succeeded' | 'failed'
  phase: ConversionPhase | null
  completedSteps: number
  totalSteps: 4
  message: string
  error: string | null
  retryable: boolean
}

export type LifecycleAction =
  | 'content-review'
  | 'content-approve'
  | 'final-approve'
  | 'final-reopen'
  | 'final-open'

export type LifecyclePhase =
  | 'stopping-editor'
  | 'validating-content'
  | 'approving-content'
  | 'initializing-final'
  | 'starting-editor'
  | 'approving-final'
  | 'completing'
  | 'reopening-final'
  | 'opening-final'
  | 'complete'

export type LifecycleStatus = {
  status: 'idle' | 'running' | 'succeeded' | 'failed'
  action: LifecycleAction | null
  phase: LifecyclePhase | null
  stage: string
  completedSteps: number
  totalSteps: number
  message: string
  error: string | null
  retryable: boolean
  availableActions: LifecycleAction[]
}

export type SlideItem = {
  id: string
  title: string
  number: number
  sectionTitle: string | null
}

export type StoryboardDocumentSection = {
  title: string
  paragraphs: string[]
  bullets: string[]
}

export type StoryboardDocument = {
  title: string
  sections: StoryboardDocumentSection[]
}

export type StoryboardVisual = {
  recommended: boolean
  type: string
  intent: string | null
  purpose: string | null
}

export type StoryboardSlide = {
  id: string
  number: number
  title: string
  points: string[]
  sectionId: string
  sectionTitle: string
  visual: StoryboardVisual | null
}

export type StoryboardSection = {
  id: string
  title: string
  slides: StoryboardSlide[]
}

export type Storyboard = {
  stage: string
  request: StoryboardDocument
  explanationPolicy: StoryboardDocument
  storyOutline: StoryboardDocument
  slidePlan: StoryboardDocument
  sections: StoryboardSection[]
  canInitialize: boolean
  canSubmit: boolean
  canApprove: boolean
  nextActionLabel: string
  actionToken: string
}

export type StoryboardAction = 'initialize' | 'submit' | 'approve'

export type ReviewSlide = {
  id: string
  title: string
  number: number | null
  impact: 'requested' | 'related' | 'changed' | 'added' | 'removed' | 'review'
}

export type HtmlProposal = {
  status: 'proposed' | 'approved' | 'applied'
  scope: 'local' | 'related' | 'structural-global'
  summary: string
  impactSummary: string
  affectedSlides: ReviewSlide[]
  postApplyReviewStatus: 'pending' | 'checked' | null
}

export type HtmlReview = {
  currentHtmlUrl: string
  candidateHtmlUrl: string | null
  fullPreviewUrl: string | null
  proposal: HtmlProposal | null
  actionToken: string
  canApply: boolean
  canApproveDeck: boolean
}

export type ReviewMark = 'pending' | 'reviewed' | 'needs-work'
export type ReviewMarks = Record<string, ReviewMark>
export type HtmlView = 'current' | 'candidate'

export type AiAction = 'shorten' | 'add-diagram' | 'improve-structure' | 'custom'
export type AiJobPhase =
  | 'preparing'
  | 'running-agent'
  | 'validating-candidate'
  | 'registering-proposal'
  | 'succeeded'
  | 'failed'

export type AiStatus = {
  available: boolean
  reason: string | null
  supportedActions: AiAction[]
  allowedStage: boolean
  status: 'idle' | 'running' | 'succeeded' | 'failed'
  phase: AiJobPhase | null
  message: string
  error: string | null
  retryable: boolean
}

export type AiProposalInput = {
  slideId: string
  action: AiAction
  instruction: string
}
