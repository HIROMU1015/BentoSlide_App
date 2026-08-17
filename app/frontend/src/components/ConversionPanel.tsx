import type { ConversionPhase, ConversionStatus } from '../types'

const phaseLabels: Record<ConversionPhase, string> = {
  validating: '承認済みHTMLを確認中',
  building: 'BentoSlideへ変換中',
  'validating-output': '変換結果を検証中',
  'starting-authoring': 'Bento編集画面を準備中',
  complete: '変換完了',
}

type Props = {
  status: ConversionStatus | null
  busy: boolean
  canStart: boolean
  onStart: () => void
  onRetry: () => void
}

export function ConversionPanel({ status, busy, canStart, onStart, onRetry }: Props) {
  const running = status?.status === 'running'
  const failed = status?.status === 'failed'
  const label = status?.phase ? phaseLabels[status.phase] : 'BentoSlideへ変換'

  return (
    <section className="inspector-section conversion-panel" aria-live="polite">
      <div className="section-kicker">Bento Conversion</div>
      <h3>{label}</h3>
      {running && (
        <div className="conversion-progress" role="status">
          <span className="conversion-spinner" aria-hidden="true" />
          <div>
            <strong>処理中</strong>
            <p>{status.message}</p>
          </div>
        </div>
      )}
      {status && status.phase && (
        <div className="conversion-steps">
          <span>完了したステップ</span>
          <strong>{status.completedSteps} / {status.totalSteps}</strong>
        </div>
      )}
      {failed && (
        <div className="conversion-error" role="alert">
          <strong>変換を完了できませんでした</strong>
          <p>{status.error ?? status.message}</p>
        </div>
      )}
      {failed && status.retryable ? (
        <button type="button" className="primary-button" onClick={onRetry} disabled={busy || running}>
          再試行
        </button>
      ) : (
        <button type="button" className="primary-button" onClick={onStart} disabled={!canStart || busy || running}>
          {running ? '変換しています…' : 'BentoSlideへ変換'}
        </button>
      )}
    </section>
  )
}
