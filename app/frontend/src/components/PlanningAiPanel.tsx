import { useState } from 'react'
import type { PlanningAiStatus, Storyboard } from '../types'

type Props = {
  storyboard: Storyboard
  status: PlanningAiStatus | null
  disabled: boolean
  onStart: (instruction: string) => void
  onRetry: (instruction: string) => void
  onApply: () => void
  onCancel: () => void
}

const phaseLabels: Record<string, string> = {
  preparing: '現在案と一次資料を準備中',
  'running-agent': 'Storyboardの変更案を作成中',
  'validating-candidate': '変更案の整合性を検証中',
  'registering-proposal': '確認用の変更案を登録中',
  succeeded: '変更案を作成済み',
  failed: '変更案を作成できませんでした',
}

const changeLabels: Record<string, string> = {
  changed: '変更',
  added: '追加',
  removed: '削除',
  moved: '移動',
}

export function PlanningAiPanel({
  storyboard, status, disabled, onStart, onRetry, onApply, onCancel,
}: Props) {
  const [instruction, setInstruction] = useState('')
  const proposal = storyboard.proposal ?? null
  const running = status?.status === 'running'
  const canStart = storyboard.stage === 'planning' && !proposal && Boolean(status?.available && status.allowedStage)
  const canRequest = status?.status === 'failed'
    ? Boolean(status.available && status.allowedStage && status.retryable)
    : canStart

  return (
    <section className="inspector-section planning-ai-panel" aria-label="AI Planning Proposal">
      <div className="section-kicker">AI Planning</div>
      <h3>自然言語で構成の変更案を作成</h3>
      <p>AIは確認用のCandidateだけを作成します。現在案は「この変更案を反映」を押すまで変わりません。</p>

      {running && (
        <div className="conversion-progress" role="status">
          <span className="conversion-spinner" aria-hidden="true" />
          <div>
            <strong>{phaseLabels[status.phase ?? ''] ?? '処理中'}</strong>
            <p>{status.message}</p>
          </div>
        </div>
      )}

      {proposal ? (
        <div className="planning-impact">
          <strong>{proposal.summary}</strong>
          <p>{proposal.impactSummary}</p>
          <ul>
            {proposal.impact.slides.map((slide) => (
              <li key={`${slide.id}-${slide.change}`}>
                <span className={`impact ${slide.change}`}>{changeLabels[slide.change]}</span>
                <span>
                  {slide.previousNumber != null && slide.number != null && slide.previousNumber !== slide.number
                    ? `Slide ${slide.previousNumber} → Slide ${slide.number}: ${slide.title}`
                    : `Slide ${slide.number ?? slide.previousNumber ?? '—'}: ${slide.title}`}
                </span>
              </li>
            ))}
            {proposal.impact.sections.map((section) => (
              <li key={`${section.id}-${section.change}`}>
                <span className={`impact ${section.change}`}>{changeLabels[section.change]}</span>
                <span>Section: {section.title}</span>
              </li>
            ))}
          </ul>
          <div className="planning-impact-flags">
            {proposal.impact.explanationPolicyChanged && <span>説明方針を変更</span>}
            {proposal.impact.storyOutlineChanged && <span>全体ストーリーを変更</span>}
            {proposal.impact.slidePlanChanged && <span>スライド構成を変更</span>}
            <span>Visual plan: {proposal.impact.visualChanges}件変更</span>
          </div>
          <button className="primary-button" type="button" disabled={disabled} onClick={onApply}>
            この変更案を反映
          </button>
          <button className="secondary-button" type="button" disabled={disabled} onClick={onCancel}>
            この変更案を破棄
          </button>
        </div>
      ) : (
        <>
          <label className="ai-instruction">
            変更したい内容
            <textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="例: 方法を2枚に分けて、結論を先にしてください"
              maxLength={2000}
              disabled={disabled || running}
            />
          </label>
          {status?.status === 'failed' && (
            <div className="conversion-error" role="alert">
              <strong>変更案を作成できませんでした</strong>
              <p>{status.error ?? status.message}</p>
            </div>
          )}
          {status && !status.available && <p className="ai-unavailable">{status.reason ?? status.message}</p>}
          <button
            className="primary-button"
            type="button"
            disabled={disabled || running || !instruction.trim() || !canRequest}
            onClick={() => status?.status === 'failed' ? onRetry(instruction) : onStart(instruction)}
          >
            {status?.status === 'failed' ? '現在の指示で再試行' : '変更案を作成'}
          </button>
        </>
      )}
    </section>
  )
}
