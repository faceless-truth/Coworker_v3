import { describe, it, expect } from 'vitest'
import { deriveTitle, relativeTime } from './utils'

describe('deriveTitle', () => {
  it('returns "New conversation" for null', () => {
    expect(deriveTitle(null)).toBe('New conversation')
  })

  it('returns "New conversation" for undefined', () => {
    expect(deriveTitle(undefined)).toBe('New conversation')
  })

  it('returns "New conversation" for empty string', () => {
    expect(deriveTitle('')).toBe('New conversation')
  })

  it('returns "New conversation" for whitespace-only', () => {
    expect(deriveTitle('   \n\n  ')).toBe('New conversation')
  })

  it('returns short messages unchanged', () => {
    expect(deriveTitle('What is Division 7A?')).toBe(
      'What is Division 7A?',
    )
  })

  it('truncates messages longer than 50 chars with ellipsis', () => {
    const long =
      'Going-concern sale of my pharmacy business and the GST implications'
    const result = deriveTitle(long)
    expect(result).toHaveLength(48)
    expect(result.endsWith('…')).toBe(true)
    expect(result.startsWith('Going-concern sale of my pharmacy business')).toBe(
      true,
    )
  })

  it('uses only the first line of a multi-line message', () => {
    expect(deriveTitle('First line\nSecond line\nThird')).toBe(
      'First line',
    )
  })
})

describe('relativeTime', () => {
  const now = new Date('2026-06-01T12:00:00Z').getTime()

  it('returns "just now" for under a minute', () => {
    expect(
      relativeTime('2026-06-01T11:59:30Z', now),
    ).toBe('just now')
  })

  it('returns Xm ago for under an hour', () => {
    expect(relativeTime('2026-06-01T11:30:00Z', now)).toBe('30m ago')
  })

  it('returns Xh ago for under a day', () => {
    expect(relativeTime('2026-06-01T06:00:00Z', now)).toBe('6h ago')
  })

  it('returns Xd ago for under a week', () => {
    expect(relativeTime('2026-05-30T12:00:00Z', now)).toBe('2d ago')
  })

  it('falls back to locale date past a week', () => {
    const result = relativeTime('2026-04-15T12:00:00Z', now)
    expect(result).not.toMatch(/ago/)
    expect(result.length).toBeGreaterThan(0)
  })

  it('returns empty string for invalid ISO', () => {
    expect(relativeTime('not-a-date', now)).toBe('')
  })
})
