import type { Storyboard } from '../types'

type Props = {
  storyboard: Storyboard
  selectedSlide: string | null
  onSelect: (slideId: string) => void
  onViewChange: (view: 'current' | 'candidate') => void
}

export function StoryboardCanvas({ storyboard, selectedSlide, onSelect, onViewChange }: Props) {
  const hasSlides = storyboard.sections.some((section) => section.slides.length > 0)
  return (
    <section className="main-canvas storyboard-canvas" aria-label="Storyboard確認">
      <div className="canvas-toolbar">
        <div className="view-switch" role="group" aria-label="表示する構成案">
          <button
            type="button"
            className={storyboard.view !== 'candidate' ? 'current is-active' : 'current'}
            onClick={() => onViewChange('current')}
          >
            Current
          </button>
          {storyboard.proposal && (
            <button
              type="button"
              className={storyboard.view === 'candidate' ? 'candidate is-active' : 'candidate'}
              onClick={() => onViewChange('candidate')}
            >
              Candidate
            </button>
          )}
        </div>
        <span className={`view-label ${storyboard.view ?? 'current'}`}>
          {storyboard.view === 'candidate' ? '変更案を表示中' : '現在案を表示中'}
        </span>
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
