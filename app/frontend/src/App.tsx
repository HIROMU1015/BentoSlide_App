import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  applyHtmlChange,
  approveHtmlDeck,
  getConversionStatus,
  getLifecycleStatus,
  loadAppData,
  startConversion,
  startLifecycleAction,
} from './api/client'
import { Inspector } from './components/Inspector'
import { MainCanvas } from './components/MainCanvas'
import { SlideNavigator } from './components/SlideNavigator'
import { initialReviewMarks, reviewedSlideIds } from './state/reviewState'
import type {
  AppState,
  BentoIntegration,
  ConversionStatus,
  HtmlReview,
  HtmlView,
  LifecycleAction,
  LifecycleStatus,
  ProjectResponse,
  ReviewMark,
  ReviewMarks,
  SlideItem,
} from './types'

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
  const [bento, setBento] = useState<BentoIntegration | null>(null)
  const [conversion, setConversion] = useState<ConversionStatus | null>(null)
  const [lifecycle, setLifecycle] = useState<LifecycleStatus | null>(null)
  const [selectedSlide, setSelectedSlide] = useState<string | null>(null)
  const [selectedElement] = useState<string | null>(null)
  const [currentMode, setCurrentMode] = useState<AppState['mode'] | null>(null)
  const [currentProposal, setCurrentProposal] = useState<HtmlReview['proposal']>(null)
  const [htmlView, setHtmlView] = useState<HtmlView>('current')
  const [marks, setMarks] = useState<ReviewMarks>({})
  const [busy, setBusy] = useState(false)
  const [lifecycleRequestBusy, setLifecycleRequestBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const data = await loadAppData(htmlView)
      setProject(data.project)
      setState(data.state)
      setSlides(data.slides)
      setReview(data.review)
      setBento(data.bento)
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
    let active = true
    void getConversionStatus()
      .then((result) => { if (active) setConversion(result) })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : String(reason)) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    void getLifecycleStatus()
      .then((result) => { if (active) setLifecycle(result) })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : String(reason)) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (conversion?.status !== 'running') return
    let active = true
    const poll = async () => {
      try {
        const result = await getConversionStatus()
        if (!active) return
        setConversion(result)
        if (result.status === 'succeeded') {
          setNotice('BentoSlideへの変換が完了しました。')
          await refresh()
        } else if (result.status === 'failed') {
          await refresh()
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [conversion?.status, refresh])

  useEffect(() => {
    if (lifecycle?.status !== 'running') return
    let active = true
    const poll = async () => {
      try {
        const result = await getLifecycleStatus()
        if (!active) return
        setLifecycle(result)
        if (result.status === 'succeeded') {
          setNotice(result.message)
          await refresh()
        } else if (result.status === 'failed') {
          await refresh()
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [lifecycle?.status, refresh])

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

  const handleConversion = (retry: boolean) => {
    const prompt = retry
      ? 'BentoSlideへの変換を再試行しますか？'
      : '承認済みHTMLをBentoSlideへ変換しますか？'
    if (!window.confirm(prompt)) return
    setBusy(true)
    setError(null)
    setNotice(null)
    void startConversion()
      .then(setConversion)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setBusy(false))
  }

  const handleLifecycleAction = (action: LifecycleAction, retry = false) => {
    const prompts: Record<LifecycleAction, string> = {
      'content-review': retry
        ? '内容確認画面の準備を再試行しますか？'
        : '現在の編集内容を検証し、内容確認へ進みますか？',
      'content-approve': retry
        ? '内容承認と最終調整の準備を再試行しますか？'
        : '現在の内容を承認し、最終調整へ進みますか？',
      'final-approve': retry
        ? '最終承認と完成処理を再試行しますか？'
        : '現在の最終版を承認して完成させますか？',
      'final-reopen': retry
        ? '最終調整の再開をもう一度試しますか？'
        : '最終承認を解除して最終調整を再開しますか？',
      'final-open': '完成版をこのPCで開きますか？',
    }
    if (!window.confirm(prompts[action])) return
    setLifecycleRequestBusy(true)
    setError(null)
    setNotice(null)
    void startLifecycleAction(action)
      .then(setLifecycle)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLifecycleRequestBusy(false))
  }

  const handleMark = (slideId: string, mark: ReviewMark) => {
    setMarks((current) => ({ ...current, [slideId]: mark }))
  }

  const handleAiAction = (action: string) => {
    setNotice(`${action}: AI integration is not enabled in this prototype.`)
  }

  const lifecycleTransitioning = lifecycleRequestBusy || lifecycle?.status === 'running'
  const processing = busy || lifecycleTransitioning || conversion?.status === 'running'

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
          <span className="save-state"><i />{processing ? '処理中' : '最新の状態'}</span>
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
          bento={bento}
          transitioning={lifecycleTransitioning}
        />
        <Inspector
          state={state}
          selected={selected}
          selectedElement={selectedElement}
          review={review}
          marks={marks}
          busy={processing}
          conversion={conversion}
          lifecycle={lifecycle}
          onSelectSlide={setSelectedSlide}
          onMark={handleMark}
          onApply={handleApply}
          onRetryCheck={handleRetryCheck}
          onApproveDeck={handleApproveDeck}
          onStartConversion={() => handleConversion(false)}
          onRetryConversion={() => handleConversion(true)}
          onLifecycleAction={handleLifecycleAction}
          onAiAction={handleAiAction}
        />
      </div>

      <footer className="workflow-bar">
        <div><span className="workflow-dot" />{state.statusLabel}</div>
        <p>{state.nextActionLabel}</p>
        <button type="button" onClick={() => void refresh()} disabled={processing}>状態を更新</button>
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
