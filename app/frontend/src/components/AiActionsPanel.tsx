import { useEffect, useState } from 'react'
import type { AiAction, AiProposalInput, AiStatus, SlideItem } from '../types'

const actionLabels: Record<AiAction, string> = {
  shorten: '短くする',
  'add-diagram': '図を追加',
  'improve-structure': '構成を改善',
  custom: '自由に変更',
}

const phaseLabels = {
  preparing: '安全な作業領域を準備中',
  'running-agent': '変更案を作成中',
  'validating-candidate': '変更案を検証中',
  'registering-proposal': '確認用の変更案を登録中',
  succeeded: '変更案を作成しました',
  failed: '変更案を作成できませんでした',
}

type Props = {
  selected: SlideItem | null
  status: AiStatus | null
  disabled: boolean
  hasProposal: boolean
  onStart: (input: AiProposalInput) => void
  onRetry: (input: AiProposalInput) => void
}

export function AiActionsPanel({ selected, status, disabled, hasProposal, onStart, onRetry }: Props) {
  const supported = status?.supportedActions ?? []
  const [action, setAction] = useState<AiAction>('shorten')
  const [instruction, setInstruction] = useState('')
  const running = status?.status === 'running'
  const failed = status?.status === 'failed'

  useEffect(() => {
    if (supported.length && !supported.includes(action)) setAction(supported[0])
  }, [action, supported])

  const customMissing = action === 'custom' && !instruction.trim()
  const unavailableReason = status === null
    ? 'AI Actionsの利用可否を確認しています。'
    : !status.available
      ? status.reason ?? 'AI Actionsを利用できません。'
      : hasProposal
        ? '現在の変更案を確認・反映または取り消してから、新しい候補を作成できます。'
        : !status.allowedStage
          ? 'AI Actionsはwhole-deck HTMLの確認中だけ利用できます。'
          : null

  return (
    <section className="inspector-section ai-actions" aria-live="polite">
      <div className="section-kicker">AI Actions</div>
      <h3>選択したスライドの変更案</h3>
      <p className="muted">現在案は変更せず、確認用の変更案だけを作成します。</p>

      <div className="action-grid" role="group" aria-label="AI変更の種類">
        {(Object.keys(actionLabels) as AiAction[]).map((value) => (
          <button
            key={value}
            type="button"
            className={action === value ? 'is-active' : ''}
            onClick={() => setAction(value)}
            disabled={disabled || running || !supported.includes(value)}
          >
            {actionLabels[value]}
          </button>
        ))}
      </div>
      <label className="ai-instruction">
        <span>{action === 'custom' ? '変更内容（必須）' : '補足指示（任意）'}</span>
        <textarea
          value={instruction}
          maxLength={2000}
          rows={4}
          onChange={(event) => setInstruction(event.target.value)}
          disabled={disabled || running}
          placeholder={action === 'custom' ? 'どのように変更するか入力' : '必要な場合だけ補足を入力'}
        />
      </label>

      {unavailableReason && <p className="ai-unavailable">{unavailableReason}</p>}
      {running && status && (
        <div className="conversion-progress" role="status">
          <span className="conversion-spinner" aria-hidden="true" />
          <div><strong>{status.phase ? phaseLabels[status.phase] : '処理中'}</strong><p>{status.message}</p></div>
        </div>
      )}
      {failed && status && (
        <div className="conversion-error" role="alert">
          <strong>変更案を作成できませんでした</strong>
          <p>{status.error ?? status.message}</p>
        </div>
      )}
      {status?.status === 'succeeded' && (
        <div className="success-card"><strong>変更案を表示しています</strong><p>内容を確認するまで現在案には反映されません。</p></div>
      )}

      {failed && status.retryable ? (
        <button
          type="button"
          className="primary-button"
          onClick={() => selected && onRetry({ slideId: selected.id, action, instruction: instruction.trim() })}
          disabled={disabled || running || !selected || customMissing}
        >再試行</button>
      ) : (
        <button
          type="button"
          className="primary-button"
          disabled={disabled || running || Boolean(unavailableReason) || !selected || customMissing}
          onClick={() => selected && onStart({ slideId: selected.id, action, instruction: instruction.trim() })}
        >
          {running ? '変更案を作成しています…' : '変更案を作成'}
        </button>
      )}
    </section>
  )
}
