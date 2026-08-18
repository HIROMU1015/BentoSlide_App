import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import {
  applyHtmlGeneration, applyPlanningAiProposal, cancelHtmlGeneration, cancelPlanningAiProposal,
  getAiStatus, getConversionStatus, getHtmlGenerationStatus, getLifecycleStatus,
  getPlanningAiStatus, getStoryboard, loadAppData, startAiProposal, startConversion,
  startHtmlGeneration, startLifecycleAction, startPlanningAiProposal, startStoryboardAction,
} from './api/client'
import type {
  AiStatus, AppState, ConversionStatus, HtmlReview, HtmlView, LifecycleStatus,
  HtmlGenerationStatus, PlanningAiStatus, Storyboard,
} from './types'

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
  applyPlanningAiProposal: vi.fn(),
  cancelPlanningAiProposal: vi.fn(),
  applyHtmlGeneration: vi.fn(),
  cancelHtmlGeneration: vi.fn(),
  getConversionStatus: vi.fn(),
  getAiStatus: vi.fn(),
  getLifecycleStatus: vi.fn(),
  getPlanningAiStatus: vi.fn(),
  getHtmlGenerationStatus: vi.fn(),
  getStoryboard: vi.fn(),
  loadAppData: vi.fn(),
  startConversion: vi.fn(),
  startAiProposal: vi.fn(),
  startLifecycleAction: vi.fn(),
  startPlanningAiProposal: vi.fn(),
  startHtmlGeneration: vi.fn(),
  startStoryboardAction: vi.fn(),
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

const idlePlanningAi: PlanningAiStatus = {
  available: true, reason: null, allowedStage: true, status: 'idle', phase: null,
  message: 'AI Planningを利用できます。', error: null, retryable: false,
  hasProposal: false, proposalId: null,
}

const idleHtmlGeneration: HtmlGenerationStatus = {
  available: true, reason: null, allowedStage: true, status: 'idle', phase: null,
  message: '承認済み構成からHTML案を生成できます。', error: null, retryable: false,
  hasCandidate: false, generationId: null, candidate: null,
}

