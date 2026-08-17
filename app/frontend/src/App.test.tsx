import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getConversionStatus, loadAppData, startConversion } from './api/client'
import type { AppState, ConversionStatus, HtmlReview, HtmlView } from './types'

vi.mock('./api/client', () => ({
  applyHtmlChange: vi.fn(),
  approveHtmlDeck: vi.fn(),
  getConversionStatus: vi.fn(),
  loadAppData: vi.fn(),
  startConversion: vi.fn(),
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
  vi.mocked(startConversion).mockResolvedValue({
    ...idleConversion,
    status: 'running',
    phase: 'validating',
    message: '承認済みHTMLを確認しています',
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
    expect(screen.getByText('処理中')).toBeInTheDocument()
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
})
