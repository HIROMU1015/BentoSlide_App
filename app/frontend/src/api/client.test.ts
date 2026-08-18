import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  applyHtmlGeneration, applyPlanningAiProposal, cancelHtmlGeneration, cancelPlanningAiProposal,
  getAiStatus, getConversionStatus, getHtmlGenerationStatus, getLifecycleStatus,
  getPlanningAiStatus, getStoryboard, loadAppData, startAiProposal, startConversion,
  startHtmlGeneration, startLifecycleAction, startPlanningAiProposal, startStoryboardAction,
} from './client'
import type { AppState, HtmlGenerationStatus, HtmlReview, Storyboard } from '../types'

const state: AppState = {
  mode: 'html-design',
  stage: 'html_review',
  statusLabel: 'Reviewing HTML',
  nextActionLabel: 'Review the candidate',
  canConvert: false,
  htmlAvailable: true,
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

describe('Planning AI proposal API client', () => {
  it('uses the polling, candidate, apply, and cancel endpoints with explicit confirmation', async () => {
    const proposal = {
      id: 'a'.repeat(32), status: 'proposed' as const, summary: '変更', impactSummary: '1枚追加',
      impact: {
        slides: [], sections: [], explanationPolicyChanged: false,
        storyOutlineChanged: false, slidePlanChanged: true, visualChanges: 1,
      },
      actionToken: 'opaque-planning-proposal-token',
    }
    const storyboard = {
      view: 'candidate' as const, proposal, stage: 'planning',
      request: { title: '依頼', sections: [] }, explanationPolicy: { title: '方針', sections: [] },
      storyOutline: { title: '流れ', sections: [] }, slidePlan: { title: '構成', sections: [] },
      sections: [], canInitialize: false, canSubmit: false, canApprove: false,
      nextActionLabel: '確認', actionToken: 'opaque-storyboard-token-value',
    }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => storyboard }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startPlanningAiProposal('方法を分ける')
    await getPlanningAiStatus()
    await getStoryboard('candidate')
    await applyPlanningAiProposal(storyboard)
    await cancelPlanningAiProposal(storyboard)

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/ai/planning/proposals', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ confirmed: true, instruction: '方法を分ける' }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/ai/planning/status', expect.anything())
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/storyboard?view=candidate', expect.anything())
    expect(fetchMock).toHaveBeenNthCalledWith(
      4, `/api/ai/planning/proposals/${proposal.id}/apply`, expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirmed: true, actionToken: proposal.actionToken }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      5, `/api/ai/planning/proposals/${proposal.id}/cancel`, expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirmed: true, actionToken: proposal.actionToken }),
      }),
    )
  })
})

describe('initial HTML generation API client', () => {
  it('uses polling, start, apply, and cancel endpoints with opaque confirmation data', async () => {
    const status: HtmlGenerationStatus = {
      available: true, reason: null, allowedStage: false, status: 'succeeded', phase: 'ready',
      message: '確認できます', error: null, retryable: false, hasCandidate: true,
      generationId: 'b'.repeat(32),
      candidate: {
        id: 'b'.repeat(32), status: 'proposed', summary: '生成しました',
        generatedSlideCount: 1, sectionCount: 1, visualsSummary: '図なし',
        provenanceSummary: '一次資料のみ', warnings: [],
        slides: [{ id: 's1', title: '背景', number: 1, sectionId: 'main', sectionTitle: 'Main' }],
        candidateHtmlUrl: '/api/html/view/candidate/',
        actionToken: 'opaque-html-generation-action-token',
      },
    }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => status }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startHtmlGeneration('文字量を抑えて')
    await getHtmlGenerationStatus()
    await applyHtmlGeneration(status)
    await cancelHtmlGeneration(status)

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/ai/html-generation', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ confirmed: true, instruction: '文字量を抑えて' }),
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/ai/html-generation/status', expect.anything())
    expect(fetchMock).toHaveBeenNthCalledWith(
      3, `/api/ai/html-generation/${status.candidate!.id}/apply`, expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirmed: true, actionToken: status.candidate!.actionToken }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      4, `/api/ai/html-generation/${status.candidate!.id}/cancel`, expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirmed: true, actionToken: status.candidate!.actionToken }),
      }),
    )
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

  it('loads Storyboard without requesting HTML review or slide APIs', async () => {
    const storyboard: Storyboard = {
      stage: 'planning',
      request: { title: '依頼内容', sections: [] },
      explanationPolicy: { title: '説明方針', sections: [] },
      storyOutline: { title: '全体ストーリー', sections: [] },
      slidePlan: { title: 'スライド構成', sections: [] },
      sections: [{ id: 'main', title: 'Main', slides: [{
        id: 's1', number: 1, title: '背景', points: ['課題'], sectionId: 'main', sectionTitle: 'Main', visual: null,
      }] }],
      canInitialize: false, canSubmit: true, canApprove: false,
      nextActionLabel: '提出します', actionToken: 'storyboard-action-token-is-opaque',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const payload = url === '/api/project'
        ? { project: { title: 'Fixture', kind: 'fixture' } }
        : url === '/api/state'
          ? { ...state, mode: 'storyboard', stage: 'planning', htmlAvailable: false }
          : url === '/api/bento'
            ? { available: false, editorUrl: null, message: '準備中' }
            : storyboard
      return { ok: true, json: async () => payload } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadAppData()

    expect(result.storyboard).toEqual(storyboard)
    expect(result.slides).toEqual([{ id: 's1', number: 1, title: '背景', sectionTitle: 'Main' }])
    expect(fetchMock).toHaveBeenCalledWith('/api/storyboard', expect.anything())
    expect(fetchMock.mock.calls.map(([url]) => String(url))).not.toContain('/api/html/review')
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/slides'))).toBe(false)
  })

  it('keeps approved Storyboard usable while HTML is not created yet', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const payload = url === '/api/project'
        ? { project: { title: 'Fixture', kind: 'fixture' } }
        : url === '/api/state'
          ? { ...state, mode: 'html-design', stage: 'html_authoring', htmlAvailable: false }
          : { available: false, editorUrl: null, message: '準備中' }
      return { ok: true, json: async () => payload } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadAppData()

    expect(result.review).toBeNull()
    expect(result.slides).toEqual([])
    expect(fetchMock.mock.calls.map(([url]) => String(url))).not.toContain('/api/html/review')
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/slides'))).toBe(false)
  })
})

describe('Storyboard API client', () => {
  it('posts explicit confirmation with only the opaque action token', async () => {
    const storyboard = {
      stage: 'planning', request: { title: '依頼', sections: [] },
      explanationPolicy: { title: '方針', sections: [] }, storyOutline: { title: '流れ', sections: [] },
      slidePlan: { title: '構成', sections: [] }, sections: [], canInitialize: false, canSubmit: true,
      canApprove: false, nextActionLabel: '提出', actionToken: 'opaque-storyboard-token-value',
    } satisfies Storyboard
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => storyboard }) as Response)
    vi.stubGlobal('fetch', fetchMock)

    await startStoryboardAction('submit', storyboard)

    expect(fetchMock).toHaveBeenCalledWith('/api/storyboard/submit', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ confirmed: true, actionToken: storyboard.actionToken }),
    }))
  })
})
