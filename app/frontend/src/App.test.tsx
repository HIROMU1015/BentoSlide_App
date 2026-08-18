import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import {
  getAiStatus, getConversionStatus, getLifecycleStatus, loadAppData, startAiProposal,
  startConversion, startLifecycleAction,
} from './api/client'
import type { AiStatus, AppState, ConversionStatus, HtmlReview, HtmlView, LifecycleStatus } from './types'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

vi.mock('./api/client', () => ({
  applyHtmlChange: vi.fn(),
  approveHtmlDeck: vi.fn(),
  getConversionStatus: vi.fn(),
  getAiStatus: vi.fn(),
  getLifecycleStatus: vi.fn(),
  loadAppData: vi.fn(),
  startConversion: vi.fn(),
  startAiProposal: vi.fn(),
  startLifecycleAction: vi.fn(),
}))

const idleConversion: ConversionStatus = {
  status: 'idle',
  phase: null,
  completedSteps: 0,
  totalSteps: 4,
  message: '変換を開始できます',
  error: null,
  retryable: false,
}

const idleLifecycle: LifecycleStatus = {
  status: 'idle', action: null, phase: null, stage: 'html_review',
  completedSteps: 0, totalSteps: 1, message: '待機中', error: null, retryable: false,
  availableActions: [],
}

const idleAi: AiStatus = {
  available: true, reason: null,
  supportedActions: ['shorten', 'add-diagram', 'improve-structure', 'custom'],
  allowedStage: true, status: 'idle', phase: null,
  message: '変更案を作成できます', error: null, retryable: false,
}

