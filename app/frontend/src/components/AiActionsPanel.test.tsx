import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AiStatus, SlideItem } from '../types'
import { AiActionsPanel } from './AiActionsPanel'

const slide: SlideItem = { id: 's1', title: '対象', number: 1, sectionTitle: 'Main' }
const idle: AiStatus = {
  available: true, reason: null,
  supportedActions: ['shorten', 'add-diagram', 'improve-structure', 'custom'],
  allowedStage: true, status: 'idle', phase: null, message: '利用できます', error: null, retryable: false,
}

afterEach(cleanup)

function renderPanel(status: AiStatus = idle, overrides = {}) {
  const onStart = vi.fn()
  const onRetry = vi.fn()
  render(<AiActionsPanel
    selected={slide}
    status={status}
    disabled={false}
    hasProposal={false}
    onStart={onStart}
    onRetry={onRetry}
    {...overrides}
  />)
  return { onStart, onRetry }
}

describe('AiActionsPanel', () => {
  it('starts the selected action with an optional instruction', () => {
    const { onStart } = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: '図を追加' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '流れが分かる図' } })
    fireEvent.click(screen.getByRole('button', { name: '変更案を作成' }))
    expect(onStart).toHaveBeenCalledWith({ slideId: 's1', action: 'add-diagram', instruction: '流れが分かる図' })
  })

  it('shows the real running phase', () => {
    renderPanel({ ...idle, status: 'running', phase: 'validating-candidate', message: '候補を検証しています' })
    expect(screen.getByRole('status')).toHaveTextContent('変更案を検証中')
    expect(screen.getByRole('status')).toHaveTextContent('候補を検証しています')
  })

  it('shows failure and calls retry', () => {
    const { onRetry } = renderPanel({
      ...idle, status: 'failed', phase: 'failed', message: '失敗しました', error: '対象外の変更があります', retryable: true,
    })
    expect(screen.getByRole('alert')).toHaveTextContent('対象外の変更があります')
    fireEvent.click(screen.getByRole('button', { name: '再試行' }))
    expect(onRetry).toHaveBeenCalledWith({ slideId: 's1', action: 'shorten', instruction: '' })
  })

  it('requires a custom instruction and disables unavailable SDK state', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: '自由に変更' }))
    expect(screen.getByRole('button', { name: '変更案を作成' })).toBeDisabled()
  })
})
