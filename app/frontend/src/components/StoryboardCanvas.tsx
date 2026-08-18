import type { Storyboard } from '../types'

type Props = {
  storyboard: Storyboard
  selectedSlide: string | null
  onSelect: (slideId: string) => void
}

export function StoryboardCanvas({ storyboard, selectedSlide, onSelect }: Props) {
  const hasSlides = storyboard.sections.some((section) => section.slides.length > 0)
  return (
    <section className="main-canvas storyboard-canvas" aria-label="Storyboard確認">
      <div className="canvas-toolbar">
        <strong>構成案</strong>
        <span className="view-label">読み取り専用</span>
      </div>
      <div className="storyboard-scroll">
        {!hasSlides && (
          <div className="storyboard-empty">
            <h2>{storyboard.slidePlan.title}</h2>
            <p>構成案の本文は右側のInspectorから確認できます。</p>
          </div>
        )}
        {storyboard.sections.map((section) => (
          <section className="storyboard-section" key={section.id}>
            <header><span>SECTION</span><h2>{section.title}</h2></header>
            <div className="storyboard-card-grid">
              {section.slides.map((slide) => (
                <button
                  key={slide.id}
                  type="button"
                  className={selectedSlide === slide.id ? 'storyboard-card is-selected' : 'storyboard-card'}
                  onClick={() => onSelect(slide.id)}
                >
                  <span className="storyboard-card-number">{String(slide.number).padStart(2, '0')}</span>
                  <strong>{slide.title}</strong>
                  {slide.points.length > 0 && (
                    <ul>{slide.points.map((point, index) => <li key={`${slide.id}-${index}`}>{point}</li>)}</ul>
                  )}
                  {slide.visual && (
                    <span className={slide.visual.recommended ? 'visual-badge recommended' : 'visual-badge'}>
                      {slide.visual.recommended ? `Visual: ${slide.visual.type}` : 'Visual: なし'}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </section>
        ))}
      </div>
    </section>
  )
}
