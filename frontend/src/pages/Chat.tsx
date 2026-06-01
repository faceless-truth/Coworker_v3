import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import {
  conversations as conversationsApi,
  ApiError,
  type ConversationSummary,
  type ChatMessageOut,
} from '../api/client'
import { streamChatMessage } from '../api/chatStream'
import ConversationSidebar from './Chat/ConversationSidebar'
import MessageList from './Chat/MessageList'
import {
  type StreamingMessage,
  appendTokenToStreaming,
  emptyStreamingMessage,
  markConsultationComplete,
  markConsultationError,
  pushConsultationStarted,
} from './Chat/MessageList.state'
import Composer from './Chat/Composer'

function firstUserMessage(history: ChatMessageOut[]): string | null {
  const msg = history.find((m) => m.role === 'user')
  return msg ? msg.content : null
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export default function Chat() {
  const { user } = useAuth()
  const displayName = user?.display_name ?? 'there'

  const [convoList, setConvoList] = useState<ConversationSummary[]>([])
  const [convoLoading, setConvoLoading] = useState(true)
  const [convoError, setConvoError] = useState<string | null>(null)

  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [historyState, setHistoryState] = useState<{
    convId: string | null
    messages: ChatMessageOut[]
  }>({ convId: null, messages: [] })
  const [historyErrorState, setHistoryErrorState] = useState<{
    convId: string
    error: string
  } | null>(null)

  // Derived rendering values keyed off the currently selected
  // conversation so stale messages or errors from a previous
  // selection never flash on screen.
  const history =
    historyState.convId === selectedId ? historyState.messages : []
  const historyError =
    historyErrorState && historyErrorState.convId === selectedId
      ? historyErrorState.error
      : null
  const historyLoading =
    selectedId !== null &&
    historyState.convId !== selectedId &&
    historyError === null

  const [streaming, setStreaming] = useState<StreamingMessage | null>(null)

  // Map<conversation_id, first_user_message_content>. Populated as
  // we load histories; the sidebar uses it to derive titles client-side.
  const [firstMessages, setFirstMessages] = useState<Map<string, string>>(
    new Map(),
  )

  const refetchConversations = useCallback(async () => {
    try {
      const resp = await conversationsApi.list()
      setConvoList(resp.conversations)
      setConvoError(null)
      return resp.conversations
    } catch (err) {
      setConvoError(errorMessage(err))
      return []
    } finally {
      setConvoLoading(false)
    }
  }, [])

  useEffect(() => {
    void (async () => {
      const list = await refetchConversations()
      if (list.length > 0 && !selectedId) {
        setSelectedId(list[0].id)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refetchConversations])

  useEffect(() => {
    if (!selectedId) return
    let cancelled = false
    const id = selectedId
    void conversationsApi
      .history(id)
      .then((resp) => {
        if (cancelled) return
        setHistoryState({ convId: id, messages: resp.messages })
        const first = firstUserMessage(resp.messages)
        if (first) {
          setFirstMessages((prev) => {
            const next = new Map(prev)
            next.set(id, first)
            return next
          })
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setHistoryErrorState({ convId: id, error: errorMessage(err) })
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  const handleNewChat = useCallback(async () => {
    try {
      const conv = await conversationsApi.create()
      setConvoList((prev) => [conv, ...prev])
      setSelectedId(conv.id)
      setHistoryState({ convId: conv.id, messages: [] })
      setStreaming(null)
    } catch (err) {
      setConvoError(errorMessage(err))
    }
  }, [])

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id)
    setStreaming(null)
  }, [])

  const handleSend = useCallback(
    async (content: string) => {
      let conversationId = selectedId
      if (!conversationId) {
        try {
          const conv = await conversationsApi.create()
          setConvoList((prev) => [conv, ...prev])
          setSelectedId(conv.id)
          conversationId = conv.id
        } catch (err) {
          setStreaming({
            segments: [],
            done: true,
            error: errorMessage(err),
          })
          return
        }
      }

      const optimisticUser: ChatMessageOut = {
        id: `optimistic-${Date.now()}`,
        role: 'user',
        content,
        model: null,
        input_tokens: null,
        output_tokens: null,
        error: null,
        created_at: new Date().toISOString(),
      }
      setHistoryState((prev) =>
        prev.convId === conversationId
          ? { convId: conversationId, messages: [...prev.messages, optimisticUser] }
          : { convId: conversationId, messages: [optimisticUser] },
      )
      setFirstMessages((prev) => {
        if (prev.has(conversationId)) return prev
        const next = new Map(prev)
        next.set(conversationId, content)
        return next
      })

      let working = emptyStreamingMessage()
      setStreaming(working)

      try {
        for await (const ev of streamChatMessage(conversationId, content)) {
          switch (ev.type) {
            case 'token':
              working = appendTokenToStreaming(working, ev.text, ev.source)
              break
            case 'specialist_consultation_started':
              working = pushConsultationStarted(working, ev)
              break
            case 'specialist_consultation_complete':
              working = markConsultationComplete(working, ev)
              break
            case 'specialist_consultation_error':
              working = markConsultationError(working, ev)
              break
            case 'done':
              working = {
                ...working,
                done: true,
                totalInputTokens: ev.total_input_tokens,
                totalOutputTokens: ev.total_output_tokens,
              }
              break
            case 'error':
              working = { ...working, done: true, error: ev.error }
              break
          }
          setStreaming(working)
        }
      } catch (err) {
        setStreaming({
          ...working,
          done: true,
          error: errorMessage(err),
        })
        return
      }

      // If the stream emitted an error event, keep the partial
      // segments + error visible instead of clearing.
      if (working.error) {
        return
      }

      try {
        const resp = await conversationsApi.history(conversationId)
        setHistoryState({ convId: conversationId, messages: resp.messages })
      } catch {
        // Keep the streamed segments visible; refetch failure is
        // non-fatal and the user can navigate away and back.
      }
      setStreaming(null)
      void refetchConversations()
    },
    [selectedId, refetchConversations],
  )

  const showEmptyState =
    !convoLoading && !convoError && convoList.length === 0 && !selectedId

  return (
    <div className="flex h-full" style={{ background: '#f3f1ee' }}>
      <ConversationSidebar
        conversations={convoList}
        selectedId={selectedId}
        onSelect={handleSelect}
        onNewChat={handleNewChat}
        loading={convoLoading}
        error={convoError}
        firstMessages={firstMessages}
      />

      <div className="flex flex-col flex-1 min-w-0 h-full">
        {showEmptyState ? (
          <div
            className="flex-1 flex items-center justify-center"
            style={{ background: '#f3f1ee' }}
          >
            <div className="text-center">
              <div
                className="text-lg font-semibold mb-2"
                style={{ color: '#142234' }}
              >
                Start a new conversation
              </div>
              <div
                className="text-sm mb-4"
                style={{ color: '#858481' }}
              >
                Ask CoWorker a tax or accounting question to get started.
              </div>
              <button
                type="button"
                onClick={handleNewChat}
                className="btn-primary"
              >
                Start a new chat
              </button>
            </div>
          </div>
        ) : !selectedId ? (
          <div
            className="flex-1 flex items-center justify-center text-sm"
            style={{ color: '#858481', background: '#f3f1ee' }}
          >
            Select a conversation or start a new one.
          </div>
        ) : (
          <>
            {historyError && (
              <div
                className="px-6 py-2 text-sm"
                style={{ color: '#e11d48' }}
              >
                {historyError}
              </div>
            )}
            {historyLoading && history.length === 0 ? (
              <div
                className="flex-1 flex items-center justify-center text-sm"
                style={{ color: '#858481' }}
              >
                Loading…
              </div>
            ) : (
              <MessageList history={history} streaming={streaming} />
            )}
            <Composer
              onSend={handleSend}
              disabled={streaming !== null && !streaming.done}
              displayName={displayName}
            />
          </>
        )}
      </div>
    </div>
  )
}
