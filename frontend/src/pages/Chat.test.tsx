import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import Chat from './Chat'
import type {
  ConversationSummary,
  ConversationListResponse,
  MessageHistoryResponse,
  CurrentUser,
} from '../api/client'
import type { ChatStreamEvent } from '../api/chatStream'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>(
    '../api/client',
  )
  return {
    ...actual,
    conversations: {
      list: vi.fn(),
      create: vi.fn(),
      history: vi.fn(),
    },
  }
})

vi.mock('../api/chatStream', () => ({
  streamChatMessage: vi.fn(),
}))

vi.mock('../auth/AuthContext', () => ({
  useAuth: vi.fn(),
}))

import { conversations } from '../api/client'
import { streamChatMessage } from '../api/chatStream'
import { useAuth } from '../auth/AuthContext'

const listMock = vi.mocked(conversations.list)
const createMock = vi.mocked(conversations.create)
const historyMock = vi.mocked(conversations.history)
const streamMock = vi.mocked(streamChatMessage)
const useAuthMock = vi.mocked(useAuth)

function makeUser(): CurrentUser {
  return {
    user_id: 'user-1',
    firm_id: 'firm-1',
    firm_slug: 'mc-s-accountants',
    upn: 'elio@mcands.com.au',
    display_name: 'Elio',
    role: 'owner',
  }
}

function makeConv(id: string, updated: string): ConversationSummary {
  return {
    id,
    title: null,
    created_at: updated,
    updated_at: updated,
  }
}

async function* yieldEvents(
  events: ChatStreamEvent[],
): AsyncGenerator<ChatStreamEvent> {
  for (const ev of events) yield ev
}

beforeEach(() => {
  vi.clearAllMocks()
  useAuthMock.mockReturnValue({
    user: makeUser(),
    loading: false,
    logout: vi.fn(),
  })
})

afterEach(() => {
  cleanup()
})

