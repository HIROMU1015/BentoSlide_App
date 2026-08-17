import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { loadAppData } from './api/client'
import type { AppState, HtmlReview, HtmlView } from './types'

vi.mock('./api/client', () => ({
  applyHtmlChange: vi.fn(),
  approveHtmlDeck: vi.fn(),
  loadAppData: vi.fn(),
}))

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
  vi.mocked(loadAppData).mockImplementation(async (view: HtmlView = 'current') => ({
    project: { project: { title: 'Fixture', kind: 'fixture' } },
    state: appState,
    review,
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
