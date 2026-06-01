import type {
  ConsultationStartedEvent,
  ConsultationCompleteEvent,
  ConsultationErrorEvent,
} from '../../api/chatStream'

export type StreamingSegment =
  | { kind: 'text'; source: string; text: string }
  | {
      kind: 'consultation'
      specialist_name: string
      display_name: string
      prompt_version_id: string
      model: string
      step_index: number
      complete: boolean
      input_tokens?: number
      output_tokens?: number
      error?: string
    }

export interface StreamingMessage {
  segments: StreamingSegment[]
  totalInputTokens?: number
  totalOutputTokens?: number
  done: boolean
  error?: string
}

export function emptyStreamingMessage(): StreamingMessage {
  return { segments: [], done: false }
}

export function appendTokenToStreaming(
  msg: StreamingMessage,
  text: string,
  source: string,
): StreamingMessage {
  const segs = msg.segments
  const last = segs[segs.length - 1]
  if (last && last.kind === 'text' && last.source === source) {
    const updated: StreamingSegment = {
      kind: 'text',
      source,
      text: last.text + text,
    }
    return { ...msg, segments: [...segs.slice(0, -1), updated] }
  }
  return {
    ...msg,
    segments: [...segs, { kind: 'text', source, text }],
  }
}

export function pushConsultationStarted(
  msg: StreamingMessage,
  ev: ConsultationStartedEvent,
): StreamingMessage {
  return {
    ...msg,
    segments: [
      ...msg.segments,
      {
        kind: 'consultation',
        specialist_name: ev.specialist_name,
        display_name: ev.display_name,
        prompt_version_id: ev.prompt_version_id,
        model: ev.model,
        step_index: ev.step_index,
        complete: false,
      },
    ],
  }
}

export function markConsultationComplete(
  msg: StreamingMessage,
  ev: ConsultationCompleteEvent,
): StreamingMessage {
  return {
    ...msg,
    segments: msg.segments.map((s) =>
      s.kind === 'consultation' && s.step_index === ev.step_index
        ? {
            ...s,
            complete: true,
            input_tokens: ev.input_tokens,
            output_tokens: ev.output_tokens,
          }
        : s,
    ),
  }
}

export function markConsultationError(
  msg: StreamingMessage,
  ev: ConsultationErrorEvent,
): StreamingMessage {
  return {
    ...msg,
    segments: msg.segments.map((s) =>
      s.kind === 'consultation' && s.step_index === ev.step_index
        ? { ...s, complete: true, error: ev.error }
        : s,
    ),
  }
}
