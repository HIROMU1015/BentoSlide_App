import { useCallback, useEffect, useRef } from 'react'
import type { AppState, BentoIntegration, HtmlGenerationStatus, HtmlReview, HtmlView, Storyboard } from '../types'
import { StoryboardCanvas } from './StoryboardCanvas'

type Props = {
  state: AppState
  review: HtmlReview | null
  htmlGeneration: HtmlGenerationStatus | null
  htmlView: HtmlView
  selectedSlide: string | null
  onViewChange: (view: HtmlView) => void
  bento: BentoIntegration | null
  storyboard: Storyboard | null
  onStoryboardSelect: (slideId: string) => void
  onStoryboardViewChange: (view: 'current' | 'candidate') => void
  transitioning?: boolean
}

function HtmlCanvas({ review, htmlGeneration, htmlView, selectedSlide, onViewChange }: Props) {
  const frame = useRef<HTMLIFrameElement>(null)
  const initialCandidate = htmlGeneration?.candidate ?? null
  const source = initialCandidate?.candidateHtmlUrl
    ?? (htmlView === 'candidate' ? review?.candidateHtmlUrl : review?.currentHtmlUrl)

  const syncFrame = useCallback(() => {
    const iframe = frame.current
    const document = iframe?.contentDocument
    if (!iframe || !document) return
    const firstSlide = document.querySelector<HTMLElement>('[data-slide-id]')
    if (firstSlide) {
      const width = firstSlide.offsetWidth || 1280
      const scale = Math.min(1, Math.max(0.35, (iframe.clientWidth - 32) / width))
      ;(document.body.style as CSSStyleDeclaration & { zoom: string }).zoom = String(scale)
      document.body.style.margin = '16px auto'
    }
    if (selectedSlide) {
      const escaped = CSS.escape(selectedSlide)
      document.querySelector<HTMLElement>(`[data-slide-id="${escaped}"]`)?.scrollIntoView({ block: 'start' })
    }
  }, [selectedSlide])

  useEffect(() => {
    syncFrame()
  }, [syncFrame, htmlView])

  useEffect(() => {
    window.addEventListener('resize', syncFrame)
    return () => window.removeEventListener('resize', syncFrame)
  }, [syncFrame])

  if (!source) {
    return (
      <EmptyCanvas
        title="HTMLを準備しています"
        detail="構成案は承認済みです。HTMLデザインが作成されると、ここにプレビューが表示されます。"
      />
    )
  }

  return (
    <section className={`main-canvas html-canvas view-${htmlView}`}>
      <div className="canvas-toolbar">
        {!initialCandidate && <div className="view-switch" role="group" aria-label="表示する案">
          <button
            type="button"
            className={htmlView === 'current' ? 'current is-active' : 'current'}
            onClick={() => onViewChange('current')}
          >
            現在案
          </button>
          {review?.candidateHtmlUrl && (
            <button
              type="button"
              className={htmlView === 'candidate' ? 'candidate is-active' : 'candidate'}
              onClick={() => onViewChange('candidate')}
            >
              変更案
            </button>
          )}
        </div>}
        <span className={`view-label ${initialCandidate ? 'candidate' : htmlView}`}>
          {initialCandidate ? '生成されたHTML案を表示中' : htmlView === 'current' ? '現在案を表示中' : '変更案を表示中'}
        </span>
        {review?.fullPreviewUrl && (
          <a className="text-link" href={review.fullPreviewUrl} target="_blank" rel="noreferrer">
            詳細プレビュー
          </a>
        )}
      </div>
      <div className="frame-shell">
        <iframe
          ref={frame}
          key={source}
          title={initialCandidate ? '生成されたHTML案のプレビュー' : htmlView === 'current' ? '現在案のHTMLプレビュー' : '変更案のHTMLプレビュー'}
          src={source}
          sandbox="allow-same-origin"
          onLoad={syncFrame}
        />
      </div>
    </section>
  )
}

function EmptyCanvas({ title, detail }: { title: string; detail: string }) {
  return (
    <section className="main-canvas empty-canvas">
      <div className="empty-icon" aria-hidden="true">▤</div>
      <h2>{title}</h2>
      <p>{detail}</p>
    </section>
  )
}

export function MainCanvas(props: Props) {
  const { state, bento, storyboard, transitioning } = props
  if (state.mode === 'storyboard' && storyboard) {
    return (
      <StoryboardCanvas
        storyboard={storyboard}
        selectedSlide={props.selectedSlide}
        onSelect={props.onStoryboardSelect}
        onViewChange={props.onStoryboardViewChange}
      />
    )
  }
  if (state.mode === 'html-design') return <HtmlCanvas {...props} />
  if (transitioning && (state.mode === 'bento-edit' || state.mode === 'final-edit')) {
    return <EmptyCanvas title="BentoSlideを更新しています" detail="編集画面を安全に切り替えています。" />
  }
  const editorUrl = bento?.available ? bento.editorUrl : state.bentoEditorUrl
  if ((state.mode === 'bento-edit' || state.mode === 'final-edit') && editorUrl) {
    return (
      <section className="main-canvas bento-canvas">
        <div className="canvas-toolbar">
          <span className="view-label bento">Bento編集</span>
          <a className="text-link" href={editorUrl} target="_blank" rel="noreferrer">別画面で開く</a>
        </div>
        <div className="frame-shell">
          <iframe title="Bento編集画面" src={editorUrl} allow="clipboard-write" />
        </div>
      </section>
    )
  }
  const messages: Record<string, [string, string]> = {
    storyboard: ['構成を準備しています', '資料と構成案が揃うと、ここで全体を確認できます。'],
    converting: ['BentoSlideへ変換します', '承認済みのHTMLを既存エンジンが安全に変換・検証します。'],
    'bento-edit': ['Bento編集を準備しています', bento?.message ?? '変換結果の検証が終わると編集画面が利用できます。'],
    complete: ['資料は完成しています', '完成版は既存のviewerから確認できます。'],
    blocked: ['処理を停止しています', state.nextActionLabel],
  }
  const [title, detail] = messages[state.mode] ?? ['準備中', state.nextActionLabel]
  return <EmptyCanvas title={title} detail={detail} />
}
