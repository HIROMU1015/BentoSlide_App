import type { AppState, LifecycleAction, LifecyclePhase, LifecycleStatus } from '../types'

const phaseLabels: Record<LifecyclePhase, string> = {
  'stopping-editor': '編集画面を安全に停止中',
  'validating-content': '内容を検証中',
  'approving-content': '内容を承認中',
  'initializing-final': '最終調整を準備中',
  'starting-editor': '編集画面を起動中',
  'approving-final': '最終版を承認中',
  completing: '完成処理中',
  'reopening-final': '最終調整を再開中',
  'opening-final': '完成版を開いています',
  complete: '処理完了',
}

type Props = {
  state: AppState
  status: LifecycleStatus | null
  busy: boolean
  onAction: (action: LifecycleAction, retry?: boolean) => void
}

function actionForStage(state: AppState, status: LifecycleStatus | null): LifecycleAction | null {
  const available = status?.availableActions ?? []
  if (state.stage === 'bento_authoring') return 'content-review'
  if (state.stage === 'content_review') {
    return available.includes('content-review') ? 'content-review' : 'content-approve'
  }
  if (state.stage === 'bento_finalization') {
    return available.includes('final-reopen') ? 'final-reopen' : 'final-approve'
  }
  return null
}

const actionLabels: Record<LifecycleAction, string> = {
  'content-review': '内容確認へ進む',
  'content-approve': 'この内容を承認して最終調整へ進む',
  'final-approve': '最終版を承認して完成',
  'final-reopen': '最終調整を再開',
  'final-open': '完成版を開く',
}

export function BentoLifecyclePanel({ state, status, busy, onAction }: Props) {
  const running = status?.status === 'running'
  const failed = status?.status === 'failed'
  const action = actionForStage(state, status)
  const phase = status?.phase ? phaseLabels[status.phase] : null

  return (
    <section className="inspector-section lifecycle-panel" aria-live="polite">
      <div className="section-kicker">Bento Workflow</div>
      <h3>{phase ?? (state.stage === 'complete' ? 'BentoSlideが完成しました' : '承認と完成')}</h3>

      {running && (
        <div className="conversion-progress" role="status">
          <span className="conversion-spinner" aria-hidden="true" />
          <div><strong>処理中</strong><p>{status.message}</p></div>
        </div>
      )}
      {running && (
        <div className="conversion-steps">
          <span>完了したステップ</span>
          <strong>{status.completedSteps} / {status.totalSteps}</strong>
        </div>
      )}
      {failed && (
        <div className="conversion-error" role="alert">
          <strong>処理を完了できませんでした</strong>
          <p>{status.error ?? status.message}</p>
        </div>
      )}

      {failed && status.retryable && status.action ? (
        <button
          type="button" className="primary-button" disabled={busy || running}
          onClick={() => onAction(status.action!, true)}
        >
          再試行
        </button>
      ) : state.stage === 'complete' ? (
        <div className="completion-actions">
          <p>最終版の検証と承認が完了しています。</p>
          <button type="button" className="primary-button" disabled={busy || running} onClick={() => onAction('final-open')}>
            {actionLabels['final-open']}
          </button>
          <button type="button" className="secondary-button" disabled={busy || running} onClick={() => onAction('final-reopen')}>
            {actionLabels['final-reopen']}
          </button>
        </div>
      ) : action ? (
        <button
          type="button" className="primary-button" disabled={busy || running}
          onClick={() => onAction(action)}
        >
          {actionLabels[action]}
        </button>
      ) : null}
    </section>
  )
}
