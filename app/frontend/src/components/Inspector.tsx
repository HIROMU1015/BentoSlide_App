import type {
  AiProposalInput, AiStatus, AppState, ConversionStatus, HtmlReview, LifecycleAction, LifecycleStatus,
  ReviewMark, ReviewMarks, SlideItem, Storyboard, StoryboardAction, StoryboardSlide,
} from '../types'
import { BentoLifecyclePanel } from './BentoLifecyclePanel'
import { ConversionPanel } from './ConversionPanel'
import { HtmlReviewPanel } from './HtmlReviewPanel'
import { AiActionsPanel } from './AiActionsPanel'
import { StoryboardInspector } from './StoryboardInspector'

type Props = {
  state: AppState
  selected: SlideItem | null
  selectedElement: string | null
  review: HtmlReview | null
  marks: ReviewMarks
  busy: boolean
  conversion: ConversionStatus | null
  lifecycle: LifecycleStatus | null
  ai: AiStatus | null
  storyboard: Storyboard | null
  selectedStoryboardSlide: StoryboardSlide | null
  onSelectSlide: (slideId: string) => void
  onMark: (slideId: string, mark: ReviewMark) => void
  onApply: () => void
  onRetryCheck: () => void
  onApproveDeck: () => void
  onStartConversion: () => void
  onRetryConversion: () => void
  onLifecycleAction: (action: LifecycleAction, retry?: boolean) => void
  onStartAiProposal: (input: AiProposalInput) => void
  onRetryAiProposal: (input: AiProposalInput) => void
  onStoryboardAction: (action: StoryboardAction) => void
}

export function Inspector(props: Props) {
  const { state, selected, selectedElement, review } = props
  if (state.mode === 'storyboard' && props.storyboard) {
    return (
      <StoryboardInspector
        storyboard={props.storyboard}
        selected={props.selectedStoryboardSlide}
        busy={props.busy}
        onAction={props.onStoryboardAction}
      />
    )
  }
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

      {state.mode === 'html-design' && state.htmlAvailable && review && (
        <AiActionsPanel
          selected={selected}
          status={props.ai}
          disabled={props.busy}
          hasProposal={Boolean(review.proposal && review.proposal.status !== 'applied')}
          onStart={props.onStartAiProposal}
          onRetry={props.onRetryAiProposal}
        />
      )}
    </aside>
  )
}
