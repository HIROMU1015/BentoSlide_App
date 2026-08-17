import type { SlideItem } from '../types'

type Props = {
  slides: SlideItem[]
  selectedSlide: string | null
  onSelect: (slideId: string) => void
}

export function SlideNavigator({ slides, selectedSlide, onSelect }: Props) {
  return (
    <nav className="slide-navigator" aria-label="スライド一覧">
      <div className="panel-heading">
        <span>Slides</span>
        <span className="count-badge">{slides.length}</span>
      </div>
      <ol className="slide-list">
        {slides.map((slide) => (
          <li key={slide.id}>
            <button
              className={selectedSlide === slide.id ? 'slide-button is-selected' : 'slide-button'}
              type="button"
              onClick={() => onSelect(slide.id)}
              aria-current={selectedSlide === slide.id ? 'page' : undefined}
            >
              <span className="slide-number">{String(slide.number).padStart(2, '0')}</span>
              <span className="slide-copy">
                <strong>{slide.title}</strong>
                {slide.sectionTitle && <small>{slide.sectionTitle}</small>}
              </span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  )
}
