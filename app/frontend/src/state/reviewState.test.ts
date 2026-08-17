import { describe, expect, it } from 'vitest'
import { canApplyProposal, initialReviewMarks, reviewedSlideIds } from './reviewState'
import type { HtmlProposal } from '../types'

const proposal: HtmlProposal = {
  status: 'proposed',
  scope: 'related',
  summary: '説明を短くする',
  impactSummary: '次のスライドも用語を揃える',
  affectedSlides: [
    { id: 's1', title: '背景', number: 1, impact: 'changed' },
    { id: 's2', title: '提案', number: 2, impact: 'related' },
  ],
  postApplyReviewStatus: null,
}

describe('review checklist', () => {
  it('requires every affected slide and preserves proposal order', () => {
    const marks = initialReviewMarks(proposal)
    expect(canApplyProposal(proposal, marks)).toBe(false)
    marks.s2 = 'reviewed'
    marks.s1 = 'reviewed'
    expect(canApplyProposal(proposal, marks)).toBe(true)
    expect(reviewedSlideIds(proposal, marks)).toEqual(['s1', 's2'])
  })

  it('never treats needs-work as approval', () => {
    expect(canApplyProposal(proposal, { s1: 'reviewed', s2: 'needs-work' })).toBe(false)
  })
})