describe('Chat page', () => {
  it('lists conversations on mount', async () => {
    const list: ConversationListResponse = {
      conversations: [
        makeConv('c-1', '2026-06-01T11:00:00Z'),
        makeConv('c-2', '2026-05-30T11:00:00Z'),
      ],
    }
    listMock.mockResolvedValue(list)
    historyMock.mockResolvedValue({
      messages: [
        {
          id: 'm-1',
          role: 'user',
          content: 'What is Division 7A?',
          model: null,
          input_tokens: null,
          output_tokens: null,
          error: null,
          created_at: '2026-06-01T11:00:00Z',
        },
      ],
    })

    render(<Chat />)
    // The text appears both in the sidebar (derived title) and in
    // the message list (the user bubble), so multiple matches are
    // expected.
    await waitFor(() =>
      expect(
        screen.getAllByText('What is Division 7A?').length,
      ).toBeGreaterThanOrEqual(1),
    )
  })

  it('renders the empty state when no conversations exist', async () => {
    listMock.mockResolvedValue({ conversations: [] })
    render(<Chat />)
    await waitFor(() =>
      expect(screen.getByText(/start a new conversation/i)).toBeInTheDocument(),
    )
  })

  it('streams a turn end-to-end with a specialist consultation badge', async () => {
    listMock.mockResolvedValue({ conversations: [] })
    createMock.mockResolvedValue(makeConv('c-new', '2026-06-01T12:00:00Z'))

    const finalHistory: MessageHistoryResponse = {
      messages: [
        {
          id: 'm-user',
          role: 'user',
          content: 'Going-concern sale of pharmacy?',
          model: null,
          input_tokens: null,
          output_tokens: null,
          error: null,
          created_at: '2026-06-01T12:00:00Z',
        },
        {
          id: 'm-asst',
          role: 'assistant',
          content: 'GST and CGT both apply.',
          model: 'claude-sonnet-4-6',
          input_tokens: 100,
          output_tokens: 250,
          error: null,
          created_at: '2026-06-01T12:00:01Z',
        },
      ],
    }
    historyMock.mockResolvedValue(finalHistory)

    streamMock.mockReturnValue(
      yieldEvents([
        { type: 'token', text: 'Let me check ', source: 'orchestrator' },
        { type: 'token', text: 'with a specialist.', source: 'orchestrator' },
        {
          type: 'specialist_consultation_started',
          specialist_name: 'gst',
          display_name: 'GST Specialist',
          prompt_version_id: 'v-gst-1',
          model: 'claude-opus-4-7',
          step_index: 2,
        },
        // Post 003d-summary: specialist tokens are no longer streamed.
        // The user sees the badge only; the full specialist text
        // arrives as a <details> collapsible in the persisted message
        // after history refetch.
        {
          type: 'specialist_consultation_complete',
          specialist_name: 'gst',
          input_tokens: 50,
          output_tokens: 120,
          step_index: 2,
        },
        { type: 'token', text: 'GST applies.', source: 'orchestrator' },
        {
          type: 'done',
          message_id: 'm-asst',
          trace_id: 't-1',
          total_input_tokens: 150,
          total_output_tokens: 370,
        },
      ]),
    )

    render(<Chat />)
    await screen.findByText(/start a new conversation/i)

    await userEvent.click(
      screen.getByRole('button', { name: /start a new chat/i }),
    )

    await screen.findByPlaceholderText(/ask coworker something tax-related/i)

    const composer = screen.getByLabelText('Message')
    await userEvent.type(composer, 'Going-concern sale of pharmacy?')
    await userEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        'c-new',
        'Going-concern sale of pharmacy?',
      ),
    )

    // Once the stream resolves we refetch history; the persisted
    // assistant message is what's left on screen. (The live
    // consultation badge is only visible during the stream and is
    // replaced by the persisted text once `done` fires.)
    await waitFor(() =>
      expect(screen.getByText('GST and CGT both apply.')).toBeInTheDocument(),
    )
  })

  it('shows consultation badges live during streaming', async () => {
    listMock.mockResolvedValue({ conversations: [] })
    createMock.mockResolvedValue(makeConv('c-new', '2026-06-01T12:00:00Z'))
    historyMock.mockResolvedValue({ messages: [] })

    // A queue + deferred-promise generator so the test can advance
    // events one at a time and assert intermediate UI state.
    const queue: { ev: ChatStreamEvent; release: () => void }[] = []
    function enqueue(ev: ChatStreamEvent): Promise<void> {
      return new Promise((release) => queue.push({ ev, release }))
    }
    async function* controlledGen(): AsyncGenerator<ChatStreamEvent> {
      while (true) {
        if (queue.length === 0) {
          // Yield to the event loop so the test can push more events.
          await new Promise((r) => setTimeout(r, 5))
          continue
        }
        const next = queue.shift()
        if (!next) continue
        yield next.ev
        next.release()
        if (next.ev.type === 'done' || next.ev.type === 'error') return
      }
    }
    streamMock.mockReturnValue(controlledGen())

    render(<Chat />)
    await screen.findByText(/start a new conversation/i)
    await userEvent.click(
      screen.getByRole('button', { name: /start a new chat/i }),
    )
    await screen.findByPlaceholderText(/ask coworker something tax-related/i)
    await userEvent.type(screen.getByLabelText('Message'), 'q?')
    await userEvent.click(screen.getByRole('button', { name: /send/i }))

    void enqueue({
      type: 'specialist_consultation_started',
      specialist_name: 'gst',
      display_name: 'GST Specialist',
      prompt_version_id: 'v-1',
      model: 'claude-opus-4-7',
      step_index: 1,
    })

    await waitFor(() =>
      expect(
        screen.getByText(/consulting gst specialist/i),
      ).toBeInTheDocument(),
    )

    void enqueue({
      type: 'specialist_consultation_complete',
      specialist_name: 'gst',
      input_tokens: 50,
      output_tokens: 120,
      step_index: 1,
    })

    await waitFor(() =>
      expect(screen.getByText(/✓ gst specialist/i)).toBeInTheDocument(),
    )

    void enqueue({
      type: 'done',
      message_id: 'm-asst',
      trace_id: 't-1',
      total_input_tokens: 50,
      total_output_tokens: 120,
    })
  })

  it('surfaces a stream error without crashing', async () => {
    listMock.mockResolvedValue({ conversations: [] })
    createMock.mockResolvedValue(makeConv('c-new', '2026-06-01T12:00:00Z'))
    historyMock.mockResolvedValue({ messages: [] })

    streamMock.mockReturnValue(
      yieldEvents([
        { type: 'error', error: 'ConnectorError: Anthropic 503' },
      ]),
    )

    render(<Chat />)
    await screen.findByText(/start a new conversation/i)
    await userEvent.click(
      screen.getByRole('button', { name: /start a new chat/i }),
    )
    await screen.findByPlaceholderText(/ask coworker something tax-related/i)

    await userEvent.type(screen.getByLabelText('Message'), 'Test')
    await userEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() =>
      expect(
        screen.getByText(/connectorerror: anthropic 503/i),
      ).toBeInTheDocument(),
    )
  })

  it('renders persisted assistant message with collapsible specialist sections', async () => {
    listMock.mockResolvedValue({
      conversations: [makeConv('c-existing', '2026-06-01T11:00:00Z')],
    })
    historyMock.mockResolvedValue({
      messages: [
        {
          id: 'm-user',
          role: 'user',
          content: 'Pharmacy GST and CGT?',
          model: null,
          input_tokens: null,
          output_tokens: null,
          error: null,
          created_at: '2026-06-01T11:00:00Z',
        },
        {
          id: 'm-asst',
          role: 'assistant',
          content: [
            'GST and CGT both apply to this structure.',
            '',
            '- GST: going concern exemption available (s 38-325).',
            '- CGT: small business concessions in Division 152.',
            '',
            'Click any specialist below to see the full analysis.',
            '',
            '<!-- specialist-consultations-start -->',
            '',
            '<details>',
            '<summary>GST Specialist — full analysis (prompt v12345678, claude-opus-4-7)</summary>',
            '',
            'Full GST verbatim text that is hidden by default.',
            '',
            '</details>',
            '',
            '<details>',
            '<summary>CGT Specialist — full analysis (prompt v87654321, claude-opus-4-7)</summary>',
            '',
            'Full CGT verbatim text that is hidden by default.',
            '',
            '</details>',
            '',
            '<!-- specialist-consultations-end -->',
          ].join('\n'),
          model: 'claude-sonnet-4-6',
          input_tokens: 100,
          output_tokens: 250,
          error: null,
          created_at: '2026-06-01T11:00:01Z',
        },
      ],
    })

    render(<Chat />)

    // Select the existing conversation from the sidebar.
    const convButton = await screen.findByRole('button', {
      name: /pharmacy gst and cgt|untitled/i,
    })
    await userEvent.click(convButton)

    // Synthesis is visible immediately.
    await screen.findByText(/gst and cgt both apply to this structure\./i)
    expect(
      screen.getByText(/click any specialist below to see the full analysis\./i),
    ).toBeInTheDocument()

    // Two <details> elements render, both closed by default.
    const detailsElements = document.querySelectorAll('details')
    expect(detailsElements.length).toBe(2)
    for (const d of detailsElements) {
      expect((d as HTMLDetailsElement).open).toBe(false)
    }

    // Summary labels are visible.
    expect(
      screen.getByText(/gst specialist — full analysis/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/cgt specialist — full analysis/i),
    ).toBeInTheDocument()

    // Full verbatim text is in the DOM (inside the closed details) but
    // not visually exposed (details default state hides children).
    expect(
      screen.getByText(/full gst verbatim text that is hidden by default\./i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/full cgt verbatim text that is hidden by default\./i),
    ).toBeInTheDocument()
  })
})
