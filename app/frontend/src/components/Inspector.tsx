import type {
  AppState, ConversionStatus, HtmlReview, LifecycleAction, LifecycleStatus,
  ReviewMark, ReviewMarks, SlideItem,
} from '../types'
import { BentoLifecyclePanel } from './BentoLifecyclePanel'
import { ConversionPanel } from './ConversionPanel'
import { HtmlReviewPanel } from './HtmlReviewPanel'

type Props = {
  state: AppState
  selected: SlideItem | null
  selectedElement: string | null
  review: HtmlReview | null
  marks: ReviewMarks
  busy: boolean
  conversion: ConversionStatus | null
  lifecycle: LifecycleStatus | null
  onSelectSlide: (slideId: string) => void
  onMark: (slideId: string, mark: ReviewMark) => void
  onApply: () => void
  onRetryCheck: () => void
  onApproveDeck: () => void
  onStartConversion: () => void
  onRetryConversion: () => void
  onLifecycleAction: (action: LifecycleAction, retry?: boolean) => void
  onAiAction: (action: string) => void
}

export function Inspector(props: Props) {
  const { state, selected, selectedElement, review, onAiAction } = props
  return (
    <aside className="inspector">
      <div className="panel-heading"><span>Inspector</span></div>
      <section className="inspector-section selection-card">
        <div className="section-kicker">選択中</div>
        <strong>{selected ? `${String(selected.number).padStart(2, '0')} ${selected.title}` : 'スライド未選択'}</strong>
        <small>{selectedElement ? `要素: ${selectedElement}` : 'スライド全体'}</small>
      </section>

      {state.mode === 'html-design' && review && (
        <HtmlReviewPanel {...props} review={review} selectedSlide={selected?.id ?? null} />
      )}

      {(state.canConvert || props.conversion?.status === 'running' || props.conversion?.status === 'failed') && (
        <ConversionPanel
          status={props.conversion}
          busy={props.busy}
          canStart={state.canConvert}
          onStart={props.onStartConversion}
          onRetry={props.onRetryConversion}
        />
      )}

      {['bento_authoring', 'content_review', 'bento_finalization', 'complete'].includes(state.stage) && (
        <BentoLifecyclePanel
          state={state}
          status={props.lifecycle}
          busy={props.busy}
          onAction={props.onLifecycleAction}
        />
      )}

      <section className="inspector-section ai-actions">
        <div className="section-kicker">AI Actions</div>
        <h3>選択したスライドを変更</h3>
        <div className="action-grid">
          {['構成を改善', '図を追加', '短くする', '自由に変更'].map((action) => (
            <button key={action} type="button" onClick={() => onAiAction(action)} disabled={!selected}>{action}</button>
          ))}
        </div>
        <p className="prototype-note">AI連携は次のPhaseで有効になります。</p>
      </section>
    </aside>
  )
}
