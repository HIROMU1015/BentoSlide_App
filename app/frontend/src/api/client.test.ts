import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadAppData } from './client'
import type { AppState, HtmlReview } from '../types'

const state: AppState = {
  mode: 'html-design',
  stage: 'html_review',
  statusLabel: 'Reviewing HTML',
  nextActionLabel: 'Review the candidate',
  canConvert: false,
  canEditBento: false,
  hasCandidate: true,
  isBlocked: false,
  bentoEditorUrl: null,
}

function installFetch(candidateHtmlUrl: string | null) {
  const review: HtmlReview = {
    currentHtmlUrl: '/api/html/view/current/',
    candidateHtmlUrl,
    fullPreviewUrl: null,
    proposal: null,
    actionToken: 'opaque-action-token-that-is-long-enough',
    canApply: false,
    canApproveDeck: false,
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const payload = url === '/api/project'
      ? { project: { title: 'Fixture', kind: 'fixture' } }
      : url === '/api/state'
        ? state
        : url === '/api/html/review'
          ? review
          : { slides: [{ id: url.endsWith('candidate') ? 'candidate-slide' : 'current-slide', title: 'Slide', number: 1, sectionTitle: null }] }
    return { ok: true, json: async () => payload } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('loadAppData', () => {
  it('loads the candidate navigator when the candidate preview exists', async () => {
    const fetchMock = installFetch('/api/html/view/candidate/')

    const result = await loadAppData('candidate')

    expect(result.slideView).toBe('candidate')
    expect(result.slides[0]?.id).toBe('candidate-slide')
    expect(fetchMock).toHaveBeenCalledWith('/api/slides?view=candidate', expect.anything())
  })

  it('falls back to the current navigator when the candidate disappeared', async () => {
    const fetchMock = installFetch(null)

    const result = await loadAppData('candidate')

    expect(result.slideView).toBe('current')
    expect(result.slides[0]?.id).toBe('current-slide')
    expect(fetchMock).toHaveBeenCalledWith('/api/slides?view=current', expect.anything())
  })
})
