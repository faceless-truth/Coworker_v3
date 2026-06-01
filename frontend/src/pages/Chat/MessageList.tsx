import { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'
import type { ChatMessageOut } from '../../api/client'
import type { StreamingMessage, StreamingSegment } from './MessageList.state'

interface Props {
  history: ChatMessageOut[]
  streaming: StreamingMessage | null
}

const MD_COMPONENTS: Components = {
  a: ({ children, ...rest }) => (
    <a {...rest} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  code: ({ children, ...rest }) => (
    <code
      {...rest}
      style={{
        fontFamily: 'JetBrains Mono, monospace',
        background: '#f3f1ee',
        padding: '1px 4px',
        borderRadius: '3px',
        fontSize: '0.92em',
      }}
    >
      {children}
    </code>
  ),
  pre: ({ children, ...rest }) => (
    <pre
      {...rest}
      style={{
        fontFamily: 'JetBrains Mono, monospace',
        background: '#f3f1ee',
        padding: '12px',
        borderRadius: '4px',
        overflowX: 'auto',
        fontSize: '0.9em',
        margin: '8px 0',
      }}
    >
      {children}
    </pre>
  ),
  details: ({ children, ...rest }) => (
    <details
      {...rest}
      style={{
        margin: '8px 0',
        padding: '8px 12px',
        border: '1px solid #d9d8d8',
        borderRadius: '4px',
        background: '#ffffff',
      }}
    >
      {children}
    </details>
  ),
  summary: ({ children, ...rest }) => (
    <summary
      {...rest}
      style={{
        cursor: 'pointer',
        fontWeight: 500,
        color: '#142234',
        userSelect: 'none',
      }}
    >
      {children}
    </summary>
  ),
}

function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw]}
      components={MD_COMPONENTS}
    >
      {children}
    </ReactMarkdown>
  )
}

function ConsultationBadge({
  segment,
}: {
  segment: Extract<StreamingSegment, { kind: 'consultation' }>
}) {
  const tooltip = (() => {
    const parts: string[] = [
      `Prompt version: ${segment.prompt_version_id}`,
      `Model: ${segment.model}`,
    ]
    if (
      segment.input_tokens !== undefined &&
      segment.output_tokens !== undefined
    ) {
      parts.push(
        `${segment.input_tokens} → ${segment.output_tokens} tokens`,
      )
    }
    return parts.join('\n')
  })()

  if (segment.error) {
    return (
      <span
        title={`Consultation failed: ${segment.error}`}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '6px',
          padding: '4px 12px',
          borderRadius: '999px',
          background: 'rgba(225, 29, 72, 0.06)',
          border: '1px solid #e11d48',
          color: '#e11d48',
          fontSize: '13px',
          fontWeight: 500,
          margin: '8px 0',
        }}
      >
        × Consultation failed: {segment.display_name}
      </span>
    )
  }

  return (
    <span
      title={tooltip}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 12px',
        borderRadius: '999px',
        background: 'rgba(48, 128, 188, 0.08)',
        border: '1px solid #3080bc',
        color: '#142234',
        fontSize: '13px',
        fontWeight: 500,
        margin: '8px 0',
      }}
    >
      {segment.complete ? (
        <>✓ {segment.display_name}</>
      ) : (
        <>⋯ Consulting {segment.display_name}…</>
      )}
    </span>
  )
}

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end mb-4">
      <div
        className="px-4 py-2 text-sm"
        style={{
          background: '#3080bc',
          color: 'white',
          borderRadius: '14px 14px 2px 14px',
          maxWidth: '70%',
          whiteSpace: 'pre-wrap',
        }}
      >
        {content}
      </div>
    </div>
  )
}

function AssistantPersisted({ message }: { message: ChatMessageOut }) {
  if (message.error) {
    return (
      <div
        className="mb-4 text-sm px-3 py-2"
        style={{
          color: '#e11d48',
          background: 'rgba(225, 29, 72, 0.06)',
          border: '1px solid #e11d48',
          borderRadius: '4px',
          maxWidth: '80%',
        }}
      >
        {message.error}
      </div>
    )
  }
  return (
    <div className="mb-6" style={{ maxWidth: '80%', color: '#34322d' }}>
      <Markdown>{message.content}</Markdown>
    </div>
  )
}

function AssistantStreaming({ msg }: { msg: StreamingMessage }) {
  return (
    <div className="mb-6" style={{ maxWidth: '80%', color: '#34322d' }}>
      {msg.segments.map((seg, idx) => {
        if (seg.kind === 'consultation') {
          return <ConsultationBadge key={idx} segment={seg} />
        }
        const isSpecialist = seg.source.startsWith('specialist:')
        return (
          <div
            key={idx}
            style={
              isSpecialist
                ? {
                    borderLeft: '2px solid #eb881f',
                    paddingLeft: '12px',
                    marginLeft: '4px',
                  }
                : undefined
            }
          >
            <Markdown>{seg.text}</Markdown>
          </div>
        )
      })}
      {msg.error && (
        <div
          className="mt-2 text-sm px-3 py-2"
          style={{
            color: '#e11d48',
            background: 'rgba(225, 29, 72, 0.06)',
            border: '1px solid #e11d48',
            borderRadius: '4px',
          }}
        >
          {msg.error}
        </div>
      )}
      {!msg.done && !msg.error && (
        <div
          className="mt-2 text-xs"
          style={{ color: '#858481' }}
          aria-label="streaming"
        >
          ⋯
        </div>
      )}
    </div>
  )
}

export default function MessageList({ history, streaming }: Props) {
  const endRef = useRef<HTMLDivElement | null>(null)
  const segmentCount = streaming?.segments.length ?? 0
  const lastSeg = streaming?.segments[segmentCount - 1]
  const lastSegmentTextLen =
    lastSeg && lastSeg.kind === 'text' ? lastSeg.text.length : 0

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [history.length, segmentCount, lastSegmentTextLen, streaming?.done])

  return (
    <div
      className="flex-1 overflow-y-auto px-6 py-6"
      style={{ background: '#f3f1ee' }}
    >
      <div className="mx-auto" style={{ maxWidth: '820px' }}>
        {history.map((m) =>
          m.role === 'user' ? (
            <UserBubble key={m.id} content={m.content} />
          ) : (
            <AssistantPersisted key={m.id} message={m} />
          ),
        )}
        {streaming && <AssistantStreaming msg={streaming} />}
        <div ref={endRef} />
      </div>
    </div>
  )
}