const appState: AppState = {
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

const review: HtmlReview = {
  currentHtmlUrl: '/api/html/view/current/',
  candidateHtmlUrl: '/api/html/view/candidate/',
  fullPreviewUrl: null,
  proposal: null,
  actionToken: 'opaque-action-token-that-is-long-enough',
  canApply: false,
  canApproveDeck: false,
}

beforeEach(() => {
  vi.mocked(getConversionStatus).mockResolvedValue(idleConversion)
  vi.mocked(getLifecycleStatus).mockResolvedValue(idleLifecycle)
  vi.mocked(getAiStatus).mockResolvedValue(idleAi)
  vi.mocked(startConversion).mockResolvedValue({
    ...idleConversion,
    status: 'running',
    phase: 'validating',
    message: '承認済みHTMLを確認しています',
  })
  vi.mocked(startLifecycleAction).mockResolvedValue({
    ...idleLifecycle, status: 'running', action: 'content-review', phase: 'stopping-editor',
    stage: 'bento_authoring', totalSteps: 3, message: '編集画面を停止しています',
  })
  vi.mocked(startAiProposal).mockResolvedValue({
    ...idleAi, status: 'running', phase: 'preparing', message: '準備中',
  })
  vi.mocked(loadAppData).mockImplementation(async (view: HtmlView = 'current') => ({
    project: { project: { title: 'Fixture', kind: 'fixture' } },
    state: appState,
    review,
    bento: { available: false, editorUrl: null, message: '準備中' },
    slideView: view,
    slides: [{
      id: `${view}-slide`,
      title: view === 'candidate' ? 'Candidate slide' : 'Current slide',
      number: 1,
      sectionTitle: 'Main',
    }],
  }))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('App candidate navigation', () => {
  it('switches the navigator and selected slide with the HTML view', async () => {
    const { container } = render(<App />)
    await screen.findByText('Current slide')

    const candidateButton = container.querySelector<HTMLButtonElement>('button.candidate')
    expect(candidateButton).not.toBeNull()
    fireEvent.click(candidateButton!)

    await screen.findByText('Candidate slide')
    await waitFor(() => expect(loadAppData).toHaveBeenCalledWith('candidate'))
    expect(screen.queryByText('Current slide')).not.toBeInTheDocument()
  })
})

describe('App conversion workflow', () => {
  const readyState: AppState = {
    ...appState,
    mode: 'converting',
    stage: 'ready_for_conversion',
    canConvert: true,
    hasCandidate: false,
  }

  function useReadyState() {
    vi.mocked(loadAppData).mockResolvedValue({
      project: { project: { title: '変換テスト', kind: 'fixture' } },
      state: readyState,
      review: null,
      bento: { available: false, editorUrl: null, message: '準備中' },
      slideView: 'current',
      slides: [{ id: 'slide-1', title: 'Slide', number: 1, sectionTitle: null }],
    })
  }

  it('confirms and starts conversion', async () => {
    useReadyState()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'BentoSlideへ変換' }))

    await waitFor(() => expect(startConversion).toHaveBeenCalledTimes(1))
    expect(window.confirm).toHaveBeenCalledWith('承認済みHTMLをBentoSlideへ変換しますか？')
  })

  it('shows the real running phase and completed steps', async () => {
    useReadyState()
    vi.mocked(getConversionStatus).mockResolvedValue({
      status: 'running',
      phase: 'building',
      completedSteps: 1,
      totalSteps: 4,
      message: 'BentoSlideへ変換しています',
      error: null,
      retryable: false,
    })
    render(<App />)

    expect(await screen.findByText('BentoSlideへ変換中')).toBeInTheDocument()
    expect(screen.getByText('1 / 4')).toBeInTheDocument()
    expect(screen.getAllByText('処理中')).toHaveLength(2)
  })

  it('shows a failed reason and retries after confirmation', async () => {
    useReadyState()
    vi.mocked(getConversionStatus).mockResolvedValue({
      status: 'failed',
      phase: 'building',
      completedSteps: 1,
      totalSteps: 4,
      message: '変換に失敗しました',
      error: '変換元HTMLとregistryを確認してください。',
      retryable: true,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('変換元HTMLとregistryを確認してください。')
    fireEvent.click(screen.getByRole('button', { name: '再試行' }))

    await waitFor(() => expect(startConversion).toHaveBeenCalledTimes(1))
    expect(window.confirm).toHaveBeenCalledWith('BentoSlideへの変換を再試行しますか？')
  })

  it('refreshes all app data and opens the existing Bento editor after success', async () => {
    useReadyState()
    const succeeded: ConversionStatus = {
      status: 'succeeded', phase: 'complete', completedSteps: 4, totalSteps: 4,
      message: '変換が完了しました', error: null, retryable: false,
    }
    vi.mocked(getConversionStatus).mockResolvedValueOnce(idleConversion).mockResolvedValue(succeeded)
    vi.mocked(loadAppData)
      .mockResolvedValueOnce({
        project: { project: { title: '変換テスト', kind: 'fixture' } }, state: readyState, review: null,
        bento: { available: false, editorUrl: null, message: '準備中' }, slideView: 'current',
        slides: [{ id: 'slide-1', title: 'Slide', number: 1, sectionTitle: null }],
      })
      .mockResolvedValue({
        project: { project: { title: '変換テスト', kind: 'fixture' } },
        state: { ...readyState, mode: 'bento-edit', stage: 'bento_authoring', canConvert: false, canEditBento: true },
        review: null,
        bento: { available: true, editorUrl: 'http://127.0.0.1:8765/', message: '編集できます' },
        slideView: 'current',
        slides: [{ id: 'slide-1', title: 'Slide', number: 1, sectionTitle: null }],
      })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'BentoSlideへ変換' }))

    expect(await screen.findByTitle('Bento編集画面')).toHaveAttribute('src', 'http://127.0.0.1:8765/')
    expect(loadAppData).toHaveBeenCalledTimes(2)
  })

  it('settles an immediate conversion success before showing refreshed state', async () => {
    useReadyState()
    const refreshed = deferred<Awaited<ReturnType<typeof loadAppData>>>()
    const initial = await loadAppData('current')
    vi.mocked(loadAppData).mockReset().mockResolvedValueOnce(initial).mockReturnValueOnce(refreshed.promise)
    vi.mocked(startConversion).mockResolvedValue({
      ...idleConversion, status: 'succeeded', phase: 'complete', completedSteps: 4,
      message: '完了しました',
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'BentoSlideへ変換' }))
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
    expect(screen.getAllByText('処理中').length).toBeGreaterThan(0)

    refreshed.resolve({
      ...initial,
      state: { ...readyState, mode: 'bento-edit', stage: 'bento_authoring', canConvert: false, canEditBento: true },
      bento: { available: true, editorUrl: 'http://127.0.0.1:8765/', message: '編集できます' },
    })
    expect(await screen.findByTitle('Bento編集画面')).toHaveAttribute('src', 'http://127.0.0.1:8765/')
  })

  it('refreshes after an immediate conversion failure and enables retry only after settling', async () => {
    useReadyState()
    const refreshed = deferred<Awaited<ReturnType<typeof loadAppData>>>()
    const initial = await loadAppData('current')
    vi.mocked(loadAppData).mockReset().mockResolvedValueOnce(initial).mockReturnValueOnce(refreshed.promise)
    vi.mocked(startConversion).mockResolvedValue({
      ...idleConversion, status: 'failed', phase: 'building', completedSteps: 1,
      message: '失敗しました', error: '候補を確認してください', retryable: true,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'BentoSlideへ変換' }))
    const retry = await screen.findByRole('button', { name: '再試行' })
    expect(retry).toBeDisabled()
    refreshed.resolve(initial)
    await waitFor(() => expect(retry).toBeEnabled())
    expect(screen.getByRole('alert')).toHaveTextContent('候補を確認してください')
  })
})

