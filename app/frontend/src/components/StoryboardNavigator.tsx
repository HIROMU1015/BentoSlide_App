import type { Storyboard } from '../types'

type Props = {
  storyboard: Storyboard
  selectedSlide: string | null
  onSelect: (slideId: string) => void
}

export function StoryboardNavigator({ storyboard, selectedSlide, onSelect }: Props) {
  const count = storyboard.sections.reduce((total, section) => total + section.slides.length, 0)
  return (
    <nav className="slide-navigator storyboard-navigator" aria-label="Storyboard一覧">
      <div className="panel-heading">
        <span>Storyboard</span>
        <span className="count-badge">{count}</span>
      </div>
      <div className="storyboard-nav-sections">
        {storyboard.sections.map((section) => (
          <section key={section.id}>
            <h2>{section.title}</h2>
            <ol className="slide-list">
              {section.slides.map((slide) => (
                <li key={slide.id}>
                  <button
                    className={selectedSlide === slide.id ? 'slide-button is-selected' : 'slide-button'}
                    type="button"
                    onClick={() => onSelect(slide.id)}
                    aria-current={selectedSlide === slide.id ? 'page' : undefined}
                  >
                    <span className="slide-number">{String(slide.number).padStart(2, '0')}</span>
                    <span className="slide-copy"><strong>{slide.title}</strong></span>
                  </button>
                </li>
              ))}
            </ol>
          </section>
        ))}
      </div>
    </nav>
  )
}
