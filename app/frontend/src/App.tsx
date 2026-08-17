import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  applyHtmlChange,
  approveHtmlDeck,
  getConversionStatus,
  getAiStatus,
  getLifecycleStatus,
  loadAppData,
  startConversion,
  startAiProposal,
  startLifecycleAction,
} from './api/client'
import { Inspector } from './components/Inspector'
import { MainCanvas } from './components/MainCanvas'
import { SlideNavigator } from './components/SlideNavigator'
import { initialReviewMarks, reviewedSlideIds } from './state/reviewState'
import type {
  AppState,
  AiProposalInput,
  AiStatus,
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

const POLL_DELAY_MS = 250

export default function App() {
  const [project, setProject] = useState<ProjectResponse | null>(null)
  const [state, setState] = useState<AppState | null>(null)
  const [slides, setSlides] = useState<SlideItem[]>([])
  const [review, setReview] = useState<HtmlReview | null>(null)
  const [bento, setBento] = useState<BentoIntegration | null>(null)
  const [conversion, setConversion] = useState<ConversionStatus | null>(null)
  const [lifecycle, setLifecycle] = useState<LifecycleStatus | null>(null)
  const [ai, setAi] = useState<AiStatus | null>(null)
  const [selectedSlide, setSelectedSlide] = useState<string | null>(null)
  const [selectedElement] = useState<string | null>(null)
  const [currentMode, setCurrentMode] = useState<AppState['mode'] | null>(null)
  const [currentProposal, setCurrentProposal] = useState<HtmlReview['proposal']>(null)
  const [htmlView, setHtmlView] = useState<HtmlView>('current')
  const [marks, setMarks] = useState<ReviewMarks>({})
  const [busy, setBusy] = useState(false)
  const [lifecycleRequestBusy, setLifecycleRequestBusy] = useState(false)
  const [aiRequestBusy, setAiRequestBusy] = useState(false)
  const [conversionSettling, setConversionSettling] = useState(false)
  const [lifecycleSettling, setLifecycleSettling] = useState(false)
  const [aiSettling, setAiSettling] = useState(false)
  const [lastAiRequest, setLastAiRequest] = useState<AiProposalInput | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const mounted = useRef(true)
  const htmlViewRef = useRef<HtmlView>('current')
  const conversionGeneration = useRef(0)
  const lifecycleGeneration = useRef(0)
  const aiGeneration = useRef(0)
  const conversionTimer = useRef<number | null>(null)
  const lifecycleTimer = useRef<number | null>(null)
  const aiTimer = useRef<number | null>(null)

  const refresh = useCallback(async (requestedView?: HtmlView) => {
    try {
      const data = await loadAppData(requestedView ?? htmlViewRef.current)
      if (!mounted.current) return
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
      htmlViewRef.current = data.slideView
      return true
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason))
      return false
    }
  }, [])

  const chooseHtmlView = useCallback((view: HtmlView) => {
    htmlViewRef.current = view
    setHtmlView(view)
    void refresh(view)
  }, [refresh])

  const trackConversion = useCallback((initial: ConversionStatus) => {
    const generation = ++conversionGeneration.current
    if (conversionTimer.current !== null) window.clearTimeout(conversionTimer.current)

    const accept = async (result: ConversionStatus) => {
      if (!mounted.current || generation !== conversionGeneration.current) return
      if (result.status === 'running') {
        setConversionSettling(false)
        setConversion(result)
        conversionTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
        return
      }
      setConversionSettling(true)
      setConversion(result)
      if (result.status === 'succeeded') setNotice('BentoSlideへの変換が完了しました。')
      const refreshed = await refresh()
      if (!mounted.current || generation !== conversionGeneration.current) return
      if (!refreshed) {
        conversionTimer.current = window.setTimeout(() => void accept(result), POLL_DELAY_MS)
        return
      }
      setConversionSettling(false)
    }

    async function poll() {
      try {
        const result = await getConversionStatus()
        await accept(result)
      } catch (reason) {
        if (!mounted.current || generation !== conversionGeneration.current) return
        setError(reason instanceof Error ? reason.message : String(reason))
        conversionTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
      }
    }

    void accept(initial)
  }, [refresh])

  const trackLifecycle = useCallback((initial: LifecycleStatus) => {
    const generation = ++lifecycleGeneration.current
    if (lifecycleTimer.current !== null) window.clearTimeout(lifecycleTimer.current)

    const accept = async (result: LifecycleStatus) => {
      if (!mounted.current || generation !== lifecycleGeneration.current) return
      if (result.status === 'running') {
        setLifecycleSettling(false)
        setLifecycle(result)
        lifecycleTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
        return
      }
      setLifecycleSettling(true)
      setLifecycle(result)
      if (result.status === 'succeeded') setNotice(result.message)
      const refreshed = await refresh()
      if (!mounted.current || generation !== lifecycleGeneration.current) return
      if (!refreshed) {
        lifecycleTimer.current = window.setTimeout(() => void accept(result), POLL_DELAY_MS)
        return
      }
      setLifecycleSettling(false)
    }

    async function poll() {
      try {
        const result = await getLifecycleStatus()
        await accept(result)
      } catch (reason) {
        if (!mounted.current || generation !== lifecycleGeneration.current) return
        setError(reason instanceof Error ? reason.message : String(reason))
        lifecycleTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
      }
    }

    void accept(initial)
  }, [refresh])

  const trackAi = useCallback((initial: AiStatus) => {
    const generation = ++aiGeneration.current
    if (aiTimer.current !== null) window.clearTimeout(aiTimer.current)

    const accept = async (result: AiStatus) => {
      if (!mounted.current || generation !== aiGeneration.current) return
      if (result.status === 'running') {
        setAiSettling(false)
        setAi(result)
        aiTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
        return
      }
      setAiSettling(true)
      setAi(result)
      const refreshed = await refresh(result.status === 'succeeded' ? 'candidate' : undefined)
      if (!mounted.current || generation !== aiGeneration.current) return
      if (!refreshed) {
        aiTimer.current = window.setTimeout(() => void accept(result), POLL_DELAY_MS)
        return
      }
      if (result.status === 'succeeded') setNotice('AIの変更案を作成しました。現在案はまだ変更していません。')
      setAiSettling(false)
    }

    async function poll() {
      try {
        const result = await getAiStatus()
        await accept(result)
      } catch (reason) {
        if (!mounted.current || generation !== aiGeneration.current) return
        setError(reason instanceof Error ? reason.message : String(reason))
        aiTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
      }
    }

    void accept(initial)
  }, [refresh])

  useEffect(() => {
    mounted.current = true
    void refresh()
    void getConversionStatus()
      .then((result) => result.status === 'running' ? trackConversion(result) : setConversion(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
    void getLifecycleStatus()
      .then((result) => result.status === 'running' ? trackLifecycle(result) : setLifecycle(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
    void getAiStatus()
      .then((result) => result.status === 'running' ? trackAi(result) : setAi(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
    return () => {
      mounted.current = false
      conversionGeneration.current += 1
      lifecycleGeneration.current += 1
      aiGeneration.current += 1
      if (conversionTimer.current !== null) window.clearTimeout(conversionTimer.current)
      if (lifecycleTimer.current !== null) window.clearTimeout(lifecycleTimer.current)
      if (aiTimer.current !== null) window.clearTimeout(aiTimer.current)
    }
  }, [refresh, trackAi, trackConversion, trackLifecycle])

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
    setConversionSettling(true)
    setError(null)
    setNotice(null)
    void startConversion()
      .then(trackConversion)
      .catch((reason) => {
        setConversionSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
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
    setLifecycleSettling(true)
    setError(null)
    setNotice(null)
    void startLifecycleAction(action)
      .then(trackLifecycle)
      .catch((reason) => {
        setLifecycleSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setLifecycleRequestBusy(false))
  }

  const handleMark = (slideId: string, mark: ReviewMark) => {
    setMarks((current) => ({ ...current, [slideId]: mark }))
  }

  const handleAiProposal = (input: AiProposalInput) => {
    if (!window.confirm('現在案を変更せず、選択したスライドの確認用変更案をAIで作成しますか？')) return
    setLastAiRequest(input)
    setAiRequestBusy(true)
    setAiSettling(true)
    setError(null)
    setNotice(null)
    void startAiProposal(input)
      .then(trackAi)
      .catch((reason) => {
        setAiSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setAiRequestBusy(false))
  }

  const handleRetryAiProposal = (input: AiProposalInput) => {
    handleAiProposal(lastAiRequest ?? input)
  }

  const lifecycleTransitioning = lifecycleRequestBusy || lifecycle?.status === 'running' || lifecycleSettling
  const conversionTransitioning = conversion?.status === 'running' || conversionSettling
  const aiTransitioning = aiRequestBusy || ai?.status === 'running' || aiSettling
  const processing = busy || lifecycleTransitioning || conversionTransitioning || aiTransitioning

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
          onViewChange={chooseHtmlView}
          bento={bento}
          transitioning={lifecycleTransitioning || conversionTransitioning}
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
          ai={ai}
          onSelectSlide={setSelectedSlide}
          onMark={handleMark}
          onApply={handleApply}
          onRetryCheck={handleRetryCheck}
          onApproveDeck={handleApproveDeck}
          onStartConversion={() => handleConversion(false)}
          onRetryConversion={() => handleConversion(true)}
          onLifecycleAction={handleLifecycleAction}
          onStartAiProposal={handleAiProposal}
          onRetryAiProposal={handleRetryAiProposal}
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
