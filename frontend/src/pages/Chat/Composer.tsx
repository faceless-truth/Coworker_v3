import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Send } from 'lucide-react'

interface Props {
  onSend: (content: string) => void | Promise<void>
  disabled: boolean
  displayName: string
}

const MAX_ROWS = 8
const LINE_HEIGHT_PX = 22

export default function Composer({ onSend, disabled, displayName }: Props) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (!disabled) textareaRef.current?.focus()
  }, [disabled])

  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    const max = MAX_ROWS * LINE_HEIGHT_PX
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden'
  }, [value])

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    setValue('')
    void onSend(trimmed)
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const placeholder = `Ask CoWorker something tax-related, ${displayName}…`

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
      className="border-t px-6 py-4"
      style={{ borderColor: '#d9d8d8', background: 'white' }}
    >
      <div className="mx-auto" style={{ maxWidth: '820px' }}>
        <div
          className="flex items-end gap-3 px-3 py-2"
          style={{
            border: '1px solid #d9d8d8',
            borderRadius: '8px',
            background: 'white',
          }}
        >
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            rows={1}
            className="flex-1 resize-none outline-none text-sm py-1"
            style={{
              minHeight: `${LINE_HEIGHT_PX}px`,
              maxHeight: `${MAX_ROWS * LINE_HEIGHT_PX}px`,
              lineHeight: `${LINE_HEIGHT_PX}px`,
              background: 'transparent',
              color: '#34322d',
            }}
            aria-label="Message"
          />
          <button
            type="submit"
            disabled={disabled || value.trim().length === 0}
            className="btn-primary flex items-center gap-2 flex-shrink-0"
            style={{
              opacity:
                disabled || value.trim().length === 0 ? 0.5 : 1,
              cursor:
                disabled || value.trim().length === 0
                  ? 'not-allowed'
                  : 'pointer',
            }}
            aria-label="Send"
          >
            {disabled ? (
              <span aria-label="streaming">⋯</span>
            ) : (
              <Send size={14} />
            )}
            <span>Send</span>
          </button>
        </div>
        <div
          className="text-xs mt-2"
          style={{ color: '#858481' }}
        >
          Enter to send · Shift+Enter for newline
        </div>
      </div>
    </form>
  )
}
