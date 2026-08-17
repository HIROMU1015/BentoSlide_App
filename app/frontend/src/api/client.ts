import type { AppState, HtmlReview, ProjectResponse, SlideItem } from '../types'

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

export async function loadAppData(): Promise<{
  project: ProjectResponse
  state: AppState
  slides: SlideItem[]
  review: HtmlReview | null
}> {
  const [project, state, slidesResponse] = await Promise.all([
    request<ProjectResponse>('/api/project'),
    request<AppState>('/api/state'),
    request<{ slides: SlideItem[] }>('/api/slides'),
  ])
  const review = state.mode === 'html-design' ? await request<HtmlReview>('/api/html/review') : null
  return { project, state, slides: slidesResponse.slides, review }
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
