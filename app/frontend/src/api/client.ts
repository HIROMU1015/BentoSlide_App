import type {
  AppState,
  AiProposalInput,
  AiStatus,
  BentoIntegration,
  ConversionStatus,
  HtmlReview,
  HtmlGenerationStatus,
  HtmlView,
  LifecycleAction,
  LifecycleStatus,
  PlanningAiStatus,
  ProjectResponse,
  SlideItem,
  Storyboard,
  StoryboardAction,
} from '../types'

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: options?.body ? { 'Content-Type': 'application/json', ...options.headers } : options?.headers,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.error ?? payload.detail ?? `Request failed (${response.status})`)
  }
  return payload as T
}

export async function loadAppData(requestedView: HtmlView = 'current'): Promise<{
  project: ProjectResponse
  state: AppState
  slides: SlideItem[]
  review: HtmlReview | null
  storyboard?: Storyboard | null
  slideView: HtmlView
  bento: BentoIntegration
  htmlGeneration?: HtmlGenerationStatus | null
}> {
  const [project, state, bento] = await Promise.all([
    request<ProjectResponse>('/api/project'),
    request<AppState>('/api/state'),
    request<BentoIntegration>('/api/bento'),
  ])
  const storyboard = state.mode === 'storyboard' ? await getStoryboard('current') : null
  const review = state.mode === 'html-design' && state.htmlAvailable
    ? await request<HtmlReview>('/api/html/review')
    : null
  const htmlGeneration = state.mode === 'html-design' && state.stage === 'html_authoring' && !state.htmlAvailable
    ? await getHtmlGenerationStatus()
    : null
  const initialCandidate = htmlGeneration?.candidate ?? null
  const slideView = requestedView === 'candidate' && (review?.candidateHtmlUrl || initialCandidate?.candidateHtmlUrl)
    ? 'candidate'
    : initialCandidate?.candidateHtmlUrl
      ? 'candidate'
      : 'current'
  const storyboardSlides: SlideItem[] = storyboard?.sections.flatMap((section) => section.slides.map((slide) => ({
    id: slide.id,
    title: slide.title,
    number: slide.number,
    sectionTitle: section.title,
  }))) ?? []
  const slides = storyboard
    ? storyboardSlides
      : state.htmlAvailable || initialCandidate
      ? (await request<{ slides: SlideItem[] }>(`/api/slides?view=${slideView}`)).slides
      : []
  return { project, state, slides, review, storyboard, slideView, bento, htmlGeneration }
}

export function getStoryboard(view: 'current' | 'candidate' = 'current') {
  return request<Storyboard>(view === 'current' ? '/api/storyboard' : '/api/storyboard?view=candidate')
}

const storyboardEndpoints: Record<StoryboardAction, string> = {
  initialize: '/api/storyboard/initialize',
  submit: '/api/storyboard/submit',
  approve: '/api/storyboard/approve',
}

export function startStoryboardAction(action: StoryboardAction, storyboard: Storyboard) {
  return request<Storyboard>(storyboardEndpoints[action], {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, actionToken: storyboard.actionToken }),
  })
}

export function getConversionStatus() {
  return request<ConversionStatus>('/api/convert/status')
}

export function startConversion() {
  return request<ConversionStatus>('/api/convert', {
    method: 'POST',
    body: JSON.stringify({ confirmed: true }),
  })
}

export function getLifecycleStatus() {
  return request<LifecycleStatus>('/api/bento/lifecycle/status')
}

const lifecycleEndpoints: Record<LifecycleAction, string> = {
  'content-review': '/api/bento/content/review',
  'content-approve': '/api/bento/content/approve',
  'final-approve': '/api/bento/final/approve',
  'final-reopen': '/api/bento/final/reopen',
  'final-open': '/api/bento/final/open',
}

export function startLifecycleAction(action: LifecycleAction) {
  return request<LifecycleStatus>(lifecycleEndpoints[action], {
    method: 'POST',
    body: JSON.stringify({ confirmed: true }),
  })
}

export function getAiStatus() {
  return request<AiStatus>('/api/ai/status')
}

export function startAiProposal(input: AiProposalInput) {
  return request<AiStatus>('/api/ai/proposals', {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, ...input }),
  })
}

export function getPlanningAiStatus() {
  return request<PlanningAiStatus>('/api/ai/planning/status')
}

export function startPlanningAiProposal(instruction: string) {
  return request<PlanningAiStatus>('/api/ai/planning/proposals', {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, instruction }),
  })
}

export function applyPlanningAiProposal(storyboard: Storyboard) {
  if (!storyboard.proposal) throw new Error('Planning Proposalがありません')
  return request<Storyboard>(`/api/ai/planning/proposals/${storyboard.proposal.id}/apply`, {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, actionToken: storyboard.proposal.actionToken }),
  })
}

export function cancelPlanningAiProposal(storyboard: Storyboard) {
  if (!storyboard.proposal) throw new Error('Planning Proposalがありません')
  return request<Storyboard>(`/api/ai/planning/proposals/${storyboard.proposal.id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, actionToken: storyboard.proposal.actionToken }),
  })
}

export function getHtmlGenerationStatus() {
  return request<HtmlGenerationStatus>('/api/ai/html-generation/status')
}

export function startHtmlGeneration(instruction = '') {
  return request<HtmlGenerationStatus>('/api/ai/html-generation', {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, instruction }),
  })
}

export function applyHtmlGeneration(status: HtmlGenerationStatus) {
  if (!status.candidate) throw new Error('確認できるHTML Candidateがありません')
  return request<HtmlGenerationStatus>(`/api/ai/html-generation/${status.candidate.id}/apply`, {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, actionToken: status.candidate.actionToken }),
  })
}

export function cancelHtmlGeneration(status: HtmlGenerationStatus) {
  if (!status.candidate) throw new Error('確認できるHTML Candidateがありません')
  return request<HtmlGenerationStatus>(`/api/ai/html-generation/${status.candidate.id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ confirmed: true, actionToken: status.candidate.actionToken }),
  })
}

export function applyHtmlChange(review: HtmlReview, reviewedSlideIds: string[]) {
  return request('/api/html/review/apply', {
    method: 'POST',
    body: JSON.stringify({
      actionToken: review.actionToken,
      confirmed: true,
      reviewedSlideIds,
    }),
  })
}

export function approveHtmlDeck(review: HtmlReview) {
  return request('/api/html/review/approve-deck', {
    method: 'POST',
    body: JSON.stringify({ actionToken: review.actionToken, confirmed: true }),
  })
}
