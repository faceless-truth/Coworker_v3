import { describe, it, expect } from 'vitest'
import {
  parseSSEEvent,
  streamChatMessage,
  type ChatStreamEvent,
} from './chatStream'

describe('parseSSEEvent', () => {
  it('returns null for empty input', () => {
    expect(parseSSEEvent('')).toBeNull()
  })

  it('parses a single token event', () => {
    const raw =
      'event: token\ndata: {"text": "hello", "source": "orchestrator"}'
    expect(parseSSEEvent(raw)).toEqual({
      type: 'token',
      text: 'hello',
      source: 'orchestrator',
    })
  })

  it('parses specialist_consultation_started with full wire fields', () => {
    const raw =
      'event: specialist_consultation_started\n' +
      'data: {"specialist_name": "smsf", "display_name": "SMSF Specialist", "prompt_version_id": "v-1", "model": "claude-opus-4-7", "step_index": 2}'
    expect(parseSSEEvent(raw)).toEqual({
      type: 'specialist_consultation_started',
      specialist_name: 'smsf',
      display_name: 'SMSF Specialist',
      prompt_version_id: 'v-1',
      model: 'claude-opus-4-7',
      step_index: 2,
    })
  })

  it('parses done with totals', () => {
    const raw =
      'event: done\ndata: {"message_id": "m-1", "trace_id": "t-1", "total_input_tokens": 100, "total_output_tokens": 250}'
    expect(parseSSEEvent(raw)).toEqual({
      type: 'done',
      message_id: 'm-1',
      trace_id: 't-1',
      total_input_tokens: 100,
      total_output_tokens: 250,
    })
  })

  it('returns null on malformed JSON', () => {
    const raw = 'event: token\ndata: {not json'
    expect(parseSSEEvent(raw)).toBeNull()
  })

  it('returns null on unknown event name', () => {
    const raw = 'event: unicorn\ndata: {"k": 1}'
    expect(parseSSEEvent(raw)).toBeNull()
  })

  it('returns null if event line missing', () => {
    expect(parseSSEEvent('data: {"x": 1}')).toBeNull()
  })

  it('returns null if data line missing', () => {
    expect(parseSSEEvent('event: token')).toBeNull()
  })
})

function makeMockResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c))
      controller.close()
    },
  })
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

async function collect(
  gen: AsyncGenerator<ChatStreamEvent>,
): Promise<ChatStreamEvent[]> {
  const out: ChatStreamEvent[] = []
  for await (const ev of gen) out.push(ev)
  return out
}

describe('streamChatMessage', () => {
  it('emits multiple events from a single buffer chunk', async () => {
    const chunks = [
      'event: token\ndata: {"text": "A", "source": "orchestrator"}\n\n' +
        'event: token\ndata: {"text": "B", "source": "orchestrator"}\n\n' +
        'event: done\ndata: {"message_id": "m", "trace_id": "t", "total_input_tokens": 1, "total_output_tokens": 2}\n\n',
    ]
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeMockResponse(chunks)
    try {
      const events = await collect(streamChatMessage('c-1', 'hi'))
      expect(events).toHaveLength(3)
      expect(events[0]).toMatchObject({ type: 'token', text: 'A' })
      expect(events[1]).toMatchObject({ type: 'token', text: 'B' })
      expect(events[2]).toMatchObject({ type: 'done', message_id: 'm' })
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('handles events split across buffer chunks', async () => {
    const chunks = [
      'event: token\ndata: {"text": "',
      'hello world',
      '", "source": "orchestrator"}\n\n',
      'event: done\ndata: {"message_id": "m", "trace_id": "t", "total_input_tokens": 0, "total_output_tokens": 0}\n\n',
    ]
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeMockResponse(chunks)
    try {
      const events = await collect(streamChatMessage('c-1', 'hi'))
      expect(events).toHaveLength(2)
      expect(events[0]).toMatchObject({
        type: 'token',
        text: 'hello world',
      })
      expect(events[1]).toMatchObject({ type: 'done' })
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('skips malformed events and continues', async () => {
    const chunks = [
      'event: token\ndata: {bad json\n\n' +
        'event: token\ndata: {"text": "ok", "source": "orchestrator"}\n\n',
    ]
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeMockResponse(chunks)
    try {
      const events = await collect(streamChatMessage('c-1', 'hi'))
      expect(events).toHaveLength(1)
      expect(events[0]).toMatchObject({ type: 'token', text: 'ok' })
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('throws on non-2xx response', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () =>
      new Response('boom', { status: 500 })
    try {
      await expect(
        collect(streamChatMessage('c-1', 'hi')),
      ).rejects.toThrow(/HTTP 500/)
    } finally {
      globalThis.fetch = originalFetch
    }
  })
})
