import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  applyHtmlChange,
  applyHtmlGeneration,
  applyPlanningAiProposal,
  approveHtmlDeck,
  cancelPlanningAiProposal,
  cancelHtmlGeneration,
  getConversionStatus,
  getAiStatus,
  getLifecycleStatus,
  getPlanningAiStatus,
  getHtmlGenerationStatus,
  getStoryboard,
  loadAppData,
  startConversion,
  startAiProposal,
  startLifecycleAction,
  startPlanningAiProposal,
  startHtmlGeneration,
  startStoryboardAction,
} from './api/client'
import { Inspector } from './components/Inspector'
import { MainCanvas } from './components/MainCanvas'
import { SlideNavigator } from './components/SlideNavigator'
import { StoryboardNavigator } from './components/StoryboardNavigator'
import { initialReviewMarks, reviewedSlideIds } from './state/reviewState'
import type {
  AppState,
  AiProposalInput,
  AiStatus,
  BentoIntegration,
  ConversionStatus,
  HtmlGenerationStatus,
  HtmlReview,
  HtmlView,
  LifecycleAction,
  LifecycleStatus,
  PlanningAiStatus,
  ProjectResponse,
  ReviewMark,
  ReviewMarks,
  SlideItem,
  Storyboard,
  StoryboardAction,
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
  const [storyboard, setStoryboard] = useState<Storyboard | null>(null)
  const [bento, setBento] = useState<BentoIntegration | null>(null)
  const [conversion, setConversion] = useState<ConversionStatus | null>(null)
  const [lifecycle, setLifecycle] = useState<LifecycleStatus | null>(null)
  const [ai, setAi] = useState<AiStatus | null>(null)
  const [planningAi, setPlanningAi] = useState<PlanningAiStatus | null>(null)
  const [htmlGeneration, setHtmlGeneration] = useState<HtmlGenerationStatus | null>(null)
  const [selectedSlide, setSelectedSlide] = useState<string | null>(null)
  const [selectedElement] = useState<string | null>(null)
  const [currentMode, setCurrentMode] = useState<AppState['mode'] | null>(null)
  const [currentProposal, setCurrentProposal] = useState<HtmlReview['proposal']>(null)
  const [htmlView, setHtmlView] = useState<HtmlView>('current')
  const [marks, setMarks] = useState<ReviewMarks>({})
  const [busy, setBusy] = useState(false)
  const [lifecycleRequestBusy, setLifecycleRequestBusy] = useState(false)
  const [aiRequestBusy, setAiRequestBusy] = useState(false)
  const [planningAiRequestBusy, setPlanningAiRequestBusy] = useState(false)
  const [htmlGenerationRequestBusy, setHtmlGenerationRequestBusy] = useState(false)
  const [conversionSettling, setConversionSettling] = useState(false)
  const [lifecycleSettling, setLifecycleSettling] = useState(false)
  const [aiSettling, setAiSettling] = useState(false)
  const [planningAiSettling, setPlanningAiSettling] = useState(false)
  const [htmlGenerationSettling, setHtmlGenerationSettling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const mounted = useRef(true)
  const htmlViewRef = useRef<HtmlView>('current')
  const storyboardViewRef = useRef<'current' | 'candidate'>('current')
  const conversionGeneration = useRef(0)
  const lifecycleGeneration = useRef(0)
  const aiGeneration = useRef(0)
  const planningAiGeneration = useRef(0)
  const htmlGenerationSequence = useRef(0)
  const conversionTimer = useRef<number | null>(null)
  const lifecycleTimer = useRef<number | null>(null)
  const aiTimer = useRef<number | null>(null)
  const planningAiTimer = useRef<number | null>(null)
  const htmlGenerationTimer = useRef<number | null>(null)

  const refresh = useCallback(async (requestedView?: HtmlView) => {
    try {
      const data = await loadAppData(requestedView ?? htmlViewRef.current)
      if (!mounted.current) return
      if (
        data.state.mode === 'storyboard'
        && storyboardViewRef.current === 'candidate'
        && data.storyboard?.proposal
      ) {
        const candidate = await getStoryboard('candidate')
        data.storyboard = candidate
        data.slides = candidate.sections.flatMap((section) => section.slides.map((slide) => ({
          id: slide.id, title: slide.title, number: slide.number, sectionTitle: section.title,
        })))
      } else if (data.state.mode === 'storyboard') {
        storyboardViewRef.current = 'current'
      }
      if (!mounted.current) return
      setProject(data.project)
      setState(data.state)
      setSlides(data.slides)
      setReview(data.review)
      setStoryboard(data.storyboard ?? null)
      setBento(data.bento)
      setHtmlGeneration(data.htmlGeneration ?? null)
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

  const chooseStoryboardView = useCallback((view: 'current' | 'candidate') => {
    storyboardViewRef.current = view
    setError(null)
    void getStoryboard(view)
      .then((value) => {
        if (!mounted.current) return
        const storyboardSlides = value.sections.flatMap((section) => section.slides.map((slide) => ({
          id: slide.id,
          title: slide.title,
          number: slide.number,
          sectionTitle: section.title,
        })))
        setStoryboard(value)
        setSlides(storyboardSlides)
        setSelectedSlide((selected) => (
          selected && storyboardSlides.some((slide) => slide.id === selected)
            ? selected : storyboardSlides[0]?.id ?? null
        ))
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [])

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

  const trackPlanningAi = useCallback((initial: PlanningAiStatus) => {
    const generation = ++planningAiGeneration.current
    if (planningAiTimer.current !== null) window.clearTimeout(planningAiTimer.current)

    const accept = async (result: PlanningAiStatus) => {
      if (!mounted.current || generation !== planningAiGeneration.current) return
      if (result.status === 'running') {
        setPlanningAiSettling(false)
        setPlanningAi(result)
        planningAiTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
        return
      }
      setPlanningAiSettling(true)
      setPlanningAi(result)
      const refreshed = await refresh()
      if (!mounted.current || generation !== planningAiGeneration.current) return
      if (!refreshed) {
        planningAiTimer.current = window.setTimeout(() => void accept(result), POLL_DELAY_MS)
        return
      }
      if (result.status === 'succeeded' && result.hasProposal) {
        setNotice('AIのPlanning Candidateを作成しました。現在案はまだ変更していません。')
        chooseStoryboardView('candidate')
      }
      setPlanningAiSettling(false)
    }

    async function poll() {
      try {
        const result = await getPlanningAiStatus()
        await accept(result)
      } catch (reason) {
        if (!mounted.current || generation !== planningAiGeneration.current) return
        setError(reason instanceof Error ? reason.message : String(reason))
        planningAiTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
      }
    }

    void accept(initial)
  }, [chooseStoryboardView, refresh])

  const trackHtmlGeneration = useCallback((initial: HtmlGenerationStatus) => {
    const sequence = ++htmlGenerationSequence.current
    if (htmlGenerationTimer.current !== null) window.clearTimeout(htmlGenerationTimer.current)

    const accept = async (result: HtmlGenerationStatus) => {
      if (!mounted.current || sequence !== htmlGenerationSequence.current) return
      if (result.status === 'running') {
        setHtmlGenerationSettling(false)
        setHtmlGeneration(result)
        htmlGenerationTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
        return
      }
      setHtmlGenerationSettling(true)
      setHtmlGeneration(result)
      const refreshed = await refresh(result.hasCandidate ? 'candidate' : undefined)
      if (!mounted.current || sequence !== htmlGenerationSequence.current) return
      if (!refreshed) {
        htmlGenerationTimer.current = window.setTimeout(() => void accept(result), POLL_DELAY_MS)
        return
      }
      if (result.status === 'succeeded' && result.hasCandidate) {
        setNotice('AIがHTML Candidateを生成しました。現在のHTMLはまだ作成していません。')
        htmlViewRef.current = 'candidate'
        setHtmlView('candidate')
      }
      setHtmlGenerationSettling(false)
    }

    async function poll() {
      try {
        await accept(await getHtmlGenerationStatus())
      } catch (reason) {
        if (!mounted.current || sequence !== htmlGenerationSequence.current) return
        setError(reason instanceof Error ? reason.message : String(reason))
        htmlGenerationTimer.current = window.setTimeout(() => void poll(), POLL_DELAY_MS)
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
    return () => {
      mounted.current = false
      conversionGeneration.current += 1
      lifecycleGeneration.current += 1
      aiGeneration.current += 1
      planningAiGeneration.current += 1
      htmlGenerationSequence.current += 1
      if (conversionTimer.current !== null) window.clearTimeout(conversionTimer.current)
      if (lifecycleTimer.current !== null) window.clearTimeout(lifecycleTimer.current)
      if (aiTimer.current !== null) window.clearTimeout(aiTimer.current)
      if (planningAiTimer.current !== null) window.clearTimeout(planningAiTimer.current)
      if (htmlGenerationTimer.current !== null) window.clearTimeout(htmlGenerationTimer.current)
    }
  }, [refresh, trackAi, trackConversion, trackLifecycle])

  useEffect(() => {
    if (state?.mode !== 'html-design' || !state.htmlAvailable) {
      aiGeneration.current += 1
      if (aiTimer.current !== null) window.clearTimeout(aiTimer.current)
      setAi(null)
      return
    }
    void getAiStatus()
      .then((result) => result.status === 'running' ? trackAi(result) : setAi(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [state?.mode, state?.htmlAvailable, trackAi])

  useEffect(() => {
    if (state?.mode !== 'storyboard' || state.stage !== 'planning') {
      planningAiGeneration.current += 1
      if (planningAiTimer.current !== null) window.clearTimeout(planningAiTimer.current)
      setPlanningAi(null)
      return
    }
    void getPlanningAiStatus()
      .then((result) => result.status === 'running' ? trackPlanningAi(result) : setPlanningAi(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [state?.mode, state?.stage, trackPlanningAi])

  useEffect(() => {
    if (state?.mode !== 'html-design' || state.stage !== 'html_authoring' || state.htmlAvailable) {
      htmlGenerationSequence.current += 1
      if (htmlGenerationTimer.current !== null) window.clearTimeout(htmlGenerationTimer.current)
      setHtmlGeneration(null)
      return
    }
    void getHtmlGenerationStatus()
      .then((result) => result.status === 'running' ? trackHtmlGeneration(result) : setHtmlGeneration(result))
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [state?.mode, state?.stage, state?.htmlAvailable, trackHtmlGeneration])

  useEffect(() => {
    setMarks(initialReviewMarks(review?.proposal ?? null))
  }, [review?.actionToken])

  const selected = useMemo(
    () => slides.find((slide) => slide.id === selectedSlide) ?? null,
    [slides, selectedSlide],
  )
  const selectedStoryboardSlide = useMemo(
    () => storyboard?.sections.flatMap((section) => section.slides).find((slide) => slide.id === selectedSlide) ?? null,
    [storyboard, selectedSlide],
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
    handleAiProposal(input)
  }

  const handlePlanningAiProposal = (instruction: string) => {
    if (!window.confirm('現在案を変更せず、Storyboard全体の確認用変更案をAIで作成しますか？')) return
    setPlanningAiRequestBusy(true)
    setPlanningAiSettling(true)
    setError(null)
    setNotice(null)
    void startPlanningAiProposal(instruction)
      .then(trackPlanningAi)
      .catch((reason) => {
        setPlanningAiSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setPlanningAiRequestBusy(false))
  }

  const handleApplyPlanningAi = () => {
    if (!storyboard?.proposal) return
    if (!window.confirm('このPlanning Candidate全体を現在案へ反映しますか？提出と承認は別途必要です。')) return
    void runAction(
      async () => {
        await applyPlanningAiProposal(storyboard)
        setPlanningAi(await getPlanningAiStatus())
      },
      'Planning Candidateを現在案へ反映しました。提出と承認はまだ行っていません。',
    )
  }

  const handleCancelPlanningAi = () => {
    if (!storyboard?.proposal) return
    if (!window.confirm('このPlanning Candidateを破棄しますか？現在案は変更されません。')) return
    void runAction(async () => {
      await cancelPlanningAiProposal(storyboard)
      setPlanningAi(await getPlanningAiStatus())
    }, 'Planning Candidateを破棄しました。')
  }

  const handleStartHtmlGeneration = (instruction: string, retry = false) => {
    const prompt = retry
      ? '承認済み構成からHTML案の生成を再試行しますか？'
      : '承認済み構成から確認用のHTML案をAIで生成しますか？'
    if (!window.confirm(prompt)) return
    setHtmlGenerationRequestBusy(true)
    setHtmlGenerationSettling(true)
    setError(null)
    setNotice(null)
    void startHtmlGeneration(instruction)
      .then(trackHtmlGeneration)
      .catch((reason) => {
        setHtmlGenerationSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setHtmlGenerationRequestBusy(false))
  }

  const handleApplyHtmlGeneration = () => {
    if (!htmlGeneration?.candidate) return
    if (!window.confirm('このHTML案をcanonical HTMLとして採用し、HTML全体の確認へ進みますか？')) return
    void runAction(
      () => applyHtmlGeneration(htmlGeneration),
      'HTML案を採用し、HTML全体の確認へ進みました。',
    )
  }

  const handleCancelHtmlGeneration = () => {
    if (!htmlGeneration?.candidate) return
    if (!window.confirm('このHTML案を破棄しますか？canonical HTMLは変更されません。')) return
    void runAction(
      () => cancelHtmlGeneration(htmlGeneration),
      'HTML Candidateを破棄しました。',
    )
  }

  const handleRegenerateHtml = (instruction: string) => {
    if (!htmlGeneration?.candidate) return
    if (!window.confirm('現在のHTML案を破棄し、承認済み構成から再生成しますか？')) return
    setHtmlGenerationRequestBusy(true)
    setHtmlGenerationSettling(true)
    setError(null)
    setNotice(null)
    void cancelHtmlGeneration(htmlGeneration)
      .then(() => startHtmlGeneration(instruction))
      .then(trackHtmlGeneration)
      .catch((reason) => {
        setHtmlGenerationSettling(false)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setHtmlGenerationRequestBusy(false))
  }

  const handleStoryboardAction = (action: StoryboardAction) => {
    if (!storyboard || busy) return
    const prompts: Record<StoryboardAction, string> = {
      initialize: '資料を確認して構成作成を開始しますか？',
      submit: '現在の構成案を確認待ちとして提出しますか？',
      approve: 'この構成を承認してHTML制作へ進みますか？',
    }
    const successes: Record<StoryboardAction, string> = {
      initialize: '構成作成を開始しました。',
      submit: '構成案を確認待ちとして提出しました。',
      approve: '構成案を承認し、HTML制作へ進みました。',
    }
    if (!window.confirm(prompts[action])) return
    setError(null)
    void runAction(() => startStoryboardAction(action, storyboard), successes[action])
  }

  const lifecycleTransitioning = lifecycleRequestBusy || lifecycle?.status === 'running' || lifecycleSettling
  const conversionTransitioning = conversion?.status === 'running' || conversionSettling
  const aiTransitioning = aiRequestBusy || ai?.status === 'running' || aiSettling
  const planningAiTransitioning = planningAiRequestBusy || planningAi?.status === 'running' || planningAiSettling
  const htmlGenerationTransitioning = htmlGenerationRequestBusy
    || htmlGeneration?.status === 'running'
    || htmlGenerationSettling
  const processing = busy || lifecycleTransitioning || conversionTransitioning || aiTransitioning
    || planningAiTransitioning || htmlGenerationTransitioning

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
    <div className="app-shell" data-mode={currentMode ?? state.mode} data-has-proposal={Boolean(currentProposal || storyboard?.proposal || htmlGeneration?.candidate)}>
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
        {state.mode === 'storyboard' && storyboard
          ? <StoryboardNavigator storyboard={storyboard} selectedSlide={selectedSlide} onSelect={setSelectedSlide} />
          : <SlideNavigator slides={slides} selectedSlide={selectedSlide} onSelect={setSelectedSlide} />}
        <MainCanvas
          state={state}
          review={review}
          htmlGeneration={htmlGeneration}
          htmlView={htmlView}
          selectedSlide={selectedSlide}
          onViewChange={chooseHtmlView}
          bento={bento}
          storyboard={storyboard}
          onStoryboardSelect={setSelectedSlide}
          onStoryboardViewChange={chooseStoryboardView}
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
          planningAi={planningAi}
          htmlGeneration={htmlGeneration}
          storyboard={storyboard}
          selectedStoryboardSlide={selectedStoryboardSlide}
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
          onStoryboardAction={handleStoryboardAction}
          onStartPlanningAi={handlePlanningAiProposal}
          onRetryPlanningAi={handlePlanningAiProposal}
          onApplyPlanningAi={handleApplyPlanningAi}
          onCancelPlanningAi={handleCancelPlanningAi}
          onStartHtmlGeneration={(instruction) => handleStartHtmlGeneration(instruction, false)}
          onRetryHtmlGeneration={(instruction) => handleStartHtmlGeneration(instruction, true)}
          onRegenerateHtml={handleRegenerateHtml}
          onApplyHtmlGeneration={handleApplyHtmlGeneration}
          onCancelHtmlGeneration={handleCancelHtmlGeneration}
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
