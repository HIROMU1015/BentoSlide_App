import type { HtmlProposal, ReviewMarks } from '../types'

export function initialReviewMarks(proposal: HtmlProposal | null): ReviewMarks {
  return Object.fromEntries((proposal?.affectedSlides ?? []).map((slide) => [slide.id, 'pending']))
}

export function canApplyProposal(proposal: HtmlProposal | null, marks: ReviewMarks): boolean {
  if (!proposal || proposal.status === 'applied' || proposal.affectedSlides.length === 0) return false
  return proposal.affectedSlides.every((slide) => marks[slide.id] === 'reviewed')
}

export function reviewedSlideIds(proposal: HtmlProposal | null, marks: ReviewMarks): string[] {
  return (proposal?.affectedSlides ?? [])
    .filter((slide) => marks[slide.id] === 'reviewed')
    .map((slide) => slide.id)
}
