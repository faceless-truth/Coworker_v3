import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import ConversationSidebar from './ConversationSidebar'
import type { ConversationSummary } from '../../api/client'

const NOW = new Date('2026-06-01T12:00:00Z')

function fix(updated: string): ConversationSummary {
  return {
    id: `c-${updated}`,
    title: null,
    created_at: updated,
    updated_at: updated,
  }
}

const SAMPLE: ConversationSummary[] = [
  fix('2026-06-01T11:30:00Z'),
  fix('2026-05-31T12:00:00Z'),
]

const FIRST_MESSAGES = new Map([
  ['c-2026-06-01T11:30:00Z', 'What is Division 7A?'],
  ['c-2026-05-31T12:00:00Z', 'Capital gains on a rental property sale'],
])

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('ConversationSidebar', () => {
  it('renders conversation rows with derived titles', () => {
    vi.setSystemTime(NOW)
    render(
      <ConversationSidebar
        conversations={SAMPLE}
        selectedId={null}
        onSelect={() => {}}
        onNewChat={() => {}}
        loading={false}
        error={null}
        firstMessages={FIRST_MESSAGES}
      />,
    )
    expect(screen.getByText('What is Division 7A?')).toBeInTheDocument()
    expect(
      screen.getByText('Capital gains on a rental property sale'),
    ).toBeInTheDocument()
  })

  it('invokes onSelect with the conversation id', async () => {
    vi.setSystemTime(NOW)
    const onSelect = vi.fn()
    render(
      <ConversationSidebar
        conversations={SAMPLE}
        selectedId={null}
        onSelect={onSelect}
        onNewChat={() => {}}
        loading={false}
        error={null}
        firstMessages={FIRST_MESSAGES}
      />,
    )
    await userEvent.click(screen.getByText('What is Division 7A?'))
    expect(onSelect).toHaveBeenCalledWith('c-2026-06-01T11:30:00Z')
  })

  it('calls onNewChat when the New chat button is clicked', async () => {
    const onNewChat = vi.fn()
    render(
      <ConversationSidebar
        conversations={[]}
        selectedId={null}
        onSelect={() => {}}
        onNewChat={onNewChat}
        loading={false}
        error={null}
        firstMessages={new Map()}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }))
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('shows the empty state when there are no conversations', () => {
    render(
      <ConversationSidebar
        conversations={[]}
        selectedId={null}
        onSelect={() => {}}
        onNewChat={() => {}}
        loading={false}
        error={null}
        firstMessages={new Map()}
      />,
    )
    expect(screen.getByText(/no conversations yet/i)).toBeInTheDocument()
  })

  it('shows a loading state', () => {
    render(
      <ConversationSidebar
        conversations={[]}
        selectedId={null}
        onSelect={() => {}}
        onNewChat={() => {}}
        loading={true}
        error={null}
        firstMessages={new Map()}
      />,
    )
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows the error state', () => {
    render(
      <ConversationSidebar
        conversations={[]}
        selectedId={null}
        onSelect={() => {}}
        onNewChat={() => {}}
        loading={false}
        error="Network down"
        firstMessages={new Map()}
      />,
    )
    expect(screen.getByText('Network down')).toBeInTheDocument()
  })

  it('marks the selected row with an orange border', () => {
    vi.setSystemTime(NOW)
    render(
      <ConversationSidebar
        conversations={SAMPLE}
        selectedId="c-2026-06-01T11:30:00Z"
        onSelect={() => {}}
        onNewChat={() => {}}
        loading={false}
        error={null}
        firstMessages={FIRST_MESSAGES}
      />,
    )
    const row = screen
      .getByText('What is Division 7A?')
      .closest('button') as HTMLElement
    expect(row.style.borderLeft).toBe('3px solid rgb(235, 136, 31)')
  })
})
