import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AppState, LifecycleStatus } from '../types'
import { BentoLifecyclePanel } from './BentoLifecyclePanel'

const baseState: AppState = {
  mode: 'bento-edit', stage: 'bento_authoring', statusLabel: '編集中', nextActionLabel: '確認',
  canConvert: false, canEditBento: true, hasCandidate: false, isBlocked: false,
  bentoEditorUrl: 'http://127.0.0.1:8765/',
}

const idle: LifecycleStatus = {
  status: 'idle', action: null, phase: null, stage: 'bento_authoring',
  completedSteps: 0, totalSteps: 1, message: '待機中', error: null, retryable: false,
  availableActions: ['content-review'],
}

afterEach(cleanup)

describe('Bento lifecycle controls', () => {
  it.each([
    ['bento_authoring', '内容確認へ進む'],
    ['content_review', 'この内容を承認して最終調整へ進む'],
    ['bento_finalization', '最終版を承認して完成'],
  ] as const)('shows the action for %s', (stage, label) => {
    const mode = stage === 'bento_finalization' ? 'final-edit' : 'bento-edit'
    render(<BentoLifecyclePanel
      state={{ ...baseState, stage, mode }} status={{ ...idle, stage, availableActions: [] }}
      busy={false} onAction={vi.fn()}
    />)
    expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
  })

  it('shows completion actions after the workflow completes', () => {
    render(<BentoLifecyclePanel
      state={{ ...baseState, stage: 'complete', mode: 'complete', canEditBento: false }}
      status={{ ...idle, stage: 'complete', availableActions: ['final-open', 'final-reopen'] }}
      busy={false} onAction={vi.fn()}
    />)
    expect(screen.getByText('BentoSlideが完成しました')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '完成版を開く' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '最終調整を再開' })).toBeInTheDocument()
  })

  it('disables actions and shows the actual running phase', () => {
    render(<BentoLifecyclePanel
      state={baseState}
      status={{
        ...idle, status: 'running', action: 'content-review', phase: 'validating-content',
        completedSteps: 1, totalSteps: 3, message: '現在の内容を検証しています', availableActions: [],
      }}
      busy={false} onAction={vi.fn()}
    />)
    expect(screen.getByText('内容を検証中')).toBeInTheDocument()
    expect(screen.getByText('1 / 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '内容確認へ進む' })).toBeDisabled()
  })

  it('shows a safe error and dispatches retry', () => {
    const onAction = vi.fn()
    render(<BentoLifecyclePanel
      state={baseState}
      status={{
        ...idle, status: 'failed', action: 'content-review', phase: 'starting-editor',
        message: '失敗', error: '編集画面を準備できませんでした。', retryable: true,
        availableActions: ['content-review'],
      }}
      busy={false} onAction={onAction}
    />)
    expect(screen.getByRole('alert')).toHaveTextContent('編集画面を準備できませんでした。')
    fireEvent.click(screen.getByRole('button', { name: '再試行' }))
    expect(onAction).toHaveBeenCalledWith('content-review', true)
  })
})
