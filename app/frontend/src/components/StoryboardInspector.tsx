import type { Storyboard, StoryboardAction, StoryboardDocument, StoryboardSlide } from '../types'

type Props = {
  storyboard: Storyboard
  selected: StoryboardSlide | null
  busy: boolean
  onAction: (action: StoryboardAction) => void
}

function DocumentView({ document }: { document: StoryboardDocument }) {
  return (
    <div className="storyboard-document">
      {document.sections.length === 0 && <p className="muted">表示する本文はまだありません。</p>}
      {document.sections.map((section, index) => (
        <section key={`${section.title}-${index}`}>
          <h4>{section.title}</h4>
          {section.paragraphs.map((paragraph, paragraphIndex) => <p key={paragraphIndex}>{paragraph}</p>)}
          {section.bullets.length > 0 && (
            <ul>{section.bullets.map((bullet, bulletIndex) => <li key={bulletIndex}>{bullet}</li>)}</ul>
          )}
        </section>
      ))}
    </div>
  )
}

export function StoryboardInspector({ storyboard, selected, busy, onAction }: Props) {
  const action = storyboard.canInitialize
    ? { id: 'initialize' as const, label: '構成作成を開始' }
    : storyboard.canSubmit
      ? { id: 'submit' as const, label: '構成案を提出' }
      : storyboard.canApprove
        ? { id: 'approve' as const, label: 'この構成を承認' }
        : null
  return (
    <aside className="inspector storyboard-inspector">
      <div className="panel-heading"><span>Inspector</span></div>
      <section className="inspector-section selection-card">
        <div className="section-kicker">選択中</div>
        <strong>{selected ? `${String(selected.number).padStart(2, '0')} ${selected.title}` : 'スライド未選択'}</strong>
        {selected?.points.map((point, index) => <p key={index}>・{point}</p>)}
        {selected?.visual && (
          <div className="visual-detail">
            <strong>{selected.visual.recommended ? 'ビジュアル推奨' : 'ビジュアルなし'}</strong>
            <p>{selected.visual.intent ?? selected.visual.purpose ?? selected.visual.type}</p>
          </div>
        )}
      </section>
      <details className="inspector-section storyboard-details" open>
        <summary>{storyboard.request.title}</summary>
        <DocumentView document={storyboard.request} />
      </details>
      <details className="inspector-section storyboard-details">
        <summary>{storyboard.explanationPolicy.title}</summary>
        <DocumentView document={storyboard.explanationPolicy} />
      </details>
      <details className="inspector-section storyboard-details">
        <summary>{storyboard.storyOutline.title}</summary>
        <DocumentView document={storyboard.storyOutline} />
      </details>
      <details className="inspector-section storyboard-details">
        <summary>{storyboard.slidePlan.title}</summary>
        <DocumentView document={storyboard.slidePlan} />
      </details>
      <section className="inspector-section storyboard-action">
        <div className="section-kicker">次の操作</div>
        <p>{storyboard.nextActionLabel}</p>
        {action && (
          <button className="primary-button" type="button" disabled={busy} onClick={() => onAction(action.id)}>
            {action.label}
          </button>
        )}
      </section>
    </aside>
  )
}
