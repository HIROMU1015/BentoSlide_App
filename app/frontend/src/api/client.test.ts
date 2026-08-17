import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getAiStatus, getConversionStatus, getLifecycleStatus, loadAppData, startAiProposal,
  startConversion, startLifecycleAction,
} from './client'
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
        : url === '/api/bento'
          ? { available: false, editorUrl: null, message: '準備中' }
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

describe('conversion API client', () => {
  it('posts explicit confirmation and reads status', async () => {
    const payload = {
      status: 'running', phase: 'validating', completedSteps: 0, totalSteps: 4,
      message: '確認中', error: null, retryable: false,
    }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => payload }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startConversion()
    await getConversionStatus()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/convert', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ confirmed: true }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/convert/status', expect.anything())
  })
})

describe('Bento lifecycle API client', () => {
  it('posts explicit confirmation to fixed action endpoints and reads status', async () => {
    const payload = {
      status: 'running', action: 'final-reopen', phase: 'reopening-final', stage: 'complete',
      completedSteps: 0, totalSteps: 2, message: '再開中', error: null, retryable: false,
      availableActions: [],
    }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => payload }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startLifecycleAction('final-reopen')
    await getLifecycleStatus()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/bento/final/reopen', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ confirmed: true }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/bento/lifecycle/status', expect.anything())
  })
})

describe('AI proposal API client', () => {
  it('posts only the explicit confirmation and bounded proposal fields', async () => {
    const payload = {
      available: true, reason: null, supportedActions: ['shorten'], allowedStage: true,
      status: 'running', phase: 'preparing', message: '準備中', error: null, retryable: false,
    }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => payload }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startAiProposal({ slideId: 's1', action: 'shorten', instruction: '簡潔に' })
    await getAiStatus()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/ai/proposals', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ confirmed: true, slideId: 's1', action: 'shorten', instruction: '簡潔に' }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/ai/status', expect.anything())
  })
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