const appState: AppState = {
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
  vi.mocked(getPlanningAiStatus).mockResolvedValue(idlePlanningAi)
  vi.mocked(getHtmlGenerationStatus).mockResolvedValue(idleHtmlGeneration)
  vi.mocked(getStoryboard).mockRejectedValue(new Error('Storyboard view is not configured in this test'))
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
  vi.mocked(startPlanningAiProposal).mockResolvedValue({
    ...idlePlanningAi, status: 'running', phase: 'preparing', message: '準備中',
  })
  vi.mocked(startHtmlGeneration).mockResolvedValue({
    ...idleHtmlGeneration, status: 'running', phase: 'preparing', message: '準備中',
  })
  vi.mocked(applyHtmlGeneration).mockResolvedValue({ ...idleHtmlGeneration, status: 'succeeded', phase: 'ready' })
  vi.mocked(cancelHtmlGeneration).mockResolvedValue(idleHtmlGeneration)
  vi.mocked(applyPlanningAiProposal).mockResolvedValue({} as Storyboard)
  vi.mocked(cancelPlanningAiProposal).mockResolvedValue({} as Storyboard)
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

describe('App Storyboard workflow', () => {
  const storyboard: Storyboard = {
    stage: 'planning',
    request: { title: '依頼内容', sections: [{ title: '概要', paragraphs: ['研究内容を明確に伝える'], bullets: [] }] },
    explanationPolicy: { title: '説明方針', sections: [{ title: '方針', paragraphs: ['専門語を先に定義する'], bullets: [] }] },
    storyOutline: { title: '全体ストーリー', sections: [{ title: '流れ', paragraphs: ['背景から方法へ進む'], bullets: [] }] },
    slidePlan: { title: 'スライド構成', sections: [{ title: '構成', paragraphs: [], bullets: ['背景', '方法'] }] },
    sections: [
      { id: 'intro', title: '導入', slides: [{
        id: 's1', number: 1, title: '背景', points: ['課題を示す'], sectionId: 'intro', sectionTitle: '導入',
        visual: { recommended: true, type: 'native-diagram', intent: '課題と目的を結ぶ', purpose: '関係を示す' },
      }] },
      { id: 'method', title: '方法', slides: [{
        id: 's2', number: 2, title: '解析手順', points: ['手順を示す'], sectionId: 'method', sectionTitle: '方法', visual: null,
      }] },
    ],
    canInitialize: false, canSubmit: true, canApprove: false,
    nextActionLabel: '構成案を確認して提出します。', actionToken: 'opaque-storyboard-action-token',
  }
  const storyboardState: AppState = {
    ...appState, mode: 'storyboard', stage: 'planning', htmlAvailable: false,
    hasCandidate: false, nextActionLabel: '構成案を確認します',
  }
  const storyboardData = {
    project: { project: { title: 'Storyboard Fixture', kind: 'fixture' } },
    state: storyboardState,
    review: null,
    storyboard,
    bento: { available: false, editorUrl: null, message: '準備中' },
    slideView: 'current' as const,
    slides: [
      { id: 's1', title: '背景', number: 1, sectionTitle: '導入' },
      { id: 's2', title: '解析手順', number: 2, sectionTitle: '方法' },
    ],
  }

  it('renders ordered cards, synchronizes selection, and exposes only the stage action', async () => {
    vi.mocked(loadAppData).mockResolvedValue(storyboardData)
    render(<App />)

    const navigator = await screen.findByRole('navigation', { name: 'Storyboard一覧' })
    const introduction = within(navigator).getByText('導入')
    const method = within(navigator).getByText('方法')
    expect(introduction.compareDocumentPosition(method) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('課題と目的を結ぶ')).toBeInTheDocument()
    fireEvent.click(within(navigator).getByRole('button', { name: /02解析手順/ }))

    expect(screen.getByText('02 解析手順')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /02\s*解析手順\s*手順を示す/ })).toHaveClass('is-selected')
    expect(screen.getByText('Visual: native-diagram')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '構成案を提出' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '構成作成を開始' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'この構成を承認' })).not.toBeInTheDocument()
  })

  it('confirms once, prevents duplicate submission, and refreshes after completion', async () => {
    vi.mocked(loadAppData).mockResolvedValue(storyboardData)
    const pending = deferred<Storyboard>()
    vi.mocked(startStoryboardAction).mockReturnValue(pending.promise)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    const button = await screen.findByRole('button', { name: '構成案を提出' })
    fireEvent.click(button)
    await waitFor(() => expect(startStoryboardAction).toHaveBeenCalledWith('submit', storyboard))
    expect(window.confirm).toHaveBeenCalledWith('現在の構成案を確認待ちとして提出しますか？')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(startStoryboardAction).toHaveBeenCalledTimes(1)

    pending.resolve({ ...storyboard, stage: 'awaiting_plan_approval', canSubmit: false, canApprove: true })
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
    expect(button).toBeEnabled()
  })

  it('requires explicit confirmation before approving a plan', async () => {
    const approvalStoryboard = {
      ...storyboard, stage: 'awaiting_plan_approval', canSubmit: false, canApprove: true,
    }
    vi.mocked(loadAppData).mockResolvedValue({ ...storyboardData, storyboard: approvalStoryboard })
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'この構成を承認' }))

    expect(window.confirm).toHaveBeenCalledWith('この構成を承認してHTML制作へ進みますか？')
    expect(startStoryboardAction).not.toHaveBeenCalled()
  })

  it('surfaces a stale token rejection without hiding the current Storyboard', async () => {
    vi.mocked(loadAppData).mockResolvedValue(storyboardData)
    vi.mocked(startStoryboardAction).mockRejectedValue(new Error('構成案が更新されています。最新のStoryboardを読み直してください。'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '構成案を提出' }))

    expect(await screen.findByRole('status')).toHaveTextContent('構成案が更新されています')
    expect(screen.getByRole('region', { name: 'Storyboard確認' })).toBeInTheDocument()
  })

  it('shows the approved-plan waiting state without requesting AI status', async () => {
    vi.mocked(getAiStatus).mockClear()
    vi.mocked(loadAppData).mockResolvedValue({
      ...storyboardData,
      state: { ...storyboardState, mode: 'html-design', stage: 'html_authoring', htmlAvailable: false },
      storyboard: null,
      slides: [],
    })
    render(<App />)

    expect(await screen.findByText('HTMLを準備しています')).toBeInTheDocument()
    expect(screen.getByText(/構成案は承認済みです/)).toBeInTheDocument()
    expect(getAiStatus).not.toHaveBeenCalled()
    expect(screen.queryByText('AI Actions')).not.toBeInTheDocument()
  })
})