describe('App Bento lifecycle workflow', () => {
  const authoringState: AppState = {
    ...appState,
    mode: 'bento-edit',
    stage: 'bento_authoring',
    canEditBento: true,
    hasCandidate: false,
    bentoEditorUrl: 'http://127.0.0.1:8765/',
  }

  function appData(state: AppState, editorUrl: string | null) {
    return {
      project: { project: { title: '承認テスト', kind: 'fixture' } },
      state,
      review: null,
      bento: { available: Boolean(editorUrl), editorUrl, message: editorUrl ? '編集できます' : '準備中' },
      slideView: 'current' as const,
      slides: [{ id: 'slide-1', title: 'Slide', number: 1, sectionTitle: null }],
    }
  }

  it('does not call a lifecycle API without confirmation', async () => {
    vi.mocked(loadAppData).mockResolvedValue(appData(authoringState, 'http://127.0.0.1:8765/'))
    vi.mocked(getLifecycleStatus).mockResolvedValue({
      ...idleLifecycle, stage: 'bento_authoring', availableActions: ['content-review'],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '内容確認へ進む' }))

    expect(startLifecycleAction).not.toHaveBeenCalled()
  })

  it('polls lifecycle status, hides the stale iframe, and loads the new editor session', async () => {
    const contentReviewState: AppState = { ...authoringState, stage: 'content_review' }
    const idleAuthoring: LifecycleStatus = {
      ...idleLifecycle, stage: 'bento_authoring', availableActions: ['content-review'],
    }
    const running: LifecycleStatus = {
      ...idleAuthoring, status: 'running', action: 'content-review', phase: 'stopping-editor',
      totalSteps: 3, message: '編集画面を停止しています', availableActions: [],
    }
    const succeeded: LifecycleStatus = {
      ...running, status: 'succeeded', phase: 'complete', stage: 'content_review',
      completedSteps: 3, message: '内容確認を開始しました。', availableActions: ['content-approve'],
    }
    vi.mocked(loadAppData)
      .mockResolvedValueOnce(appData(authoringState, 'http://127.0.0.1:8765/'))
      .mockResolvedValue(appData(contentReviewState, 'http://127.0.0.1:9876/'))
    vi.mocked(getLifecycleStatus).mockResolvedValueOnce(idleAuthoring).mockResolvedValue(succeeded)
    vi.mocked(startLifecycleAction).mockResolvedValue(running)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '内容確認へ進む' }))

    expect(await screen.findByTitle('Bento編集画面')).toHaveAttribute('src', 'http://127.0.0.1:9876/')
    expect(startLifecycleAction).toHaveBeenCalledWith('content-review')
    expect(loadAppData).toHaveBeenCalledTimes(2)
  })

  it('keeps the old editor hidden while an immediate lifecycle success is refreshing', async () => {
    const initial = appData(authoringState, 'http://127.0.0.1:8765/')
    const refreshed = deferred<ReturnType<typeof appData>>()
    vi.mocked(loadAppData).mockResolvedValueOnce(initial).mockReturnValueOnce(refreshed.promise)
    vi.mocked(getLifecycleStatus).mockResolvedValue({
      ...idleLifecycle, stage: 'bento_authoring', availableActions: ['content-review'],
    })
    vi.mocked(startLifecycleAction).mockResolvedValue({
      ...idleLifecycle, status: 'succeeded', action: 'content-review', phase: 'complete',
      stage: 'content_review', completedSteps: 3, totalSteps: 3, message: '内容確認を開始しました',
      availableActions: ['content-approve'],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '内容確認へ進む' }))
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
    expect(screen.queryByTitle('Bento編集画面')).not.toBeInTheDocument()

    refreshed.resolve(appData({ ...authoringState, stage: 'content_review' }, 'http://127.0.0.1:9876/'))
    expect(await screen.findByTitle('Bento編集画面')).toHaveAttribute('src', 'http://127.0.0.1:9876/')
  })

  it('refreshes after an immediate lifecycle failure and shows retry', async () => {
    const initial = appData(authoringState, 'http://127.0.0.1:8765/')
    vi.mocked(loadAppData).mockResolvedValue(initial)
    vi.mocked(getLifecycleStatus).mockResolvedValue({
      ...idleLifecycle, stage: 'bento_authoring', availableActions: ['content-review'],
    })
    vi.mocked(startLifecycleAction).mockResolvedValue({
      ...idleLifecycle, status: 'failed', action: 'content-review', phase: 'stopping-editor',
      stage: 'bento_authoring', totalSteps: 3, message: '開始できませんでした',
      error: '編集画面を停止できませんでした', retryable: true, availableActions: ['content-review'],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '内容確認へ進む' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('編集画面を停止できませんでした')
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: '再試行' })).toBeEnabled()
  })

  it('does not overlap lifecycle status polls while a previous request is pending', async () => {
    vi.mocked(loadAppData).mockResolvedValue(appData(authoringState, 'http://127.0.0.1:8765/'))
    const pendingPoll = deferred<LifecycleStatus>()
    vi.mocked(getLifecycleStatus)
      .mockResolvedValueOnce({ ...idleLifecycle, stage: 'bento_authoring', availableActions: ['content-review'] })
      .mockReturnValueOnce(pendingPoll.promise)
    vi.mocked(startLifecycleAction).mockResolvedValue({
      ...idleLifecycle, status: 'running', action: 'content-review', phase: 'stopping-editor',
      stage: 'bento_authoring', totalSteps: 3, message: '停止中', availableActions: [],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: '内容確認へ進む' }))

    await waitFor(() => expect(getLifecycleStatus).toHaveBeenCalledTimes(2), { timeout: 1000 })
    await new Promise((resolve) => window.setTimeout(resolve, 400))
    expect(getLifecycleStatus).toHaveBeenCalledTimes(2)
    pendingPoll.resolve({
      ...idleLifecycle, status: 'failed', action: 'content-review', phase: 'stopping-editor',
      stage: 'bento_authoring', totalSteps: 3, message: '失敗', error: '失敗', retryable: true,
      availableActions: ['content-review'],
    })
  })

  it('can reopen finalization from the completion screen', async () => {
    const completeState: AppState = {
      ...authoringState, mode: 'complete', stage: 'complete', canEditBento: false, bentoEditorUrl: null,
    }
    vi.mocked(loadAppData).mockResolvedValue(appData(completeState, null))
    vi.mocked(getLifecycleStatus).mockResolvedValue({
      ...idleLifecycle, stage: 'complete', availableActions: ['final-open', 'final-reopen'],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '最終調整を再開' }))

    await waitFor(() => expect(startLifecycleAction).toHaveBeenCalledWith('final-reopen'))
    expect(window.confirm).toHaveBeenCalledWith('最終承認を解除して最終調整を再開しますか？')
  })
})

