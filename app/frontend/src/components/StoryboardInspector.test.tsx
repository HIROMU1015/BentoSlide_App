import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Storyboard } from '../types'
import { StoryboardInspector } from './StoryboardInspector'

const base: Storyboard = {
  stage: 'planning',
  request: { title: '依頼内容', sections: [{
    title: '概要', paragraphs: ['<img src=x onerror="alert(1)">'], bullets: ['安全な本文'],
  }] },
  explanationPolicy: { title: '説明方針', sections: [] },
  storyOutline: { title: '全体ストーリー', sections: [] },
  slidePlan: { title: 'スライド構成', sections: [] },
  sections: [],
  canInitialize: false,
  canSubmit: true,
  canApprove: false,
  nextActionLabel: '確認します',
  actionToken: 'opaque-storyboard-action-token',
}

afterEach(cleanup)

describe('Storyboard stage controls', () => {
  it.each([
    ['initialized', true, false, false, '構成作成を開始'],
    ['planning', false, true, false, '構成案を提出'],
    ['awaiting_plan_approval', false, false, true, 'この構成を承認'],
  ] as const)('shows only the permitted action in %s', (stage, canInitialize, canSubmit, canApprove, label) => {
    render(<StoryboardInspector
      storyboard={{ ...base, stage, canInitialize, canSubmit, canApprove }}
      selected={null}
      busy={false}
      onAction={vi.fn()}
    />)

    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
  })

  it('renders untrusted Markdown-derived text without creating HTML elements', () => {
    const { container } = render(<StoryboardInspector
      storyboard={base}
      selected={null}
      busy={false}
      onAction={vi.fn()}
    />)

    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
  })

  it('shows the actual Planning AI phase while generation is running', () => {
    render(<StoryboardInspector
      storyboard={base}
      selected={null}
      busy={true}
      onAction={vi.fn()}
      planningAi={{
        available: true, reason: null, allowedStage: true, status: 'running',
        phase: 'validating-candidate', message: 'Planning Candidateを検証しています。',
        error: null, retryable: false, hasProposal: false, proposalId: null,
      }}
    />)

    expect(screen.getByRole('status')).toHaveTextContent('変更案の整合性を検証中')
    expect(screen.getByText('Planning Candidateを検証しています。')).toBeInTheDocument()
  })
})
