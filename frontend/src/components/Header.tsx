import { useState, useEffect } from 'react'
import { Bell, LogOut, ChevronDown } from 'lucide-react'
import { type CurrentUser } from '../api/client'
import { approvals } from '../api/client'

interface HeaderProps {
  user: CurrentUser
  onLogout: () => Promise<void>
}

export default function Header({ user, onLogout }: HeaderProps) {
  const [pendingCount, setPendingCount] = useState<number | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const today = new Date().toLocaleDateString('en-AU', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  })

  // Poll pending approvals count every 30 seconds
  useEffect(() => {
    let cancelled = false
    async function fetchCount() {
      try {
        const data = await approvals.list({ status: 'pending', limit: 1 })
        if (!cancelled) setPendingCount(data.total)
      } catch {
        // backend not yet returning this endpoint — silently ignore
      }
    }
    fetchCount()
    const interval = setInterval(fetchCount, 30_000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  const initials = user.display_name
    .split(' ')
    .map((n) => n[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()

  return (
    <header
      className="flex items-center justify-between px-6 h-14 flex-shrink-0 border-b"
      style={{ background: 'white', borderColor: '#d9d8d8' }}
    >
      {/* Left: shadow mode badge + date */}
      <div className="flex items-center gap-4">
        <span
          className="inline-flex items-center gap-1.5 text-xs font-semibold px-2 py-1"
          style={{ background: 'rgba(22,163,74,0.1)', color: '#16a34a' }}
        >
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: '#16a34a' }}
          />
          SHADOW MODE
        </span>
        <span className="text-xs" style={{ color: '#858481' }}>
          {today}
        </span>
      </div>

      {/* Right: bell + user menu */}
      <div className="flex items-center gap-4">
        {/* Approvals bell — shows live count from polling */}
        <button className="relative" style={{ color: '#858481' }}>
          <Bell size={18} />
          {pendingCount !== null && pendingCount > 0 && (
            <span
              className="absolute -top-1 -right-1 w-4 h-4 rounded-full text-white flex items-center justify-center font-bold"
              style={{ background: '#eb881f', fontSize: '10px' }}
            >
              {pendingCount > 99 ? '99+' : pendingCount}
            </span>
          )}
        </button>

        {/* User avatar + dropdown */}
        <div className="relative">
          <button
            className="flex items-center gap-2"
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold text-white"
              style={{ background: '#142234' }}
            >
              {initials}
            </div>
            <span className="text-sm font-medium hidden sm:block" style={{ color: '#34322d' }}>
              {user.display_name}
            </span>
            <ChevronDown size={14} style={{ color: '#858481' }} />
          </button>

          {menuOpen && (
            <div
              className="absolute right-0 mt-2 w-48 bg-white border shadow-sm z-50"
              style={{ borderColor: '#d9d8d8', top: '100%' }}
            >
              <div className="px-4 py-3 border-b" style={{ borderColor: '#f3f1ee' }}>
                <div className="text-xs font-semibold" style={{ color: '#142234' }}>
                  {user.display_name}
                </div>
                <div className="text-xs mt-0.5" style={{ color: '#858481' }}>
                  {user.upn}
                </div>
                <div
                  className="text-xs mt-1 font-medium uppercase tracking-wider"
                  style={{ color: '#3080bc' }}
                >
                  {user.role}
                </div>
              </div>
              <button
                className="w-full flex items-center gap-2 px-4 py-3 text-sm text-left hover:bg-gray-50"
                style={{ color: '#34322d' }}
                onClick={async () => {
                  setMenuOpen(false)
                  await onLogout()
                }}
              >
                <LogOut size={14} />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
