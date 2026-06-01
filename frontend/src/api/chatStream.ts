/**
 * SSE client for POST /api/v1/conversations/{id}/messages.
 *
 * The backend uses POST + an event-stream response, which rules out
 * EventSource (GET-only). We consume the response body via fetch +
 * ReadableStream and parse the SSE frames manually.
 *
 * Wire-format reference (matches backend/coworker/chat/orchestrator.py):
 *
 *   event: token
 *   data: {"text": "...", "source": "orchestrator" | "specialist:<slug>"}
 *
 *   event: specialist_consultation_started
 *   data: {"specialist_name": "...", "display_name": "...",
 *          "prompt_version_id": "...", "model": "...", "step_index": 1}
 *
 *   event: specialist_consultation_complete
 *   data: {"specialist_name": "...", "input_tokens": 0,
 *          "output_tokens": 0, "step_index": 1}
 *
 *   event: specialist_consultation_error
 *   data: {"specialist_name": "...", "error": "...", "step_index": 1}
 *
 *   event: done
 *   data: {"message_id": "...", "trace_id": "...",
 *          "total_input_tokens": 0, "total_output_tokens": 0}
 *
 *   event: error
 *   data: {"error": "..."}
 */

export interface TokenEvent {
  type: 'token';
  text: string;
  source: string;
}

export interface ConsultationStartedEvent {
  type: 'specialist_consultation_started';
  specialist_name: string;
  display_name: string;
  prompt_version_id: string;
  model: string;
  step_index: number;
}

export interface ConsultationCompleteEvent {
  type: 'specialist_consultation_complete';
  specialist_name: string;
  input_tokens: number;
  output_tokens: number;
  step_index: number;
}

export interface ConsultationErrorEvent {
  type: 'specialist_consultation_error';
  specialist_name: string;
  error: string;
  step_index: number;
}

export interface DoneEvent {
  type: 'done';
  message_id: string;
  trace_id: string;
  total_input_tokens: number;
  total_output_tokens: number;
}

export interface ErrorEvent {
  type: 'error';
  error: string;
}

export type ChatStreamEvent =
  | TokenEvent
  | ConsultationStartedEvent
  | ConsultationCompleteEvent
  | ConsultationErrorEvent
  | DoneEvent
  | ErrorEvent;


export async function* streamChatMessage(
  conversationId: string,
  content: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const response = await fetch(
    `/api/v1/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({ content }),
      signal,
    },
  );

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(
      `SSE request failed: HTTP ${response.status} ${body.slice(0, 200)}`,
    );
  }
  if (!response.body) {
    throw new Error('SSE response has no body');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (buffer.trim().length > 0) {
          const trailing = parseSSEEvent(buffer);
          if (trailing) yield trailing;
        }
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      let separatorIndex: number;
      while ((separatorIndex = buffer.indexOf('\n\n')) !== -1) {
        const rawEvent = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex + 2);

        const event = parseSSEEvent(rawEvent);
        if (event) yield event;
      }
    }
  } finally {
    reader.releaseLock();
  }
}


export function parseSSEEvent(raw: string): ChatStreamEvent | null {
  let eventName = '';
  let data = '';
  for (const line of raw.split('\n')) {
    if (line.startsWith('event: ')) {
      eventName = line.slice(7).trim();
    } else if (line.startsWith('data: ')) {
      data += line.slice(6);
    }
  }
  if (!eventName || !data) return null;

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }

  switch (eventName) {
    case 'token':
      return { type: 'token', ...(parsed as Omit<TokenEvent, 'type'>) };
    case 'specialist_consultation_started':
      return {
        type: 'specialist_consultation_started',
        ...(parsed as Omit<ConsultationStartedEvent, 'type'>),
      };
    case 'specialist_consultation_complete':
      return {
        type: 'specialist_consultation_complete',
        ...(parsed as Omit<ConsultationCompleteEvent, 'type'>),
      };
    case 'specialist_consultation_error':
      return {
        type: 'specialist_consultation_error',
        ...(parsed as Omit<ConsultationErrorEvent, 'type'>),
      };
    case 'done':
      return { type: 'done', ...(parsed as Omit<DoneEvent, 'type'>) };
    case 'error':
      return { type: 'error', ...(parsed as Omit<ErrorEvent, 'type'>) };
    default:
      return null;
  }
}
