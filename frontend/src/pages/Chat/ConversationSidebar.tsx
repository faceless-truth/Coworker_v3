import { Plus } from 'lucide-react'
import type { ConversationSummary } from '../../api/client'
import { deriveTitle, relativeTime } from './utils'

interface Props {
  conversations: ConversationSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
  onNewChat: () => void
  loading: boolean
  error: string | null
  firstMessages: Map<string, string>
}

export default function ConversationSidebar({
  conversations,
  selectedId,
  onSelect,
  onNewChat,
  loading,
  error,
  firstMessages,
}: Props) {
  return (
    <aside
      className="flex flex-col h-full border-r flex-shrink-0"
      style={{ width: '280px', background: 'white', borderColor: '#d9d8d8' }}
    >
      <div className="px-4 py-3 border-b" style={{ borderColor: '#d9d8d8' }}>
        <button
          type="button"
          onClick={onNewChat}
          className="btn-primary flex items-center justify-center gap-2 w-full"
        >
          <Plus size={16} />
          New chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {error && (
          <div className="px-4 py-3 text-sm" style={{ color: '#e11d48' }}>
            {error}
          </div>
        )}

        {loading && !error && (
          <div
            className="px-4 py-3 text-sm"
            style={{ color: '#858481' }}
          >
            Loading…
          </div>
        )}

        {!loading && !error && conversations.length === 0 && (
          <div
            className="px-4 py-6 text-sm text-center"
            style={{ color: '#858481' }}
          >
            No conversations yet
          </div>
        )}

        {!loading && !error && conversations.length > 0 && (
          <ul>
            {conversations.map((c) => {
              const isSelected = selectedId === c.id
              const title = deriveTitle(firstMessages.get(c.id) ?? c.title)
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(c.id)}
                    className="w-full text-left px-4 py-3 border-b cursor-pointer"
                    style={{
                      background: isSelected ? '#f3f1ee' : 'white',
                      borderLeft: isSelected
                        ? '3px solid #eb881f'
                        : '3px solid transparent',
                      borderBottomColor: '#d9d8d8',
                      color: '#34322d',
                    }}
                  >
                    <div
                      className="text-sm font-medium"
                      style={{
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        overflow: 'hidden',
                      }}
                    >
                      {title}
                    </div>
                    <div
                      className="text-xs mt-1"
                      style={{ color: '#858481' }}
                    >
                      {relativeTime(c.updated_at)}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </aside>
  )
}