describe('App Planning AI proposal workflow', () => {
  const planningState: AppState = {
    ...appState, mode: 'storyboard', stage: 'planning', htmlAvailable: false,
    hasCandidate: false, nextActionLabel: '構成案を確認します',
  }
  const proposal = {
    id: 'a'.repeat(32),
    status: 'proposed' as const,
    summary: '方法を2枚に分ける',
    impactSummary: '方法に詳細スライドを追加します。',
    impact: {
      slides: [{
        id: 'method-2', title: '詳細', change: 'added' as const,
        previousNumber: null, number: 3,
      }],
      sections: [{ id: 'method', title: '方法', change: 'changed' as const }],
      explanationPolicyChanged: true,
      storyOutlineChanged: true,
      slidePlanChanged: true,
      visualChanges: 1,
    },
    actionToken: 'opaque-planning-proposal-action-token',
  }
  const currentStoryboard: Storyboard = {
    view: 'current', proposal: null, stage: 'planning',
    request: { title: '依頼内容', sections: [] },
    explanationPolicy: { title: '説明方針', sections: [] },
    storyOutline: { title: '全体ストーリー', sections: [] },
    slidePlan: { title: 'スライド構成', sections: [] },
    sections: [{ id: 'method', title: '方法', slides: [{
      id: 'method-1', number: 1, title: '方法', points: ['手順'],
      sectionId: 'method', sectionTitle: '方法', visual: null,
    }] }],
    canInitialize: false, canSubmit: true, canApprove: false,
    nextActionLabel: '提出できます', actionToken: 'opaque-current-storyboard-token',
  }
  const proposedCurrent: Storyboard = {
    ...currentStoryboard, proposal, canSubmit: false,
    nextActionLabel: '変更案を確認してください',
  }
  const candidateStoryboard: Storyboard = {
    ...proposedCurrent, view: 'candidate',
    sections: [{ id: 'method', title: '方法', slides: [
      ...currentStoryboard.sections[0].slides,
      {
        id: 'method-2', number: 3, title: '詳細', points: ['詳細を示す'],
        sectionId: 'method', sectionTitle: '方法', visual: null,
      },
    ] }],
  }

  function planningData(storyboard: Storyboard) {
    return {
      project: { project: { title: 'Planning AI Fixture', kind: 'fixture' } },
      state: planningState,
      review: null,
      storyboard,
      bento: { available: false, editorUrl: null, message: '準備中' },
      slideView: 'current' as const,
      slides: storyboard.sections.flatMap((section) => section.slides.map((slide) => ({
        id: slide.id, title: slide.title, number: slide.number, sectionTitle: section.title,
      }))),
    }
  }

  it('creates a candidate, refreshes current state, and switches the Storyboard comparison', async () => {
    vi.mocked(loadAppData)
      .mockResolvedValueOnce(planningData(currentStoryboard))
      .mockResolvedValue(planningData(proposedCurrent))
    vi.mocked(startPlanningAiProposal).mockResolvedValue({
      ...idlePlanningAi, status: 'succeeded', phase: 'succeeded', allowedStage: false,
      message: '候補を作成しました', hasProposal: true, proposalId: proposal.id,
    })
    vi.mocked(getStoryboard).mockResolvedValue(candidateStoryboard)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    const input = await screen.findByPlaceholderText('例: 方法を2枚に分けて、結論を先にしてください')
    fireEvent.change(input, { target: { value: '方法を2枚に分けてください' } })
    fireEvent.click(screen.getByRole('button', { name: '変更案を作成' }))

    await waitFor(() => expect(startPlanningAiProposal).toHaveBeenCalledWith('方法を2枚に分けてください'))
    expect(window.confirm).toHaveBeenCalledWith('現在案を変更せず、Storyboard全体の確認用変更案をAIで作成しますか？')
    expect(await screen.findByText('方法を2枚に分ける')).toBeInTheDocument()
    expect(screen.getByText('Slide 3: 詳細')).toBeInTheDocument()
    expect(screen.getByText('Visual plan: 1件変更')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Candidate' })).toHaveClass('is-active')
    expect(getStoryboard).toHaveBeenCalledWith('candidate')
  })

  it('shows failure and retries with the instruction currently entered', async () => {
    vi.mocked(loadAppData).mockResolvedValue(planningData(currentStoryboard))
    vi.mocked(getPlanningAiStatus).mockResolvedValue({
      ...idlePlanningAi, status: 'failed', phase: 'failed',
      message: '候補を検証できません', error: 'visual planが不整合です', retryable: true,
    })
    vi.mocked(startPlanningAiProposal).mockResolvedValue({
      ...idlePlanningAi, status: 'failed', phase: 'failed',
      message: '再試行に失敗しました', error: '再試行に失敗しました', retryable: true,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('visual planが不整合です')
    const input = screen.getByPlaceholderText('例: 方法を2枚に分けて、結論を先にしてください')
    fireEvent.change(input, { target: { value: '現在の入力を使う' } })
    fireEvent.click(screen.getByRole('button', { name: '現在の指示で再試行' }))

    await waitFor(() => expect(startPlanningAiProposal).toHaveBeenCalledWith('現在の入力を使う'))
  })

  it('applies only after confirmation and then reloads the canonical Storyboard', async () => {
    vi.mocked(loadAppData)
      .mockResolvedValueOnce(planningData(proposedCurrent))
      .mockResolvedValue(planningData({ ...candidateStoryboard, view: 'current', proposal: null, canSubmit: true }))
    vi.mocked(getPlanningAiStatus).mockResolvedValue({
      ...idlePlanningAi, status: 'succeeded', phase: 'succeeded', allowedStage: false,
      hasProposal: true, proposalId: proposal.id,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'この変更案を反映' }))

    await waitFor(() => expect(applyPlanningAiProposal).toHaveBeenCalledWith(proposedCurrent))
    expect(window.confirm).toHaveBeenCalledWith(
      'このPlanning Candidate全体を現在案へ反映しますか？提出と承認は別途必要です。',
    )
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
  })

  it('cancels a proposal without applying it', async () => {
    vi.mocked(loadAppData)
      .mockResolvedValueOnce(planningData(proposedCurrent))
      .mockResolvedValue(planningData(currentStoryboard))
    vi.mocked(getPlanningAiStatus).mockResolvedValue({
      ...idlePlanningAi, status: 'succeeded', phase: 'succeeded', allowedStage: false,
      hasProposal: true, proposalId: proposal.id,
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'この変更案を破棄' }))

    await waitFor(() => expect(cancelPlanningAiProposal).toHaveBeenCalledWith(proposedCurrent))
    expect(applyPlanningAiProposal).not.toHaveBeenCalled()
  })
})

describe('App initial HTML generation workflow', () => {
  const generationState: AppState = {
    ...appState,
    mode: 'html-design',
    stage: 'html_authoring',
    htmlAvailable: false,
    hasCandidate: false,
  }
  const candidate = {
    id: 'b'.repeat(32),
    status: 'proposed' as const,
    summary: '承認済み構成から2枚のHTML案を生成しました。',
    generatedSlideCount: 2,
    sectionCount: 2,
    visualsSummary: '図は必要な箇所だけに限定しました。',
    provenanceSummary: '許可された一次資料だけを使用しました。',
    warnings: ['画像生成は行っていません。'],
    slides: [
      { id: 's1', title: '背景', number: 1, sectionId: 'intro', sectionTitle: '導入' },
      { id: 's2', title: '方法', number: 2, sectionId: 'method', sectionTitle: '方法' },
    ],
    candidateHtmlUrl: '/api/html/view/candidate/',
    actionToken: 'opaque-html-generation-action-token',
  }
  const readyGeneration: HtmlGenerationStatus = {
    ...idleHtmlGeneration,
    allowedStage: false,
    status: 'succeeded',
    phase: 'ready',
    message: '生成されたHTML案を確認できます。',
    hasCandidate: true,
    generationId: candidate.id,
    candidate,
  }

  function generationData(status: HtmlGenerationStatus) {
    return {
      project: { project: { title: 'HTML Generation Fixture', kind: 'fixture' } },
      state: generationState,
      review: null,
      htmlGeneration: status,
      bento: { available: false, editorUrl: null, message: '準備中' },
      slideView: status.candidate ? 'candidate' as const : 'current' as const,
      slides: status.candidate
        ? status.candidate.slides.map((slide) => ({
          id: slide.id, title: slide.title, number: slide.number, sectionTitle: slide.sectionTitle,
        }))
        : [],
    }
  }

  it('shows the generation entry point and starts only after explicit confirmation', async () => {
    vi.mocked(loadAppData).mockResolvedValue(generationData(idleHtmlGeneration))
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(idleHtmlGeneration)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'HTML案を生成' }))

    await waitFor(() => expect(startHtmlGeneration).toHaveBeenCalledWith(''))
    expect(window.confirm).toHaveBeenCalledWith('承認済み構成から確認用のHTML案をAIで生成しますか？')
  })

  it('shows the actual running phase and retryable failure', async () => {
    const running: HtmlGenerationStatus = {
      ...idleHtmlGeneration,
      status: 'running', phase: 'browser-checking', allowedStage: true,
      message: 'ブラウザで全スライドの表示を確認しています。',
    }
    vi.mocked(loadAppData).mockResolvedValue(generationData(running))
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(running)
    const { unmount } = render(<App />)
    expect(await screen.findByText('ブラウザ表示を確認中')).toBeInTheDocument()
    expect(screen.getByText('ブラウザで全スライドの表示を確認しています。')).toBeInTheDocument()
    unmount()

    const failed: HtmlGenerationStatus = {
      ...idleHtmlGeneration,
      status: 'failed', phase: 'failed', retryable: true,
      message: 'HTML案を検証できませんでした。', error: 'slide IDが構成と一致しません。',
    }
    vi.mocked(loadAppData).mockResolvedValue(generationData(failed))
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(failed)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('slide IDが構成と一致しません。')
    fireEvent.click(screen.getByRole('button', { name: '現在の指示で再試行' }))
    await waitFor(() => expect(startHtmlGeneration).toHaveBeenCalled())
  })

  it('reviews result metadata and applies the exact candidate into existing HTML review', async () => {
    const reviewedState = { ...appState, hasCandidate: false }
    vi.mocked(loadAppData)
      .mockResolvedValueOnce(generationData(readyGeneration))
      .mockResolvedValue({
        project: { project: { title: 'HTML Generation Fixture', kind: 'fixture' } },
        state: reviewedState,
        review,
        htmlGeneration: null,
        bento: { available: false, editorUrl: null, message: '準備中' },
        slideView: 'current',
        slides: [{ id: 's1', title: '背景', number: 1, sectionTitle: '導入' }],
      })
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(readyGeneration)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    expect(await screen.findByText(candidate.summary)).toBeInTheDocument()
    expect(screen.getByText('2枚')).toBeInTheDocument()
    expect(screen.getByTitle('生成されたHTML案のプレビュー')).toHaveAttribute('src', candidate.candidateHtmlUrl)
    fireEvent.click(screen.getByRole('button', { name: 'このHTML案を採用' }))

    await waitFor(() => expect(applyHtmlGeneration).toHaveBeenCalledWith(readyGeneration))
    expect(window.confirm).toHaveBeenCalledWith(
      'このHTML案をcanonical HTMLとして採用し、HTML全体の確認へ進みますか？',
    )
    await waitFor(() => expect(loadAppData).toHaveBeenCalledTimes(2))
  })

  it('cancels without applying the candidate', async () => {
    vi.mocked(loadAppData).mockResolvedValue(generationData(readyGeneration))
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(readyGeneration)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'このHTML案を破棄' }))
    await waitFor(() => expect(cancelHtmlGeneration).toHaveBeenCalledWith(readyGeneration))
    expect(applyHtmlGeneration).not.toHaveBeenCalled()
  })

  it('regenerates only after cancelling the reviewed candidate', async () => {
    vi.mocked(loadAppData).mockResolvedValue(generationData(readyGeneration))
    vi.mocked(getHtmlGenerationStatus).mockResolvedValue(readyGeneration)
    const order: string[] = []
    vi.mocked(cancelHtmlGeneration).mockImplementation(async () => {
      order.push('cancel')
      return idleHtmlGeneration
    })
    vi.mocked(startHtmlGeneration).mockImplementation(async () => {
      order.push('start')
      return { ...idleHtmlGeneration, status: 'running', phase: 'preparing' }
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '再生成' }))

    await waitFor(() => expect(startHtmlGeneration).toHaveBeenCalledWith(''))
    expect(cancelHtmlGeneration).toHaveBeenCalledWith(readyGeneration)
    expect(order).toEqual(['cancel', 'start'])
    expect(applyHtmlGeneration).not.toHaveBeenCalled()
    expect(window.confirm).toHaveBeenCalledWith(
      '現在のHTML案を破棄し、承認済み構成から再生成しますか？',
    )
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

    const createButton = await screen.findByRole('button', { name: '変更案を作成' })
    await waitFor(() => expect(createButton).toBeEnabled())
    fireEvent.click(createButton)

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

    const createButton = await screen.findByRole('button', { name: '変更案を作成' })
    await waitFor(() => expect(createButton).toBeEnabled())
    fireEvent.click(createButton)
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