describe('App AI proposal workflow', () => {
  it('confirms creation, refreshes the app, and switches to the review-only candidate', async () => {
    const initialData = {
      project: { project: { title: 'AI Fixture', kind: 'fixture' } },
      state: { ...appState, hasCandidate: false },
      review: { ...review, candidateHtmlUrl: null },
      bento: { available: false, editorUrl: null, message: '準備中' },
      slideView: 'current' as const,
      slides: [{ id: 's1', title: '現在案のスライド', number: 1, sectionTitle: 'Main' }],
    }
    const candidateReview: HtmlReview = {
      ...review,
      candidateHtmlUrl: '/api/html/view/candidate/',
      proposal: {
        status: 'proposed', scope: 'local', summary: '説明を短くする', impactSummary: '対象以外への変更なし',
        affectedSlides: [{ id: 's1', title: '変更案のスライド', number: 1, impact: 'changed' }],
        postApplyReviewStatus: null,
      },
      canApply: true,
    }
    const candidateData = {
      ...initialData,
      state: { ...appState, hasCandidate: true },
      review: candidateReview,
      slideView: 'candidate' as const,
      slides: [{ id: 's1', title: '変更案のスライド', number: 1, sectionTitle: 'Main' }],
    }
    vi.mocked(loadAppData).mockResolvedValueOnce(initialData).mockResolvedValue(candidateData)
    vi.mocked(getAiStatus).mockResolvedValue(idleAi)
    vi.mocked(startAiProposal).mockResolvedValue({
      ...idleAi, status: 'succeeded', phase: 'succeeded', allowedStage: false,
      message: '変更案を登録しました',
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '変更案を作成' }))

    await waitFor(() => expect(startAiProposal).toHaveBeenCalledWith({ slideId: 's1', action: 'shorten', instruction: '' }))
    expect(window.confirm).toHaveBeenCalledWith('現在案を変更せず、選択したスライドの確認用変更案をAIで作成しますか？')
    expect(await screen.findByTitle('変更案のHTMLプレビュー')).toHaveAttribute('src', '/api/html/view/candidate/')
    expect(screen.getAllByText('変更案のスライド').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '現在案' })).toBeInTheDocument()
    expect(screen.getByText('説明を短くする')).toBeInTheDocument()
  })

  it('retries with the action and instruction currently shown in the form', async () => {
    const initialData = {
      project: { project: { title: 'AI Fixture', kind: 'fixture' } },
      state: { ...appState, hasCandidate: false },
      review: { ...review, candidateHtmlUrl: null },
      bento: { available: false, editorUrl: null, message: 'Preparing' },
      slideView: 'current' as const,
      slides: [{ id: 's1', title: 'Current slide', number: 1, sectionTitle: 'Main' }],
    }
    vi.mocked(loadAppData).mockResolvedValue(initialData)
    vi.mocked(getAiStatus).mockResolvedValue(idleAi)
    vi.mocked(startAiProposal).mockResolvedValue({
      ...idleAi, status: 'failed', phase: 'failed', message: 'Proposal failed',
      error: 'Please retry', retryable: true,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '変更案を作成' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Please retry')

    fireEvent.click(screen.getByRole('button', { name: '自由に変更' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新しい指示' } })
    fireEvent.click(screen.getByRole('button', { name: '再試行' }))

    await waitFor(() => expect(startAiProposal).toHaveBeenCalledTimes(2))
    expect(startAiProposal).toHaveBeenNthCalledWith(1, {
      slideId: 's1', action: 'shorten', instruction: '',
    })
    expect(startAiProposal).toHaveBeenNthCalledWith(2, {
      slideId: 's1', action: 'custom', instruction: '新しい指示',
    })
  })
})
