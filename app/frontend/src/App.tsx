import { useCallback, useEffect, useMemo, useState } from 'react'
import { applyHtmlChange, approveHtmlDeck, loadAppData } from './api/client'
import { Inspector } from './components/Inspector'
import { MainCanvas } from './components/MainCanvas'
import { SlideNavigator } from './components/SlideNavigator'
import { initialReviewMarks, reviewedSlideIds } from './state/reviewState'
import type { AppState, HtmlReview, HtmlView, ProjectResponse, ReviewMark, ReviewMarks, SlideItem } from './types'

const modeLabels: Record<string, string> = {
  storyboard: 'Storyboard',
  'html-design': 'HTML Design',
  converting: 'Converting',
  'bento-edit': 'Bento Edit',
  'final-edit': 'Final Edit',
  complete: 'Complete',
  blocked: '確認が必要',
}

export default function App() {
  const [project, setProject] = useState<ProjectResponse | null>(null)
  const [state, setState] = useState<AppState | null>(null)
  const [slides, setSlides] = useState<SlideItem[]>([])
  const [review, setReview] = useState<HtmlReview | null>(null)
  const [selectedSlide, setSelectedSlide] = useState<string | null>(null)
  const [selectedElement] = useState<string | null>(null)
  const [currentMode, setCurrentMode] = useState<AppState['mode'] | null>(null)
  const [currentProposal, setCurrentProposal] = useState<HtmlReview['proposal']>(null)
  const [htmlView, setHtmlView] = useState<HtmlView>('current')
  const [marks, setMarks] = useState<ReviewMarks>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const data = await loadAppData(htmlView)
      setProject(data.project)
      setState(data.state)
      setSlides(data.slides)
      setReview(data.review)
      setCurrentMode(data.state.mode)
      setCurrentProposal(data.review?.proposal ?? null)
      setError(null)
      setSelectedSlide((selected) => (
        selected && data.slides.some((slide) => slide.id === selected) ? selected : data.slides[0]?.id ?? null
      ))
      setHtmlView(data.slideView)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [htmlView])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    setMarks(initialReviewMarks(review?.proposal ?? null))
  }, [review?.actionToken])

  const selected = useMemo(
    () => slides.find((slide) => slide.id === selectedSlide) ?? null,
    [slides, selectedSlide],
  )

  const runAction = useCallback(async (action: () => Promise<unknown>, success: string) => {
    setBusy(true)
    setNotice(null)
    try {
      await action()
      setNotice(success)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }, [refresh])

  const handleApply = () => {
    if (!review?.proposal) return
    if (!window.confirm('確認済みの変更案全体を現在案へ反映し、自動検証を実行しますか？')) return
    const reviewed = reviewedSlideIds(review.proposal, marks)
    void runAction(() => applyHtmlChange(review, reviewed), '変更案を反映し、自動検証が完了しました。')
  }

  const handleRetryCheck = () => {
    if (!review) return
    void runAction(() => applyHtmlChange(review, []), '自動検証が完了しました。')
  }

  const handleApproveDeck = () => {
    if (!review || !window.confirm('このHTML全体を確定してBentoSlideへ進みますか？')) return
    void runAction(() => approveHtmlDeck(review), 'HTML全体を確定しました。')
  }

  const handleMark = (slideId: string, mark: ReviewMark) => {
    setMarks((current) => ({ ...current, [slideId]: mark }))
  }

  const handleAiAction = (action: string) => {
    setNotice(`${action}: AI integration is not enabled in this prototype.`)
  }

  if (!project || !state) {
    return (
      <main className="loading-screen">
        <div className="brand-mark">B</div>
        <p>{error ? `起動できませんでした: ${error}` : 'BentoSlideを読み込んでいます…'}</p>
        {error && <button type="button" onClick={() => void refresh()}>再読み込み</button>}
      </main>
    )
  }

  return (
    <div className="app-shell" data-mode={currentMode ?? state.mode} data-has-proposal={Boolean(currentProposal)}>
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">B</div>
          <div><strong>BentoSlide</strong><small>{project.project.title}</small></div>
        </div>
        <div className="header-status">
          <span className={`mode-pill mode-${state.mode}`}>{modeLabels[state.mode]}</span>
          <span className="save-state"><i />{busy ? '処理中' : '最新の状態'}</span>
        </div>
      </header>

      <div className="workspace-grid">
        <SlideNavigator slides={slides} selectedSlide={selectedSlide} onSelect={setSelectedSlide} />
        <MainCanvas
          state={state}
          review={review}
          htmlView={htmlView}
          selectedSlide={selectedSlide}
          onViewChange={setHtmlView}
        />
        <Inspector
          state={state}
          selected={selected}
          selectedElement={selectedElement}
          review={review}
          marks={marks}
          busy={busy}
          onSelectSlide={setSelectedSlide}
          onMark={handleMark}
          onApply={handleApply}
          onRetryCheck={handleRetryCheck}
          onApproveDeck={handleApproveDeck}
          onAiAction={handleAiAction}
        />
      </div>

      <footer className="workflow-bar">
        <div><span className="workflow-dot" />{state.statusLabel}</div>
        <p>{state.nextActionLabel}</p>
        <button type="button" onClick={() => void refresh()} disabled={busy}>状態を更新</button>
      </footer>

      {(notice || error) && (
        <div className={error ? 'toast is-error' : 'toast'} role="status">
          <span>{error ?? notice}</span>
          <button type="button" onClick={() => { setError(null); setNotice(null) }} aria-label="閉じる">×</button>
        </div>
      )}
    </div>
  )
}
