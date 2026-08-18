import { useState } from 'react'
import type { HtmlGenerationPhase, HtmlGenerationStatus } from '../types'


const phaseLabels: Record<HtmlGenerationPhase, string> = {
  preparing: '構成と資料を準備中',
  generating: 'HTML案を生成中',
  validating: 'HTMLと出典を検証中',
  'browser-checking': 'ブラウザ表示を確認中',
  'registering-candidate': '確認用HTML案を登録中',
  ready: '確認できます',
  failed: '生成に失敗しました',
}

type Props = {
  status: HtmlGenerationStatus | null
  disabled: boolean
  onStart: (instruction: string) => void
  onRetry: (instruction: string) => void
  onRegenerate: (instruction: string) => void
  onApply: () => void
  onCancel: () => void
}

export function HtmlGenerationPanel({
  status, disabled, onStart, onRetry, onRegenerate, onApply, onCancel,
}: Props) {
  const [instruction, setInstruction] = useState('')
  const running = status?.status === 'running'
  const candidate = status?.candidate ?? null
  const canStart = Boolean(status?.available && status.allowedStage && !candidate && !running)

  return (
    <section className="inspector-section html-generation-panel" aria-label="AI HTML Initial Generation">
      <div className="section-kicker">AI HTML Initial Generation</div>
      <h3>HTMLデザインを作成</h3>
      <p>承認済み構成からスライド全体の確認用HTML案を生成します。現在のHTMLは採用するまで変更しません。</p>

      {status?.phase && (
        <div className={`job-status status-${status.status}`}>
          <strong>{phaseLabels[status.phase]}</strong>
          <span>{status.message}</span>
        </div>
      )}

      {status?.status === 'failed' && (
        <div className="inline-alert" role="alert">
          <strong>HTML案を生成できませんでした</strong>
          <span>{status.error ?? status.message}</span>
        </div>
      )}

      {candidate && (
        <div className="generation-result">
          <strong>{candidate.summary}</strong>
          <dl>
            <div><dt>スライド</dt><dd>{candidate.generatedSlideCount}枚</dd></div>
            <div><dt>セクション</dt><dd>{candidate.sectionCount}件</dd></div>
          </dl>
          <p><b>Visual</b>{candidate.visualsSummary}</p>
          <p><b>Source</b>{candidate.provenanceSummary}</p>
          {candidate.warnings.length > 0 && (
            <ul className="generation-warnings">
              {candidate.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          )}
        </div>
      )}

      {!candidate && (
        <label className="field-label">
          表現上の補助指示（任意）
          <textarea
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            placeholder="例: 文字量を抑えて、図は必要な箇所だけにしてください"
            disabled={disabled || running}
            maxLength={2000}
          />
        </label>
      )}

      <div className="panel-actions stacked-actions">
        {canStart && (
          <button type="button" className="primary-button" disabled={disabled} onClick={() => onStart(instruction)}>
            HTML案を生成
          </button>
        )}
        {status?.status === 'failed' && status.retryable && (
          <button type="button" className="primary-button" disabled={disabled} onClick={() => onRetry(instruction)}>
            現在の指示で再試行
          </button>
        )}
        {candidate && (
          <>
            <button type="button" className="primary-button" disabled={disabled} onClick={onApply}>
              このHTML案を採用
            </button>
            <button type="button" disabled={disabled} onClick={() => onRegenerate(instruction)}>
              再生成
            </button>
            <button type="button" disabled={disabled} onClick={onCancel}>
              このHTML案を破棄
            </button>
          </>
        )}
      </div>

      {!status?.available && status?.reason && <small>{status.reason}</small>}
    </section>
  )
}
