import type { HtmlReview, ReviewMark, ReviewMarks } from '../types'
import { canApplyProposal } from '../state/reviewState'

const impactLabels = {
  requested: '依頼対象',
  related: '関連確認',
  changed: '変更あり',
  added: '追加',
  removed: '削除',
  review: '再確認',
}

type Props = {
  review: HtmlReview
  marks: ReviewMarks
  busy: boolean
  selectedSlide: string | null
  onSelectSlide: (slideId: string) => void
  onMark: (slideId: string, mark: ReviewMark) => void
  onApply: () => void
  onRetryCheck: () => void
  onApproveDeck: () => void
}

export function HtmlReviewPanel({
  review,
  marks,
  busy,
  selectedSlide,
  onSelectSlide,
  onMark,
  onApply,
  onRetryCheck,
  onApproveDeck,
}: Props) {
  const proposal = review.proposal
  if (!proposal) {
    return (
      <section className="inspector-section review-panel">
        <div className="section-kicker">HTML Review</div>
        <h3>資料全体の確認</h3>
        <p className="muted">変更案はありません。現在のHTML全体を確認して次へ進めます。</p>
        <button className="primary-button" type="button" disabled={!review.canApproveDeck || busy} onClick={onApproveDeck}>
          このHTML全体でBentoSlideへ進む
        </button>
      </section>
    )
  }

  const reviewed = proposal.affectedSlides.filter((slide) => marks[slide.id] === 'reviewed').length
  const activeReview = proposal.status !== 'applied'
  return (
    <section className="inspector-section review-panel">
      <div className="section-kicker">変更案 · {proposal.scope}</div>
      <h3>変更すること</h3>
      <p>{proposal.summary}</p>
      <h3>他への影響</h3>
      <p>{proposal.impactSummary}</p>

      {activeReview ? (
        <>
          <div className="review-progress">
            <span>確認が必要</span>
            <strong>{reviewed} / {proposal.affectedSlides.length}</strong>
          </div>
          <ul className="review-list">
            {proposal.affectedSlides.map((slide) => {
              const mark = marks[slide.id] ?? 'pending'
              return (
                <li key={slide.id} className={selectedSlide === slide.id ? 'is-selected' : ''}>
                  <button className="review-slide-copy" type="button" onClick={() => onSelectSlide(slide.id)}>
                    <span>{slide.number ? String(slide.number).padStart(2, '0') : '—'}</span>
                    <strong>{slide.title}</strong>
                    <small className={`impact ${slide.impact}`}>{impactLabels[slide.impact]}</small>
                  </button>
                  <div className="review-mark-buttons" role="group" aria-label={`${slide.title}の確認結果`}>
                    <button
                      type="button"
                      className={mark === 'reviewed' ? 'reviewed is-active' : 'reviewed'}
                      onClick={() => onMark(slide.id, 'reviewed')}
                    >確認済み</button>
                    <button
                      type="button"
                      className={mark === 'needs-work' ? 'needs-work is-active' : 'needs-work'}
                      onClick={() => onMark(slide.id, 'needs-work')}
                    >要修正</button>
                  </div>
                </li>
              )
            })}
          </ul>
          <button className="primary-button" type="button" disabled={!canApplyProposal(proposal, marks) || busy} onClick={onApply}>
            この変更案全体を反映
          </button>
        </>
      ) : proposal.postApplyReviewStatus === 'checked' ? (
        <div className="success-card">
          <strong>自動検証が完了しました</strong>
          <p>更新後のHTML全体を確定できます。</p>
          <button className="primary-button" type="button" disabled={!review.canApproveDeck || busy} onClick={onApproveDeck}>
            このHTML全体でBentoSlideへ進む
          </button>
        </div>
      ) : (
        <div className="warning-card">
          <strong>変更案は反映済みです</strong>
          <p>影響するスライドの自動検証を完了してください。</p>
          <button className="primary-button" type="button" disabled={busy} onClick={onRetryCheck}>自動検証を再実行</button>
        </div>
      )}
    </section>
  )
}
